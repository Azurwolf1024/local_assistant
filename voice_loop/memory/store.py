"""记忆的落盘：一个角色一个目录（★隔离★），原子写 + 跨进程锁（复用 voice_loop.store）。

```
data/memory/
  <角色 id>/
    episodes.jsonl     L2 情景记忆（一行一条，便于追加）
    facts.json         L3 语义记忆（整表回写，量小）
    sessions.json      L1 状态：哪些原始对话已经归档过了（★清原始文件前要查它★）
    knowledge/         L4 该角色自己的知识库（可选；全局的在 data/knowledge/）
```

★共享层不在这个目录里★：日程/备忘/提醒是 `data/events.json`（`voice_loop/events.py`），
所有角色读写同一份 —— 这是刻意的：**记忆隔离，但日程是共用的事实**。

为什么要 `sessions.json`：L1 的「滑动窗口 + 定期清除」必须**先归档再删**，
否则会出现「原始对话删了、事件也没提出来」的信息黑洞。这份状态记的就是「这个文件提取过没有」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..store import cross_process_lock

SAFE_ID = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


def safe_id(name: str) -> str:
    """角色 id → 安全的目录名（★防路径穿越★：id 来自 json，不能直接拼路径）。"""
    cleaned = SAFE_ID.sub("_", (name or "").strip())
    return cleaned[:64] or "default"


class MemoryPaths:
    """一个角色名下所有记忆文件的位置。"""

    def __init__(self, root: str | Path, character: str) -> None:
        self.root = Path(root)
        self.character = safe_id(character)
        self.dir = self.root / self.character
        self.episodes = self.dir / "episodes.jsonl"
        self.facts = self.dir / "facts.json"
        self.sessions = self.dir / "sessions.json"
        self.knowledge = self.dir / "knowledge"

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # pragma: no cover - 只为日志好看
        return f"MemoryPaths({self.character} @ {self.dir})"


# --------------------------------------------------------------------------- #
# 小工具：原子写 JSON / 追加 JSONL / 整表回写 JSONL
# --------------------------------------------------------------------------- #
def read_json(path: Path, default: Any) -> Any:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # ★坏文件不静默丢弃★：留一份 .bad 备份，然后当空的重建（记忆丢了也要留痕）
        try:
            path.with_suffix(path.suffix + ".bad").write_text(raw, encoding="utf-8")
        except OSError:
            pass
        return default


def write_json(path: Path, data: Any) -> None:
    """临时文件 + 替换（中途崩了不会把记忆写坏）。

    ★空内容不落盘★：`[]`/`{}` 且文件还不存在时直接返回 ——
    这样「只是读了一下 / 试跑一下」不会在记忆库里撒一地空文件，
    也不会把真实记忆目录建出来（自测污染过真实数据，踩过）。
    文件已经存在时照写（用户真的忘光了也要落盘）。
    """
    if not data and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    """读 JSONL。★坏行跳过并留在原地★（一行写坏不该毁掉整个记忆库）。"""
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            got = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def append_jsonl(path: Path, records: Iterable[dict]) -> int:
    """追加若干行（带跨进程锁）。返回实际写了几条。"""
    rows = [json.dumps(r, ensure_ascii=False) for r in records]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(row + "\n")
    return len(rows)


def rewrite_jsonl(path: Path, records: Iterable[dict]) -> int:
    """整表回写（自清洁/合并用）。带锁 + 原子替换。

    ★空内容不落盘★：同 write_json —— 整表被清空且文件不存在时不动磁盘。
    """
    rows = [json.dumps(r, ensure_ascii=False) for r in records]
    if not rows and not path.exists():
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(path):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
        tmp.replace(path)
    return len(rows)


def session_key(path: Path) -> str:
    """原始对话文件的标识（就用文件名，够稳定、够可读）。"""
    return path.name
