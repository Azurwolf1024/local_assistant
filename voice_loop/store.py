"""JSON 文件存储：闹钟、备忘、日程都用它。

设计目标
    - 文件由用户直接用编辑器修改，所以格式要简单、可读、带注释字段
    - 多个线程（对话线程 + 后台提醒线程）同时读写要安全
    - ★多个**进程**同时读写也要安全★（控制台 UI 是另一个进程，见下）
    - 写入用「临时文件 + 替换」，避免中途崩溃把数据写坏

## 跨进程写安全（为什么需要，实测过）

以前只有进程内 ``threading.RLock``，而本项目的写入模式是**整表回写**
（读全部 → 改一条 → 写全部）。两个进程各持一份快照时，后写的会**静默覆盖**前一个的改动：

    服务：load() → 想给 A 标 state.fired → save()
    UI  ：          load() → 删掉 B      → save()      ← 服务那一笔白改 / UI 那一笔白删

⚠️ 而且它**不报错**，只是数据不见了。所以 ``load()`` 能看见别人的改动（靠 mtime）
并不能解决问题——**能看见**和**不会互相覆盖**是两件事。

现在用一个**单独的锁文件**（``<文件>.lock``）跨进程互斥：
    - 为什么不锁数据文件本身：写盘是 ``tmp.replace(path)``，**inode 被换掉了**，
      锁在被删掉的那个 inode 上没有意义（新读者看到的是新文件，锁形同虚设）。
    - 锁是**进程内可重入**的：同一路径的嵌套使用（``due_now`` 里再调 ``save``）不会自己把自己锁死。
    - 拿不到就等（默认 5 秒），超时才抛，所以不会因为一个卡死的进程把服务挂住。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------- 跨进程锁
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_DEPTH = threading.local()          # 每线程持有的路径计数（用于可重入）
LOCK_TIMEOUT = 5.0                  # 等锁超时（秒）


def _lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        got = _LOCKS.get(key)
        if got is None:
            got = _LOCKS[key] = threading.RLock()
        return got


def _os_acquire(fh) -> None:
    """拿操作系统的文件锁（阻塞，带超时）。"""
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"等锁超时（{LOCK_TIMEOUT:g}s）") from None
                time.sleep(0.02)
    else:
        import fcntl  # noqa: PLC0415

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _os_release(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass                        # 关文件也会释放，解锁失败不算错


@contextlib.contextmanager
def cross_process_lock(path: str | Path) -> Iterator[None]:
    """对 ``path`` 对应的锁文件加跨进程互斥锁（可重入，可嵌套）。

    用法（★读-改-写必须整段包起来★，只包写那一半没用）：

        with cross_process_lock(json_path):
            items = store.load(force=True)
            items[0]["x"] = 1
            store.save(items)
    """
    key = str(Path(path).resolve())
    local = _lock_for(key)
    with local:                                    # ① 进程内先互斥
        depth = getattr(_DEPTH, "depth", None)
        if depth is None:
            depth = _DEPTH.depth = {}
        held = depth.get(key, 0)
        depth[key] = held + 1
        if held:                                   # ② 同一线程嵌套：不再拿系统锁
            try:
                yield
            finally:
                depth[key] -= 1
            return
        lock_path = Path(str(Path(path)) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as fh:
            try:
                if fh.seek(0, os.SEEK_END) == 0:
                    fh.write(b"0")             # 锁需要至少 1 字节
                    fh.flush()
            except OSError:
                pass
            _os_acquire(fh)
            try:
                yield
            finally:
                _os_release(fh)
                depth[key] -= 1


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
        self._stamp: tuple[int, int] = (0, -1)   # (mtime_ns, 大小)：外部改动检测
        self._meta: dict = {}          # 包装格式里除 items 之外的字段
        self._wrapped: bool = False    # 是否使用 {"items": [...]} 包装

    # ------------------------------------------------------------------ 基础
    @property
    def lock_path(self) -> Path:
        """跨进程锁文件（非数据文件：写盘会换 inode，锁数据文件没有意义）。"""
        return Path(str(self.path) + ".lock")

    def locked(self) -> contextlib.AbstractContextManager[None]:
        """跨进程互斥锁。★「读 → 改 → 写」要整段包进来★，只包写那一半不管用。

        例：
            with store.locked():
                items = store.load(force=True)
                ...改...
                store.save(items)

        ★传的是**数据文件路径**★（不是 :attr:`lock_path`）：``cross_process_lock``
        自己会补 ``.lock``，两个都补就会变成 ``x.json.lock.lock``（踩过）。
        """
        return cross_process_lock(self.path)

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
        try:
            st = self.path.stat()
            self._stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._stamp = (0, -1)

    def load(self, force: bool = False) -> list[dict]:
        """读取内容；文件被外部改动过会自动重新加载。

        变更检测用 ``(mtime_ns, 大小)``而不是只比 mtime：文件系统时间戳精度
        在不同盘上不一样（FAT 系只到 2 秒），**同一毫秒里写两次**时只比 mtime
        会误判成「没变」而拿到旧缓存；带上文件大小能挡掉绝大多数这种情况。
        """
        with self._lock:
            self.ensure()
            try:
                st = self.path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = (0, -1)
            if force or self._cache is None or stamp != self._stamp:
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
                self._stamp = stamp
            return self._cache

    def save(self, items: list[dict]) -> None:
        with self._lock:
            self._write(items)

    # ------------------------------------------------------------------ 增删
    # ★下面这些「读-改-写」必须在锁内**强制重读**★：
    #   外部（控制台 UI / 用户编辑器）刚改过的话，拿缓存快照写回去等于把别人的改动抹掉。
    def append(self, item: dict) -> dict:
        with self.locked(), self._lock:
            items = list(self.load(force=True))
            item = dict(item)
            item.setdefault("id", len(items) + 1)
            item.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            items.append(item)
            self.save(items)
            return item

    def update(self, index: int, **fields: Any) -> dict | None:
        """index 从 1 开始。"""
        with self.locked(), self._lock:
            items = list(self.load(force=True))
            if not (1 <= index <= len(items)):
                return None
            items[index - 1].update(fields)
            self.save(items)
            return items[index - 1]

    def remove_at(self, index: int) -> dict | None:
        with self.locked(), self._lock:
            items = list(self.load(force=True))
            if not (1 <= index <= len(items)):
                return None
            removed = items.pop(index - 1)
            self.save(items)
            return removed

    def remove_where(self, predicate) -> list[dict]:
        with self.locked(), self._lock:
            items = list(self.load(force=True))
            keep, removed = [], []
            for it in items:
                (removed if predicate(it) else keep).append(it)
            if removed:
                self.save(keep)
            return removed

    def clear(self) -> int:
        with self.locked(), self._lock:
            n = len(self.load(force=True))
            self.save([])
            return n

    def find(self, predicate) -> list[tuple[int, dict]]:
        """返回 (序号, 元素) 列表，序号从 1 开始，便于用户说「删除第2条」。"""
        return [(i + 1, it) for i, it in enumerate(self.load()) if predicate(it)]
