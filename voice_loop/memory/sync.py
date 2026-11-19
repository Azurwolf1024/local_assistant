"""记忆云端同步：把每个角色那一份记忆镜像到一个**私有** git 仓库。

为什么不是一个账号 / 一个数据库：记忆就是磁盘上的 json（README 第 15 节），
主仓库里 `data/memory/` 被 `.gitignore` 挡着（它是隐私），可它又要能跨机器用 ——
所以单独一个「只你自己能看」的仓库，用 git 当同步协议：版本历史顺手就是「后悔药」。

仓库里放什么（跟本地设计一一对应）：

    memory/<角色>/episodes.jsonl   L2 情景记忆（会衰减、会合并）
    memory/<角色>/facts.json       L3 事实（身份类 pinned，不衰减）
    memory/<角色>/sessions.json    L1 索引：哪些原始对话已经归档过
    memory/<角色>/knowledge/       L4 这个角色自己的知识库（有才同步）
    manifest.json                  这台机器最后一次同步的清单（角色 / 条数 / 时间）
    README.md                      第一次同步时写下的说明（别把这个仓库设成公开）

★L1 原始对话（`sessions/session-*.jsonl`）不上去★：按设计它是「滑动窗口定期清掉」的
工作记忆，云端留一份既没用（下次归档会重算）又最隐私（整段对话原文）。真要归档，
那是另一件事（导出成压缩包自己存），不该混进「记忆」。

两条方向的语义（不对称，这是故意的）：

| 方向 | 语义 | 为什么 |
| --- | --- | --- |
| 本地 → 云端（`export`） | **镜像**：本地删了，云端也删 | 不然「忘掉一条」会被下一次 pull 又拉回来；删掉的东西在 git 历史里还能捞 |
| 云端 → 本地（`pull`） | **合并**：按 id / key 取新，本地独有的照样留着 | 换机器时要把云端的经历带回来，但绝不能把本地新记的东西抹掉 |

安全边界：

- 推之前**问一次 GitHub「这个仓库是不是 private」**（免鉴权查询；查不到就放过，
  但会把地址打出来）。真撞上公开仓库 → 直接拒绝，必须显式 `--allow-public`。
- git 一律 `GIT_TERMINAL_PROMPT=0` + `GCM_INTERACTIVE=never`：凭据没缓存时**立刻失败**，
  绝不在退出服务时弹个窗把进程挂住。
- 绝不把 token 写进仓库：推拉都走系统凭据（Windows 上是 Git Credential Manager）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

from .model import now_iso
from .store import read_json, read_jsonl, rewrite_jsonl, write_json

DEFAULT_BRANCH = "main"
DEFAULT_CLONE = "memory_cloud"
REPO_MEMORY = "memory"                 # 仓库里的目录名：memory/<角色>/…
LOCAL_FILES = ("episodes.jsonl", "facts.json", "sessions.json")
# ★L1 连续上下文★：段/摘要/状态三个子目录（见 memory/context.py）
CONTEXT_DIR = "context"
SEGMENT_DIR = "segments"
DIGEST_DIR = "digests"
CONTEXT_STATE = "state.json"
STATE_FILE = "manifest.json"
README_FILE = "README.md"
KNOWLEDGE_EXTS = (".md", ".txt", ".json")
KNOWLEDGE_MAX_BYTES = 2 * 1024 * 1024   # 单文件上限：知识库是文本，2 MB 已经很夸张了

README_MARK = "<!-- memory-sync:structure -->"
README_TEXT = f"""{README_MARK}
## 结构与同步规则（自动维护，别手改这一段）

这个仓库由本地语音助手（`python main.py memsync`）自动同步，**请保持 Private** ——
里面是个人记忆：经历、事实、偏好。

```
memory/<角色>/episodes.jsonl   L2 情景记忆（会衰减、会合并）
memory/<角色>/facts.json       L3 事实（名字/称呼这类是 pinned，不会掉）
memory/<角色>/sessions.json    L1 索引（哪些原始对话已归档）
memory/<角色>/knowledge/       L4 这个角色自己的知识库
manifest.json                  最近一次同步的清单（角色 / 条数 / 设备 / 时间）
```

| 方向 | 语义 | 说明 |
| --- | --- | --- |
| 本地 → 这里 | **镜像** | 本地删了这里也删；删掉的内容在 git 历史里还能找回来 |
| 这里 → 本地 | **合并** | 按 episode `id` / fact `key` 取新的那份，本地独有的保留 |

**L1 原始对话（`sessions/session-*.jsonl`）不在这个仓库里**：按设计它是会过期的工作记忆，
而且整段对话原文最隐私 —— 要归档就单独导出，不混进「记忆」。
"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def memory_root(settings: Any) -> Path:
    """记忆根目录（跟 :class:`voice_loop.memory.MemoryHub` 用同一套规则）。"""
    cfg = getattr(settings, "memory", None)
    configured = str(getattr(cfg, "dir", "") or "").strip()
    base = Path(str(getattr(settings, "root", ".") or "."))
    if configured:
        path = Path(configured)
    else:
        data_dir = str(getattr(getattr(settings, "skills", None), "data_dir", "data") or "data")
        path = Path(data_dir) / "memory"
    return path if path.is_absolute() else (base / path)


def characters_of(root: str | Path) -> list[str]:
    """本地有记忆的角色（目录里得有记忆文件或知识库，别把空目录当角色）。"""
    base = Path(root)
    if not base.is_dir():
        return []
    out = []
    for item in sorted(base.iterdir()):
        if not item.is_dir():
            continue
        has = any((item / name).is_file() for name in LOCAL_FILES) or (item / "knowledge").is_dir()
        if has and not item.name.startswith("."):
            out.append(item.name)
    return out


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _newer(left: str, right: str) -> bool:
    """时间字符串比较（本项目的时间戳一律 `YYYY-MM-DD HH:MM:SS`，字典序即可）。"""
    return str(left or "") > str(right or "")


def _git(args: list[str], cwd: str | Path, timeout: float = 120.0) -> tuple[int, str, str]:
    """跑一条 git 命令。★不交互★：没凭据就立刻失败，别把退出流程挂住。"""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        # 中文 Windows 上 git 的输出可能是 GBK；解码失败就替换，别因为日志炸掉
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    try:
        got = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except FileNotFoundError:
        return 127, "", "找不到 git（先装 Git for Windows）"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} 超时（{timeout:.0f}s）"
    except OSError as exc:  # noqa: BLE001
        return 126, "", f"{type(exc).__name__}: {exc}"
    # ★stdout 只去掉行尾换行★：`git status --porcelain` 的第一列是「暂存区状态」，
    # 空的时候就是一个前导空格 —— 整段 strip 掉会把 ` M README.md` 变成 `M README.md`，
    # 再按 `line[3:]` 切就切出 `EADME.md`（真踩过）。
    return got.returncode, (got.stdout or "").strip("\r\n"), (got.stderr or "").strip()


def parse_github(url: str) -> tuple[str, str] | None:
    """从远端地址里取出 (owner, repo)，认不出就 None。"""
    text = str(url or "").strip()
    if "github.com" not in text:
        return None
    tail = text.split("github.com", 1)[-1].lstrip(":/")
    tail = tail.replace(":", "/")                 # git@github.com:owner/repo.git 这种写法
    parts = [p for p in tail.replace(".git", "").split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def remote_visibility(url: str, timeout: float = 5.0) -> tuple[str, str]:
    """这个 GitHub 仓库是不是公开的？返回 ``(private|public|unknown, 说明)``。

    ★免鉴权的问法★：公开仓库谁都能查（200 + `"private": false`）；
    私有仓库对匿名请求是 404 —— 正好和「查不到」一个样，所以判不准时**不拦**，
    只把地址打出来让人自己确认（宁可多一条提示，也不要因为网络问题挡住同步）。
    """
    pair = parse_github(url)
    if pair is None:
        return "unknown", "不是 GitHub 仓库（是不是私有得你自己确认）"
    api = f"https://api.github.com/repos/{pair[0]}/{pair[1]}"
    req = urlrequest.Request(api, headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "local-assistant-memory-sync"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - 固定域名
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urlerror.HTTPError as exc:
        if exc.code == 404:
            return "unknown", "匿名查不到（私有仓库就是这个表现，也可能仓库名写错了）"
        return "unknown", f"查询失败：HTTP {exc.code}"
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        return "unknown", f"查询失败：{type(exc).__name__}: {exc}"
    if data.get("private") is True:
        return "private", "私有 ✓"
    if data.get("private") is False:
        return "public", "★公开仓库★"
    return "unknown", "返回里没有 private 字段"


# --------------------------------------------------------------------------- #
# 合并（只用在「云端 → 本地」这一侧）
# --------------------------------------------------------------------------- #
def merge_episodes(local: list[dict], cloud: list[dict]) -> tuple[list[dict], dict]:
    """按 `id` 取新（`updated_at` 晚的赢），两边独有的都留着。"""
    by_id: dict[str, dict] = {}
    stats = {"added": 0, "updated": 0, "kept": 0}
    for row in cloud:
        rid = str(row.get("id") or "")
        if rid:
            by_id[rid] = row
    for row in local:
        rid = str(row.get("id") or "")
        if not rid:
            continue
        old = by_id.get(rid)
        if old is None:
            by_id[rid] = row                     # 本地独有的：留着
            stats["kept"] += 1
            continue
        if _newer(row.get("updated_at", ""), old.get("updated_at", "")):
            by_id[rid] = row
            stats["updated"] += 1
    stats["added"] = len(by_id) - len(local)
    out = sorted(by_id.values(), key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return out, stats


def merge_facts(local: list[dict], cloud: list[dict]) -> tuple[list[dict], dict]:
    """按 `key` 取新（`last_seen` 晚的赢）；`evidence` 取大、`pinned` 或起来、`sources` 并起来。"""
    by_key: dict[str, dict] = {}
    stats = {"added": 0, "updated": 0, "kept": 0}
    for row in cloud:
        key = str(row.get("key") or "")
        if key:
            by_key[key] = dict(row)
    for row in local:
        key = str(row.get("key") or "")
        if not key:
            continue
        old = by_key.get(key)
        if old is None:
            by_key[key] = dict(row)
            stats["kept"] += 1
            continue
        if _newer(row.get("last_seen", ""), old.get("last_seen", "")):
            winner, loser = dict(row), old
            stats["updated"] += 1
        else:
            winner, loser = dict(old), row
        winner["evidence"] = max(int(winner.get("evidence") or 1), int(loser.get("evidence") or 1))
        winner["pinned"] = bool(winner.get("pinned")) or bool(loser.get("pinned"))
        winner["confidence"] = max(float(winner.get("confidence") or 0.0),
                                   float(loser.get("confidence") or 0.0))
        sources = list(dict.fromkeys([*(winner.get("sources") or []), *(loser.get("sources") or [])]))
        winner["sources"] = sources
        first = [str(winner.get("first_seen") or ""), str(loser.get("first_seen") or "")]
        winner["first_seen"] = min([t for t in first if t] or [now_iso()])
        by_key[key] = winner
    stats["added"] = len(by_key) - len(local)
    out = sorted(by_key.values(), key=lambda r: (str(r.get("about") or ""), str(r.get("key") or "")))
    return out, stats


def merge_sessions(local: dict, cloud: dict) -> tuple[dict, dict]:
    """按文件名（key）并起来，`extracted_at` 晚的赢。"""
    out = dict(cloud or {})
    stats = {"added": 0, "updated": 0, "kept": 0}
    for name, row in (local or {}).items():
        old = out.get(name)
        if old is None:
            out[name] = row
            stats["kept"] += 1
        elif _newer((row or {}).get("extracted_at", ""), (old or {}).get("extracted_at", "")):
            out[name] = row
            stats["updated"] += 1
    stats["added"] = len(out) - len(local or {})
    return out, stats


# --------------------------------------------------------------------------- #
# 同步器
# --------------------------------------------------------------------------- #
@dataclass
class SyncReport:
    """一次同步做了什么（命令行直接打这个，控制台/日志也能用）。"""

    ok: bool = True
    action: str = ""                                  # export / pull / push / status
    clone: str = ""
    branch: str = DEFAULT_BRANCH
    remote: str = ""
    characters: dict[str, dict] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)
    commit: str = ""
    pushed: bool = False
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "action": self.action, "clone": self.clone,
            "branch": self.branch, "remote": self.remote,
            "characters": self.characters, "changed": self.changed,
            "commit": self.commit, "pushed": self.pushed,
            "warnings": self.warnings, "notes": self.notes, "error": self.error,
        }

    def summary(self) -> str:
        if not self.ok:
            return f"同步失败：{self.error}"
        if self.action == "status":
            return "；".join(self.notes) or "干净"
        bits = [f"{len(self.characters)} 个角色"]
        total = {"episodes": 0, "facts": 0, "sessions": 0}
        for got in self.characters.values():
            for key in total:
                total[key] += int(got.get(key) or 0)
        bits.append("、".join(f"{k} {v}" for k, v in total.items() if v))
        if self.changed:
            bits.append("改动 " + "、".join(self.changed[:6]))
        if self.pushed:
            bits.append("已推送" + (f"（{self.commit[:7]}）" if self.commit else ""))
        elif self.commit:
            bits.append(f"已提交未推送（{self.commit[:7]}）")
        else:
            bits.append("云端没有变化")
        return "；".join(bits)


class MemorySync:
    """记忆 ↔ 私有 git 仓库。``settings`` 只用来定位本地记忆目录。"""

    def __init__(
        self,
        settings: Any,
        logger: logging.Logger | None = None,
        *,
        clone_dir: str | Path | None = None,
        remote: str = "",
        branch: str = "",
        timeout: float = 120.0,
    ) -> None:
        cfg = getattr(settings, "memory", None)
        root = Path(str(getattr(settings, "root", ".") or "."))
        want = str(clone_dir or getattr(cfg, "sync_dir", "") or DEFAULT_CLONE)
        path = Path(want)
        self.clone_dir = path if path.is_absolute() else (root / path)
        self.remote = str(remote or getattr(cfg, "sync_remote", "") or "").strip()
        self.branch = str(branch or getattr(cfg, "sync_branch", "") or DEFAULT_BRANCH).strip()
        self.timeout = float(timeout or getattr(cfg, "sync_timeout", 120) or 120)
        self.local_root = memory_root(settings)
        self.log = logger or logging.getLogger("voice_loop.memory.sync")
        self.settings = settings

    # ------------------------------------------------------------------ git
    def _git(self, args: list[str]) -> tuple[int, str, str]:
        return _git(args, self.clone_dir, timeout=self.timeout)

    def _git_at(self, args: list[str], cwd: str | Path) -> tuple[int, str, str]:
        return _git(args, cwd, timeout=self.timeout)

    def remote_url(self) -> str:
        rc, out, _ = self._git(["remote", "get-url", "origin"])
        return out if rc == 0 else ""

    def ensure_repo(self, report: SyncReport) -> bool:
        """准备好本地克隆：没有就 clone，有就用；远端为空仓库时改成 init + 远端。"""
        self.clone_dir.parent.mkdir(parents=True, exist_ok=True)
        if (self.clone_dir / ".git").is_dir():
            report.clone = str(self.clone_dir)
            url = self.remote_url()
            if not url and self.remote:
                rc, _, err = self._git(["remote", "add", "origin", self.remote])
                if rc != 0:
                    report.ok, report.error = False, f"设远端失败：{err}"
                    return False
            report.remote = self.remote_url() or self.remote
            self._ensure_identity()
            return True
        if self.clone_dir.is_dir() and any(self.clone_dir.iterdir()):
            report.ok = False
            report.error = (f"{self.clone_dir} 里有东西但不是 git 仓库 —— "
                            "换个目录（--dir），或者先把它清空")
            return False
        if not self.remote:
            self.clone_dir.mkdir(parents=True, exist_ok=True)
            rc, _, err = self._git(["init", "-b", self.branch])
            if rc != 0:
                report.ok, report.error = False, f"git init 失败：{err}"
                return False
            self._ensure_identity()
            report.clone, report.remote = str(self.clone_dir), ""
            report.notes.append("没有配远端，只在本地建了仓库（要推就设 [memory] sync_remote）")
            return True
        # ★clone 的 cwd 得是父目录★：目标是还不存在的路径，拿它当 cwd 会 FileNotFoundError
        rc, _, err = self._git_at(
            ["clone", "--branch", self.branch, self.remote, str(self.clone_dir)],
            self.clone_dir.parent)
        if rc != 0:
            # 远端是**空仓库**时 clone 会因为「找不到分支」失败：改成 init + 接上远端
            self.clone_dir.mkdir(parents=True, exist_ok=True)
            rc2, _, err2 = self._git(["init", "-b", self.branch])
            rc3, _, err3 = self._git(["remote", "add", "origin", self.remote])
            if rc2 != 0 or rc3 != 0:
                report.ok = False
                report.error = f"克隆失败（{err}），改成 init 也失败（{err2 or err3}）"
                return False
            report.notes.append("远端还是空的：先建了个空仓库，第一次推送会自动建分支")
        self._ensure_identity()
        report.clone = str(self.clone_dir)
        report.remote = self.remote_url() or self.remote
        return True

    def _ensure_identity(self) -> None:
        """这个克隆得有 `user.name/email`，否则 commit 直接失败。

        优先级：克隆里已有的 → **项目仓库里那份**（同一台机器上本来就是同一个人，
        提交记录才不会一半是你一半是「unknown」）→ 一个中性的兜底身份。
        """
        _, name, _ = self._git(["config", "user.name"])
        _, mail, _ = self._git(["config", "user.email"])
        if name.strip() and mail.strip():
            return
        project = Path(str(getattr(self.settings, "root", ".") or "."))
        for key, current in (("user.name", name), ("user.email", mail)):
            if current.strip():
                continue
            rc, got, _ = _git(["config", key], project, timeout=10)
            if rc == 0 and got.strip():
                self._git(["config", key, got.strip()])
        _, name, _ = self._git(["config", "user.name"])
        _, mail, _ = self._git(["config", "user.email"])
        if not name.strip():
            self._git(["config", "user.name", "local-assistant"])
        if not mail.strip():
            self._git(["config", "user.email", "assistant@localhost"])

    # --------------------------------------------------------------- 导出
    def _copy_char(self, cid: str, report: SyncReport) -> None:
        src = self.local_root / cid
        dst = self.clone_dir / REPO_MEMORY / cid
        dst.mkdir(parents=True, exist_ok=True)
        counts = {"episodes": 0, "facts": 0, "sessions": 0, "context": 0}
        for name in LOCAL_FILES:
            source, target = src / name, dst / name
            if source.is_file():
                shutil.copyfile(source, target)
                if name == "episodes.jsonl":
                    counts["episodes"] = len(read_jsonl(target))
                elif name == "facts.json":
                    counts["facts"] = len(read_json(target, []) or [])
                else:
                    counts["sessions"] = len(read_json(target, {}) or {})
            elif target.exists():
                target.unlink()                  # 本地删了 → 云端也删（镜像；git 历史里还在）
        # ★连续上下文也要上云★：换机器/换设备后「今天聊到哪儿」得接得上。
        # 段文件是追加写的不可变文件，镜像过来一个字节都不会变（前缀缓存照样命中）。
        ctx_src = src / CONTEXT_DIR
        ctx_dst = dst / CONTEXT_DIR
        if ctx_src.is_dir():
            ctx_dst.mkdir(parents=True, exist_ok=True)
            for sub in (SEGMENT_DIR, DIGEST_DIR):
                room = ctx_dst / sub
                keep = ctx_src / sub
                if room.exists():
                    shutil.rmtree(room)
                if keep.is_dir():
                    room.mkdir(parents=True, exist_ok=True)
                    for item in sorted(keep.glob("*.json*")):
                        if item.is_file():
                            shutil.copyfile(item, room / item.name)
                            counts["context"] += 1
            state = ctx_src / CONTEXT_STATE
            if state.is_file():
                shutil.copyfile(state, ctx_dst / CONTEXT_STATE)
        elif ctx_dst.exists():
            shutil.rmtree(ctx_dst)
        keep = src / "knowledge"
        room = dst / "knowledge"
        if keep.is_dir():
            if room.exists():
                shutil.rmtree(room)
            room.mkdir(parents=True, exist_ok=True)
            for item in sorted(keep.rglob("*")):
                if not item.is_file() or item.suffix.lower() not in KNOWLEDGE_EXTS:
                    continue
                if item.stat().st_size > KNOWLEDGE_MAX_BYTES:
                    report.warnings.append(
                        f"{cid}/knowledge/{item.name} 超过 "
                        f"{KNOWLEDGE_MAX_BYTES // 1024 // 1024} MB，没同步")
                    continue
                rel = item.relative_to(keep)
                (room / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, room / rel)
        elif room.exists():
            shutil.rmtree(room)
        report.characters[cid] = counts

    def export(self, report: SyncReport, *, characters: Iterable[str] | None = None) -> None:
        """本地 → 仓库（每个本地角色镜像过去）。"""
        report.action = "export"
        ids = list(characters) if characters is not None else characters_of(self.local_root)
        if not ids:
            # ★这条闸门是防手滑的★：记忆目录指错 / 刚换机器 / 自测用了临时目录时，
            # 「镜像」会变成「把云端清空」。宁可什么都不做，让人自己看清楚。
            report.ok = False
            report.error = ("本地一个角色记忆都没有 —— 不动云端（否则等于把它清空）。"
                            "刚换机器的话先用 `memsync --pull` 把云端的记忆拉下来")
            return
        for cid in ids:
            self._copy_char(Path(cid).name, report)
        cloud = self.clone_dir / REPO_MEMORY
        extra = sorted(set(characters_of(cloud)) - {Path(c).name for c in ids})
        if extra:
            report.warnings.append(
                "云端还有这些角色的记忆、本地没有（没动它们）：" + "、".join(extra))
        self._write_manifest(report)
        self._write_readme()

    def _write_manifest(self, report: SyncReport) -> None:
        """写清单。★只在内容真的变了时才写★：里面有时间戳，无条件重写会让
        「什么都没改」也每次产生一个提交（同步记录会变成一串噪声）。"""
        cloud = self.clone_dir / REPO_MEMORY
        chars = sorted(p.name for p in cloud.iterdir() if p.is_dir()) if cloud.is_dir() else []
        body = {
            "device": socket.gethostname(),
            "characters": {cid: report.characters.get(cid, {}) for cid in chars},
            "counts": {
                "characters": len(chars),
                "episodes": sum(v.get("episodes", 0) for v in report.characters.values()),
                "facts": sum(v.get("facts", 0) for v in report.characters.values()),
                "sessions": sum(v.get("sessions", 0) for v in report.characters.values()),
            },
            "layout": "memory/<character>/{episodes.jsonl,facts.json,sessions.json,knowledge/}",
            "note": "L1 原始对话不同步（会过期的工作记忆）；本地→云端是镜像，云端→本地是合并",
        }
        path = self.clone_dir / STATE_FILE
        old = read_json(path, {}) or {}
        same = {k: v for k, v in old.items() if k != "updated_at"} == body
        if same and path.is_file():
            return
        path.write_text(json.dumps({"updated_at": now_iso(), **body},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_readme(self) -> None:
        """把「结构 + 同步规则」写进仓库的 README。

        ★追加而不是覆盖★：那个 README 是你建仓库时写的，我们只负责往后加一段
        （带标记，写过一次就不再写 —— 免得每次同步都改一行）。
        """
        path = self.clone_dir / README_FILE
        old = ""
        if path.is_file():
            try:
                old = path.read_text(encoding="utf-8")
            except OSError:
                old = ""
        if README_MARK in old:
            return
        head = old.rstrip()
        text = (head + "\n\n" if head else "") + README_TEXT
        path.write_text(text, encoding="utf-8")

    # --------------------------------------------------------------- 提交推送
    def commit(self, report: SyncReport, *, message: str = "") -> None:
        rc, out, _ = self._git(["status", "--porcelain"])
        if rc != 0:
            report.ok, report.error = False, "读不到仓库状态（git status 失败）"
            return
        if not out.strip():
            report.notes.append("云端没有变化，不提交")
            return
        paths = [line[3:].strip() for line in out.splitlines() if line.strip()]
        report.changed = paths[:40]
        self._git(["add", "-A"])
        text = message.strip() or self._auto_message(report)
        rc, _, err = self._git(["commit", "-m", text])
        if rc != 0:
            report.ok, report.error = False, f"提交失败：{err}"
            return
        rc, sha, _ = self._git(["rev-parse", "HEAD"])
        report.commit = sha if rc == 0 else ""

    def _auto_message(self, report: SyncReport) -> str:
        total = {"episodes": 0, "facts": 0, "sessions": 0}
        for got in report.characters.values():
            for key in total:
                total[key] += int(got.get(key) or 0)
        bits = [f"{k} {v}" for k, v in total.items() if v]
        head = f"memory: 同步 {len(report.characters)} 个角色"
        if bits:
            head += "（" + "、".join(bits) + "）"
        return f"{head}\n\n设备 {socket.gethostname()} · {now_iso()}\n"

    def push(self, report: SyncReport, *, allow_public: bool = False) -> None:
        url = self.remote_url()
        report.remote = url
        if not url:
            report.notes.append("没有远端，跳过推送")
            return
        if not allow_public:
            vis, why = remote_visibility(url, timeout=min(8.0, self.timeout))
            report.notes.append(f"仓库可见性：{why}")
            if vis == "public":
                report.ok = False
                report.error = ("远端是**公开**仓库，记忆里全是个人信息，不能往上推。"
                                "把它设成 Private 再来；确实要公开就加 --allow-public（不建议）")
                return
        rc, out, err = self._git(["push", "origin", f"HEAD:{self.branch}"])
        if rc != 0:
            report.ok = False
            report.error = f"推送失败：{err or '（没输出，可能是凭据没缓存）'}"
            return
        if "everything up-to-date" in f"{out} {err}".lower():
            report.notes.append("远端已是最新（没有可推的提交）")
            return
        report.pushed = True

    def sync(self, *, message: str = "", allow_public: bool = False,
             dry_run: bool = False) -> SyncReport:
        """★一条龙★：导出 → 提交 → 推送（``dry_run`` 只导出并报告，不落 git）。"""
        report = SyncReport()
        if not self.ensure_repo(report):
            return report
        self.export(report)
        if dry_run:
            report.notes.append("试运行：仓库已更新，但没有提交/推送")
            return report
        self.commit(report, message=message)
        if report.ok:
            self.push(report, allow_public=allow_public)
        return report

    # ----------------------------------------------------------------- 拉取
    def _backup(self, path: Path) -> str:
        if not path.is_file():
            return ""
        target = path.with_name(f"{path.name}.bak-sync-{_stamp()}")
        try:
            shutil.copyfile(path, target)
            return str(target)
        except OSError:
            return ""

    def pull(self, *, dry_run: bool = False) -> SyncReport:
        """云端 → 本地（合并：按 id/key 取新，本地独有的留着）。"""
        report = SyncReport()
        if not self.ensure_repo(report):
            return report
        report.action = "pull"
        if self.remote_url():
            rc, _, err = self._git(["fetch", "origin", self.branch])
            if rc != 0:
                report.ok, report.error = False, f"fetch 失败：{err}"
                return report
            rc, _, err = self._git(["merge", "--ff-only", f"origin/{self.branch}"])
            if rc != 0:
                report.ok, report.error = False, (
                    f"先把云端合进来失败（{err}）—— 大概本地克隆里有没推送的提交，"
                    "先 `--dry-run` 看一眼，或者手动处理这个克隆目录")
                return report
        cloud_root = self.clone_dir / REPO_MEMORY
        local_ids = set(characters_of(self.local_root))
        cloud_ids = set(characters_of(cloud_root))
        orphan = sorted(cloud_ids - local_ids)
        if orphan:
            report.warnings.append(
                "云端有这些角色的记忆、本地没有对应目录（没往本地造）：" + "、".join(orphan))
        ids = sorted(local_ids)
        for cid in ids:
            got = self._merge_char(Path(cid).name, cloud_root / Path(cid).name, dry_run=dry_run)
            report.characters[cid] = got
            if got.get("changed"):
                report.changed.append(f"{cid}:{','.join(got['changed'])}")
        if not report.changed:
            report.notes.append("云端的记忆都已经在本地了")
        return report

    def _merge_char(self, cid: str, cloud_dir: Path, *, dry_run: bool) -> dict:
        local_dir = self.local_root / cid
        out = {"episodes": 0, "facts": 0, "sessions": 0, "changed": []}
        if not cloud_dir.is_dir():
            return out

        ep_local = read_jsonl(local_dir / "episodes.jsonl")
        ep_cloud = read_jsonl(cloud_dir / "episodes.jsonl")
        if ep_cloud:
            merged, stats = merge_episodes(ep_local, ep_cloud)
            out["episodes"] = len(merged)
            if merged != ep_local:
                out["changed"].append("episodes")
                if not dry_run:
                    self._backup(local_dir / "episodes.jsonl")
                    rewrite_jsonl(local_dir / "episodes.jsonl", merged)
            out["episodes_added"] = stats["added"]
            out["episodes_updated"] = stats["updated"]

        facts_local = read_json(local_dir / "facts.json", []) or []
        facts_cloud = read_json(cloud_dir / "facts.json", []) or []
        if facts_cloud:
            merged, stats = merge_facts(facts_local, facts_cloud)
            out["facts"] = len(merged)
            if merged != facts_local:
                out["changed"].append("facts")
                if not dry_run:
                    self._backup(local_dir / "facts.json")
                    write_json(local_dir / "facts.json", merged)
            out["facts_added"] = stats["added"]
            out["facts_updated"] = stats["updated"]

        ses_local = read_json(local_dir / "sessions.json", {}) or {}
        ses_cloud = read_json(cloud_dir / "sessions.json", {}) or {}
        if ses_cloud:
            merged, _ = merge_sessions(ses_local, ses_cloud)
            out["sessions"] = len(merged)
            if merged != ses_local:
                out["changed"].append("sessions")
                if not dry_run:
                    self._backup(local_dir / "sessions.json")
                    write_json(local_dir / "sessions.json", merged)
        got = self._merge_context(local_dir, cloud_dir, dry_run=dry_run)
        out["context"] = got["files"]
        if got["changed"]:
            out["changed"].append("context")
        return out

    def _merge_context(self, local_dir: Path, cloud_dir: Path, *,
                       dry_run: bool) -> dict:
        """合并 L1 连续上下文（★按 uid 合并段，不是整段覆盖★）。

        段文件虽然"封存后不改"，但两台机器各自追加过同一段时（同一个 seg 号、
        不同的行），整段覆盖就会丢东西。所以：同一段的行按 `uid` 并起来、按 `seq` 排。
        摘要按日期取 `dream_at` 新的；`state.json` 取 `last_dream_seq` 大的那份
        （= 记的账更靠前，不会被旧机器拉回去重复做梦）。
        """
        src = cloud_dir / CONTEXT_DIR
        dst = local_dir / CONTEXT_DIR
        out = {"files": 0, "changed": False}
        if not src.is_dir():
            return out
        for sub in (SEGMENT_DIR, DIGEST_DIR):
            room = dst / sub
            room.mkdir(parents=True, exist_ok=True)
            for item in sorted((src / sub).glob("*.json*")):
                if not item.is_file():
                    continue
                target = room / item.name
                if sub == SEGMENT_DIR:
                    known = {str(r.get("uid") or "") for r in read_jsonl(target)}
                    fresh = [r for r in read_jsonl(item)
                             if str(r.get("uid") or "") not in known]
                    if fresh:
                        out["changed"] = True
                        if not dry_run:
                            rows = read_jsonl(target) + fresh
                            rows.sort(key=lambda r: int(r.get("seq") or 0))
                            self._backup(target)
                            rewrite_jsonl(target, rows)
                else:
                    got = read_json(item, {}) or {}
                    have = read_json(target, {}) or {}
                    if _newer(str(got.get("dream_at") or ""), str(have.get("dream_at") or "")):
                        out["changed"] = True
                        if not dry_run:
                            write_json(target, got)
                out["files"] += 1
        cloud_state = read_json(src / CONTEXT_STATE, {}) or {}
        local_state = read_json(dst / CONTEXT_STATE, {}) or {}
        if cloud_state and int(cloud_state.get("last_dream_seq") or 0) > \
                int(local_state.get("last_dream_seq") or 0):
            merged = dict(cloud_state)
            # ★本机的段号不能降★：降了就会往一个已经写满的旧段里追加
            merged["seg"] = max(int(cloud_state.get("seg") or 1),
                                int(local_state.get("seg") or 1))
            merged["seq"] = max(int(cloud_state.get("seq") or 0),
                                int(local_state.get("seq") or 0))
            out["changed"] = True
            if not dry_run:
                write_json(dst / CONTEXT_STATE, merged)
        return out

    # ----------------------------------------------------------------- 状态
    def status(self) -> SyncReport:
        report = SyncReport(action="status")
        if not self.ensure_repo(report):
            return report
        url = self.remote_url()
        report.remote = url
        rc, out, _ = self._git(["status", "--porcelain"])
        report.changed = [line[3:].strip() for line in (out or "").splitlines() if line.strip()][:40]
        report.notes.append("未提交的改动 " + (f"{len(report.changed)} 个文件" if report.changed
                                            else "无"))
        if url:
            rc, _, _ = self._git(["fetch", "origin", self.branch])
            rc, ahead, _ = self._git(["rev-list", "--count", f"origin/{self.branch}..HEAD"])
            rc2, behind, _ = self._git(["rev-list", "--count", f"HEAD..origin/{self.branch}"])
            if rc == 0 and rc2 == 0:
                report.notes.append(f"本地领先 {ahead or 0} 个提交、落后 {behind or 0} 个")
            if str(ahead).strip() not in ("", "0"):
                report.notes.append("★有没推上去的同步★")
        total = {}
        cloud_root = self.clone_dir / REPO_MEMORY
        for cid in characters_of(cloud_root):
            total[cid] = {
                "episodes": len(read_jsonl(cloud_root / cid / "episodes.jsonl")),
                "facts": len(read_json(cloud_root / cid / "facts.json", []) or []),
                "sessions": len(read_json(cloud_root / cid / "sessions.json", {}) or {}),
            }
        report.characters = total
        return report


def sync_memory(settings: Any, *, pull: bool = False, dry_run: bool = False,
                allow_public: bool = False, message: str = "",
                logger: logging.Logger | None = None) -> SyncReport:
    """给 pipeline / 命令行用的一步到位入口（失败不抛异常，看 ``report``）。"""
    sync = MemorySync(settings, logger)
    try:
        if pull:
            return sync.pull(dry_run=dry_run)
        return sync.sync(message=message, allow_public=allow_public, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - 退出流程绝不能因为同步炸掉
        got = SyncReport(ok=False, error=f"{type(exc).__name__}: {exc}")
        if logger is not None:
            logger.warning(f"[记忆同步] {got.error}")
        return got
