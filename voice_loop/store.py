"""JSON 文件存储：闹钟、备忘、日程都用它。

设计目标
    - 文件由用户直接用编辑器修改，所以格式要简单、可读、带注释字段
    - 多个线程（对话线程 + 后台提醒线程）同时读写要安全
    - 写入用「临时文件 + 替换」，避免中途崩溃把数据写坏
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable


class JsonStore:
    """一个 JSON 数组文件，元素是 dict。

    如果文件是 ``{"_说明": ..., "items": [...]}`` 这种带注释的包装格式，
    保存时会**原样保留**注释字段，方便用户直接把说明写进文件里。
    """

    ITEMS_KEY = "items"

    def __init__(self, path: str | Path, default: Iterable[dict] | None = None) -> None:
        self.path = Path(path)
        self._default = list(default or [])
        self._lock = threading.RLock()
        self._cache: list[dict] | None = None
        self._mtime: float = 0.0
        self._meta: dict = {}          # 包装格式里除 items 之外的字段
        self._wrapped: bool = False    # 是否使用 {"items": [...]} 包装

    # ------------------------------------------------------------------ 基础
    def ensure(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._default)
        return self.path

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._wrapped and self._meta:
            payload: object = {**self._meta, self.ITEMS_KEY: items}
        else:
            payload = items
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._cache = items
        self._mtime = self.path.stat().st_mtime

    def load(self, force: bool = False) -> list[dict]:
        """读取内容；文件被外部改动过会自动重新加载。"""
        with self._lock:
            self.ensure()
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if force or self._cache is None or mtime != self._mtime:
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                except json.JSONDecodeError:
                    # 解析失败时备份坏文件，避免用户数据被静默覆盖
                    backup = self.path.with_suffix(self.path.suffix + f".bad{int(time.time())}")
                    try:
                        self.path.replace(backup)
                    except OSError:
                        pass
                    raw = list(self._default)
                    self._write(raw)
                if isinstance(raw, dict):  # {"_说明": ..., "items": [...]}
                    self._wrapped = True
                    self._meta = {k: v for k, v in raw.items() if k != self.ITEMS_KEY}
                    raw = raw.get(self.ITEMS_KEY, [])
                else:
                    self._wrapped = False
                    self._meta = {}
                self._cache = [x for x in raw if isinstance(x, dict)]
                self._mtime = mtime
            return self._cache

    def save(self, items: list[dict]) -> None:
        with self._lock:
            self._write(items)

    # ------------------------------------------------------------------ 增删
    def append(self, item: dict) -> dict:
        with self._lock:
            items = list(self.load())
            item = dict(item)
            item.setdefault("id", len(items) + 1)
            item.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            items.append(item)
            self.save(items)
            return item

    def update(self, index: int, **fields: Any) -> dict | None:
        """index 从 1 开始。"""
        with self._lock:
            items = list(self.load())
            if not (1 <= index <= len(items)):
                return None
            items[index - 1].update(fields)
            self.save(items)
            return items[index - 1]

    def remove_at(self, index: int) -> dict | None:
        with self._lock:
            items = list(self.load())
            if not (1 <= index <= len(items)):
                return None
            removed = items.pop(index - 1)
            self.save(items)
            return removed

    def remove_where(self, predicate) -> list[dict]:
        with self._lock:
            items = list(self.load())
            keep, removed = [], []
            for it in items:
                (removed if predicate(it) else keep).append(it)
            if removed:
                self.save(keep)
            return removed

    def clear(self) -> int:
        with self._lock:
            n = len(self.load())
            self.save([])
            return n

    def find(self, predicate) -> list[tuple[int, dict]]:
        """返回 (序号, 元素) 列表，序号从 1 开始，便于用户说「删除第2条」。"""
        return [(i + 1, it) for i, it in enumerate(self.load()) if predicate(it)]
