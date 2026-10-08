"""更新：把「有新版本了」变成界面上一颗按钮。

两条路，**自动挑**，用户不用懂自己是怎么装的：

============  ==============================  =====================================
git 检出       有 ``.git`` 且能 fetch 到远端      沿用 ``setup_flow.run_upgrade``：
                                               备份 → ``git pull --ff-only`` → 依赖 → 自检
压缩包/exe    没有 ``.git``（下载安装的）        读 GitHub Release 里的 ``latest.json``
                                               → 备份 → 下 zip → 校验 sha256 → 覆盖程序文件
============  ==============================  =====================================

★三条不许破的规矩★（跟 ``setup_flow`` 的升级同源，理由也在那里）：

1. **动手前先备份**：``data/``、``data/personas``、``config.toml`` 先拷进 ``data/backup/``，
   带 MANIFEST（sha256）与 RESTORE.txt。数据不能靠猜。
2. **绝不覆盖用户的东西**：``data/``、``models/``、``sessions/``、``.venv``、``config.toml``
   一律跳过（见 :data:`KEEP_NAMES` / :data:`KEEP_FILES`）。新版本多了配置项时，
   报告里会提示去 README 补 —— 而不是把用户的配置冲掉。
3. **校验不过就不动**：zip 的 sha256 对不上就当场停，连解压都不做。

★为什么自己解析 install.ps1★：仓库地址只写在一处（``install.ps1 -Repo`` 的默认值），
这里照 ``setup_flow.model_groups_known()`` 的老办法把它读出来，免得两处地址慢慢跑偏。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import setup_flow
from .paths import ENV_ROOT, app_root, exe_path

MANIFEST_NAME = "latest.json"
"""发布清单的文件名（``scripts/make_release.py`` 生成，挂在 Release 资产里）。"""

KEEP_NAMES = ("data", "models", "sessions", ".venv", "venv", "env", ".git", "dist",
              "__pycache__", ".mypy_cache", ".pytest_cache")
"""更新时**整个目录跳过**：用户数据、模型、运行日志、虚拟环境。"""

KEEP_FILES = ("config.toml",)
"""单个文件跳过：★它也是用户数据★（端口、角色默认值都在里面）。"""

CACHE_SECONDS = 600.0
"""检查结果在内存里放多久（界面来回切标签不该每次都打一次网络）。"""


# --------------------------------------------------------------------------- #
# 仓库地址 / 清单地址
# --------------------------------------------------------------------------- #
def default_repo() -> str:
    """默认仓库（``owner/name``）：从 ``install.ps1`` 的 ``-Repo`` 默认值里读。

    读不到就空字符串 —— 调用方会提示「用 --repo 指定」，**不瞎编一个地址**。
    """
    try:
        text = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(
            encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    got = re.search(r'\[string\]\$Repo\s*=\s*"([^"]+)"', text)
    if not got:
        return ""
    url = got.group(1).strip().rstrip("/").removesuffix(".git")
    if "github.com" not in url:
        return ""
    slug = url.split("github.com", 1)[-1].lstrip(":/")
    return slug if "/" in slug else ""


def manifest_url(repo: str) -> str:
    """清单地址：走 ``releases/latest/download/`` —— 不用 GitHub API，没有限流。"""
    return f"https://github.com/{repo.strip('/')}/releases/latest/download/{MANIFEST_NAME}"


def release_url(repo: str, filename: str, version: str = "") -> str:
    """资产地址：给了版本号就走「固定 tag」（回滚/锁定版本用），否则走 latest。"""
    base = f"https://github.com/{repo.strip('/')}/releases"
    if version:
        return f"{base}/download/v{version.lstrip('v')}/{filename}"
    return f"{base}/latest/download/{filename}"


def fetch(url: str, *, timeout: float = 10.0, max_bytes: int = 400 * 1024 * 1024) -> bytes:
    """取一段字节（stdlib urllib；★超时是硬的★，界面点一下不能卡住几十秒）。"""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url, headers={
        "User-Agent": "local-assistant-updater",
        "Accept": "application/json, application/octet-stream, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise ValueError(f"下载超过上限 {max_bytes} 字节，先停下来看看是不是地址不对")
                chunks.append(block)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{url} 返回 {exc.code}（发布里没有这个文件？）") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"连不上 {url}：{exc.reason}") from exc


# --------------------------------------------------------------------------- #
# 版本号比较
# --------------------------------------------------------------------------- #
def version_tuple(text: str) -> tuple[int, ...]:
    """``"1.2.0"`` → ``(1, 2, 0)``；带后缀的（``1.1.0+unknown`` / ``1.2.0-rc1``）只取数字段。"""
    parts: list[int] = []
    for token in re.split(r"[.\-+]", (text or "").strip().lstrip("v")):
        got = re.match(r"^\d+", token)
        if not got:
            break
        parts.append(int(got.group(0)))
    return tuple(parts)


def compare_versions(left: str, right: str) -> int:
    """-1 / 0 / 1（缺位补零，所以 ``1.2`` 与 ``1.2.0`` 相等）。"""
    a, b = version_tuple(left), version_tuple(right)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #
@dataclass
class Release:
    """发布清单里我们真正用的那几个字段（多出来的原样留着，报告里能看）。"""

    version: str = ""
    date: str = ""
    notes: str = ""
    git: str = ""
    zip_name: str = ""
    zip_url: str = ""
    zip_bytes: int = 0
    zip_sha256: str = ""
    exe_name: str = ""
    exe_url: str = ""
    exe_sha256: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def has_exe(self) -> bool:
        return bool(self.exe_url)

    def as_dict(self) -> dict:
        return {
            "version": self.version, "date": self.date, "notes": self.notes,
            "git": self.git, "zip": self.zip_name, "zip_bytes": self.zip_bytes,
            "zip_sha256": self.zip_sha256, "exe": self.exe_name,
        }


def parse_manifest(data: dict | bytes | str, *, repo: str = "") -> Release:
    """把 ``latest.json`` 变成 :class:`Release`（缺字段就缺着，不编造）。"""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        data = json.loads(data)
    assets = data.get("assets") or {}
    src = assets.get("source-zip") or {}
    exe = assets.get("console-exe") or {}
    rel = Release(
        version=str(data.get("version") or ""),
        date=str(data.get("date") or ""),
        notes=str(data.get("notes") or ""),
        git=str(data.get("git") or ""),
        zip_name=str(src.get("file") or ""),
        zip_url=str(src.get("url") or ""),
        zip_bytes=int(src.get("bytes") or 0),
        zip_sha256=str(src.get("sha256") or ""),
        exe_name=str(exe.get("file") or ""),
        exe_url=str(exe.get("url") or ""),
        exe_sha256=str(exe.get("sha256") or ""),
        raw=dict(data),
    )
    # 清单里 url 留空时（发布时没有 GitHub remote）自己拼一个，省得用户手动填
    if repo:
        if not rel.zip_url and rel.zip_name:
            rel.zip_url = release_url(repo, rel.zip_name, rel.version)
        if not rel.exe_url and rel.exe_name:
            rel.exe_url = release_url(repo, rel.exe_name, rel.version)
    return rel


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """一次「有没有新版本」的结论。界面只显示它，不做任何判断。"""

    ok: bool = False
    current: str = ""
    latest: str = ""
    behind: bool = False
    mode: str = "zip"
    """``"git"`` = 源码检出；``"zip"`` = 压缩包/exe 安装。"""
    reason: str = ""
    error: str = ""
    release: Release | None = None
    checked_at: float = 0.0
    commits: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "current": self.current, "latest": self.latest,
            "behind": self.behind, "mode": self.mode, "reason": self.reason,
            "error": self.error, "commits": self.commits,
            "checked_at": self.checked_at,
            "release": self.release.as_dict() if self.release else None,
        }


_CACHE: dict[str, Check] = {}
_CACHE_LOCK = threading.Lock()


def check(root: str | Path | None = None, *, repo: str = "", url: str = "",
          remote: str = "origin", branch: str = "", timeout: float = 10.0,
          use_cache: bool = True, fetch_json=fetch) -> Check:
    """看看有没有新版本。★只读★：不下载、不写文件、不起进程。

    ``fetch_json`` 可注入 —— 自测用假的取数函数，**绝不联网**（离线也能跑）。
    """
    root = Path(root) if root else app_root()
    repo = (repo or default_repo()).strip("/")
    key = f"{root}|{repo}|{url}"
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and (time.time() - hit.checked_at) < CACHE_SECONDS:
            return hit

    current = _current_version(root)
    if setup_flow.is_git(root):
        got = _check_git(root, current, remote=remote, branch=branch)
    else:
        got = _check_manifest(root, current, repo=repo, url=url, timeout=timeout,
                              fetch_json=fetch_json)
    got.checked_at = time.time()
    with _CACHE_LOCK:
        _CACHE[key] = got
    return got


def forget_cache() -> None:
    """忘掉检查结果（界面点「重新检查」时用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _current_version(root: Path) -> str:
    """当前装的版本：先看项目根的 VERSION（那是「装在你机器上的」）。"""
    try:
        text = (root / "VERSION").read_text(encoding="utf-8")
    except OSError:
        from . import __version__  # noqa: PLC0415

        return __version__
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    from . import __version__  # noqa: PLC0415

    return __version__


def _check_git(root: Path, current: str, *, remote: str, branch: str) -> Check:
    ok, err = setup_flow.fetch(root, remote)
    if not ok:
        return Check(ok=False, current=current, mode="git", error=err or "git fetch 失败",
                     reason=f"连不上远端（{remote}）")
    n, why = setup_flow.behind(root, branch or (setup_flow.current_branch(root) or "main"), remote)
    if n < 0:
        return Check(ok=False, current=current, mode="git", error=why, reason="远端分支对不上")
    return Check(ok=True, current=current, latest="", behind=n > 0, mode="git",
                 commits=n, reason="已经是最新版" if n == 0 else f"可以拉到 {n} 个新提交")


def _check_manifest(root: Path, current: str, *, repo: str, url: str, timeout: float,
                    fetch_json) -> Check:
    if not (url or repo):
        return Check(ok=False, current=current, mode="zip",
                     error="不知道去哪里检查（没有可用的下载地址）",
                     reason="用 --url 指定 latest.json 的地址，或 --repo owner/name")
    target = url or manifest_url(repo)
    try:
        blob = fetch_json(target, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络问题一律变成一句人话
        return Check(ok=False, current=current, mode="zip", error=f"{type(exc).__name__}: {exc}",
                     reason="检查不到（离线？地址不对？）")
    try:
        rel = parse_manifest(blob, repo=repo)
    except Exception as exc:  # noqa: BLE001
        return Check(ok=False, current=current, mode="zip",
                     error=f"清单读不懂：{type(exc).__name__}: {exc}",
                     reason=f"{target} 不像一份更新清单")
    if not rel.version:
        return Check(ok=False, current=current, mode="zip", error="清单里没有 version",
                     reason="这份清单不完整", release=rel)
    behind = compare_versions(rel.version, current) > 0
    return Check(ok=True, current=current, latest=rel.version, behind=behind, mode="zip",
                 release=rel,
                 reason=("已经是最新版" if not behind
                         else f"有新版本 {rel.version}（当前 {current}）"))


# --------------------------------------------------------------------------- #
# 更新（真的动手）
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    """一次更新的结果（界面按这个渲染）。"""

    ok: bool = False
    mode: str = ""
    before: str = ""
    after: str = ""
    backup: str = ""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed_upstream: list[str] = field(default_factory=list)
    exe_swapped: str = ""
    error: str = ""
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "mode": self.mode, "before": self.before, "after": self.after,
            "backup": self.backup, "added": self.added, "updated": self.updated,
            "removed_upstream": self.removed_upstream, "exe_swapped": self.exe_swapped,
            "error": self.error, "lines": self.lines,
        }


def _skip(rel: Path) -> bool:
    """更新时要不要跳过这个相对路径（用户的数据/环境，一律不碰）。"""
    if not rel.parts:
        return True
    if rel.parts[0] in KEEP_NAMES:
        return True
    if rel.as_posix() in KEEP_FILES:
        return True
    return any(part in ("__pycache__",) for part in rel.parts) or rel.suffix == ".pyc"


def plan_update(root: str | Path, release: Release) -> list[str]:
    """**只列会动哪些文件**（不给网络、不写盘 —— 「试运行」用的就是这个）。"""
    root = Path(root)
    name = release.zip_name or "local_assistant-source.zip"
    if not release.zip_url:
        raise ValueError("清单里没有下载地址（发布时用 --url-prefix 填上）")
    return [
        f"下载 {name}（{setup_flow.human(release.zip_bytes)}）→ 临时目录",
        f"校验 sha256{'（清单里有）' if release.zip_sha256 else '（清单里没有，跳过）'}",
        "备份 data/ 与 config.toml 到 data/backup/",
        "覆盖程序文件（跳过 data/ models/ sessions/ .venv/ config.toml）",
        "requirements.txt 变了就 pip install",
        f"如果发布里有 console-exe，替换 {exe_path().name if exe_path() else '（当前不是 exe，跳过）'}",
        "升级后自检（python scripts/check_deploy.py）",
    ]


def apply(root: str | Path | None = None, *, release: Release | None = None,
          repo: str = "", progress=print, backup: bool = True, dry_run: bool = False,
          update_exe: bool = True, fetch_bytes=fetch, install_deps: bool = True) -> Result:
    """压缩包安装的更新：备份 → 下载 → 校验 → 合并 → 依赖 → 报告。

    ★merge 而不是「解压覆盖」★：解压覆盖会把 ``data/``、``config.toml`` 一起盖掉，
    那正是升级最容易毁数据的一步；这里逐个文件判断，跳过 :func:`_skip` 里的东西。
    """
    root = Path(root) if root else app_root()
    out = Result(mode="zip", before=_current_version(root))

    def say(line: str) -> None:
        out.lines.append(line)
        try:
            progress(line)
        except Exception:  # noqa: BLE001 - 进度回调不该影响更新
            pass

    if release is None:
        got = check(root, repo=repo, use_cache=False, fetch_json=fetch_bytes)
        if not got.ok or not got.release:
            out.error = got.error or got.reason or "检查更新失败"
            say(f"× {out.error}")
            return out
        release = got.release
    if not release.zip_url:
        out.error = "清单里没有下载地址（发布时用 --url-prefix 填上）"
        say(f"× {out.error}")
        return out

    if dry_run:
        say("这是试运行：只会列出步骤，一个文件都不动")
        for line in plan_update(root, release):
            say(f"  · {line}")
        out.ok = True
        return out

    running, detail = setup_flow.service_running()
    if running:
        say(f"提醒：唤醒服务在跑（{detail}）—— 更新完请重启它，跑着的进程读的还是旧代码")

    if backup:
        try:
            bak, files, size = setup_flow.backup_user_data(root, reason="update",
                                                          tag=release.version or "")
        except Exception as exc:  # noqa: BLE001 - 备份失败必须停手
            out.error = f"备份失败，已停手：{type(exc).__name__}: {exc}"
            say(f"× {out.error}")
            return out
        out.backup = bak.relative_to(root).as_posix() if bak.is_relative_to(root) else str(bak)
        say(f"数据已备份：{out.backup}（{files} 个文件 · {setup_flow.human(size)}）")

    say(f"正在下载 {release.zip_name or release.zip_url}…")
    try:
        blob = fetch_bytes(release.zip_url, timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        out.error = f"下载失败：{type(exc).__name__}: {exc}"
        say(f"× {out.error}")
        return out

    if release.zip_sha256:
        got = hashlib.sha256(blob).hexdigest()
        if got.lower() != release.zip_sha256.lower():
            out.error = f"★校验不过，已停手★（期望 {release.zip_sha256[:16]}…，实际 {got[:16]}…）"
            say(f"× {out.error}")
            return out
        say("sha256 校验通过")
    else:
        say("清单里没有 sha256 —— 只做了文件大小检查"
            f"（{setup_flow.human(len(blob))}）")

    try:
        added, updated, stale = _merge_zip(root, blob, say=say)
    except Exception as exc:  # noqa: BLE001
        out.error = (f"合并失败：{type(exc).__name__}: {exc}"
                     f"{f'（备份在 {out.backup}，照里面的 RESTORE.txt 能还原）' if out.backup else ''}")
        say(f"× {out.error}")
        return out
    out.added, out.updated, out.removed_upstream = added, updated, stale
    say(f"程序文件：新增 {len(added)} · 更新 {len(updated)}")
    if stale:
        say(f"上游删掉、但你这里还在的文件 {len(stale)} 个（没动它们，见报告末尾）")

    if install_deps:
        rc, tail = setup_flow.pip_install()
        say("依赖：已重新执行 pip install -r requirements.txt" if rc == 0
            else f"依赖：pip install 退出码 {rc} —— {tail}")
    else:
        say("依赖：按要求跳过（--skip-deps）")

    if update_exe and release.exe_url:
        try:
            note = _swap_exe(release, fetch_bytes=fetch_bytes, say=say)
            out.exe_swapped = note
        except Exception as exc:  # noqa: BLE001 - exe 换不动不该让整次更新算失败
            say(f"提醒：exe 没能替换（{type(exc).__name__}: {exc}）—— 手动下新的 exe 覆盖即可")

    out.after = _current_version(root)
    out.ok = True
    say(f"更新完成：{out.before} → {out.after or '(版本号没变)'}")
    say("要生效请重启控制台（以及语音服务）—— 跑着的进程用的还是旧代码")
    return out


def _merge_zip(root: Path, blob: bytes, *, say=print) -> tuple[list[str], list[str], list[str]]:
    """把发布包里的**程序文件**合进项目根；返回 (新增, 更新, 上游已删)。

    ★zip 里的路径要自己校验★：成员名带 ``../`` 或绝对路径的话，
    解压会写到项目外面去（zip-slip）。这里直接拒绝可疑成员，不依赖 zipfile 的实现细节。
    """
    import tempfile  # noqa: PLC0415

    added: list[str] = []
    updated: list[str] = []
    with tempfile.TemporaryDirectory(prefix="la-update-") as tmp:
        tmpdir = Path(tmp)
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"下载下来的不是 zip（{exc}）") from exc
        with zf:
            tops: set[str] = set()
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts or any(p == ".." for p in parts):
                    raise ValueError(f"压缩包里有个可疑路径：{info.filename}")
                if len(parts) == 1 and info.is_dir():
                    tops.add(parts[0])
                    continue
                target = tmpdir.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        # 发布包的顶层目录（make_release 会套一层 local_assistant-<版本>/）
        roots = [p for p in tmpdir.iterdir() if p.is_dir()]
        src_root = roots[0] if len(roots) == 1 else tmpdir

        packed: set[str] = set()
        for path in sorted(src_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src_root)
            if _skip(rel):
                continue
            packed.add(rel.as_posix())
            dst = root / rel
            try:
                if dst.is_file() and dst.read_bytes() == path.read_bytes():
                    continue
                is_new = not dst.exists()
            except OSError:
                is_new = True
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
            (added if is_new else updated).append(rel.as_posix())

    # 上游删掉的文件：只报告（自动删用户的文件太危险，尤其这些人可能自己加过东西）
    stale: list[str] = []
    for watch in ("voice_loop", "scripts"):
        base = root / watch
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel not in packed and "__pycache__" not in rel:
                stale.append(rel)
    return added, updated, stale


def _swap_exe(release: Release, *, fetch_bytes=fetch, say=print) -> str:
    """替换正在运行的 exe 自己。返回一句给用户看的话。"""
    me = exe_path()
    if me is None:
        return ""
    say(f"正在下载新版 exe（{release.exe_name or 'console-exe'}）…")
    blob = fetch_bytes(release.exe_url, timeout=180.0)
    if release.exe_sha256:
        got = hashlib.sha256(blob).hexdigest()
        if got.lower() != release.exe_sha256.lower():
            raise ValueError("exe 的 sha256 对不上，没换（旧的那个还在）")
    old = replace_exe(me, blob)
    say(f"exe 已替换（旧的留在 {old.name}，下次启动自动清掉）—— ★关掉窗口重开就是新版★")
    return str(me)


def replace_exe(target: str | Path, data: bytes) -> Path:
    """把 exe 换成新的，返回旧文件的路径。

    ★Windows 的规矩★：运行中的 exe **不能被覆盖**，但**可以被改名**。
    所以顺序是「先把自己改名成 .old，再把新内容写到原来的名字上」——
    不用批处理、不用重启管理器那一套。
    """
    target = Path(target)
    old = target.with_name(target.name + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            old = target.with_name(f"{target.name}.{int(time.time())}.old")
    target.rename(old)
    target.write_bytes(data)
    return old


def cleanup_old_exe(target: str | Path | None = None) -> list[str]:
    """清掉上次更新留下的 ``*.old``（★启动时调一次★；删不掉就留着，不报错）。"""
    me = Path(target) if target else exe_path()
    if me is None:
        return []
    removed: list[str] = []
    for path in me.parent.glob(me.name + "*.old"):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- #
# 给界面用的异步包装
# --------------------------------------------------------------------------- #
class Updater:
    """把「一次更新」跑在后台线程里，界面轮询 :meth:`snapshot` 看进度。

    ★为什么不直接同步跑★：下载 + 解压 + pip install 可能好几分钟，
    同步的话浏览器就白等到超时（用户以为坏了，其实正在装）。
    """

    def __init__(self, root: str | Path | None = None, *, fetch_bytes=fetch) -> None:
        self.root = Path(root) if root else app_root()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {}
        self._fetch = fetch_bytes
        """下载函数（★可注入★：自测拿它顶掉网络 —— 升级逻辑不该靠联网才能验）。"""
        self._reset()

    def _reset(self) -> None:
        self._state = {
            "running": False, "started_at": 0.0, "finished_at": 0.0, "ok": None,
            "error": "", "lines": [], "result": None, "mode": "", "version": "",
        }

    def snapshot(self, *, limit: int = 400) -> dict:
        with self._lock:
            state = dict(self._state)
            state["lines"] = list(self._state["lines"])[-limit:]
            return state

    def _log(self, line: str) -> None:
        with self._lock:
            self._state["lines"].append(str(line))

    def start(self, *, check_result: Check | None = None, repo: str = "", dry_run: bool = False,
              backup: bool = True, update_exe: bool = True, install_deps: bool = True
              ) -> tuple[bool, str]:
        """开始更新。已经在跑就返回 (False, 原因)。"""
        with self._lock:
            if self._state["running"]:
                return False, "已经在更新了（看下面的进度）"
            self._reset()
            self._state["running"] = True
            self._state["started_at"] = time.time()
            self._state["mode"] = (check_result.mode if check_result else "")
            self._state["version"] = (check_result.latest if check_result else "")

        def work() -> None:
            try:
                if check_result is not None and check_result.mode == "git":
                    self._log("这份是 git 检出 —— 走 git pull（先备份）")
                    rep = setup_flow.run_upgrade(apply=True, remote="origin", root=self.root,
                                                skip_deps=not install_deps, echo=self._log)
                    text = rep.render(indent="")
                    for line in text.splitlines():
                        self._log(line)
                    result = Result(ok=rep.ok, mode="git",
                                    before=check_result.current,
                                    after=_current_version(self.root),
                                    backup=str(setup_flow.latest_backup(self.root) or ""),
                                    error="" if rep.ok else "；".join(s.name for s in rep.failed),
                                    lines=self.snapshot()["lines"])
                else:
                    result = apply(self.root,
                                   release=(check_result.release if check_result else None),
                                   repo=repo, progress=self._log, backup=backup,
                                   update_exe=update_exe, install_deps=install_deps,
                                   dry_run=dry_run, fetch_bytes=self._fetch)
                with self._lock:
                    self._state["result"] = result.as_dict()
                    self._state["ok"] = bool(result.ok)
                    self._state["error"] = result.error
            except Exception as exc:  # noqa: BLE001 - 后台线程里的意外也要落到界面上
                self._log(f"× 更新中断：{type(exc).__name__}: {exc}")
                with self._lock:
                    self._state["ok"] = False
                    self._state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._state["running"] = False
                    self._state["finished_at"] = time.time()

        self._thread = threading.Thread(target=work, name="la-updater", daemon=True)
        self._thread.start()
        return True, "开始更新"


def env_hint() -> str:
    """exe 场景的一句话提示（界面/命令行都用它，措辞只有一份）。"""
    me = exe_path()
    if me is None:
        return f"源码运行（项目根 {app_root()}）"
    return f"exe 运行（{me}）· 项目根 {app_root()} · 可用 {ENV_ROOT} 改项目根"
