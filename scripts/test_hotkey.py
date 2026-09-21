"""按键打断（voice_loop/hotkey.py）的离线测试——不用真键盘。

要守住的几条：
- 只有「正在说话」时才读键盘（否则会把 Ctrl+C 吃掉，后台服务退不出来）
- 刚开口时先把积压的按键丢掉（上一轮的回车不该打断这一轮）
- Esc / 回车 / Ctrl+C 触发，其它键不触发
- stop() 后线程真的退出，且收尾回调被调到（POSIX 下要还原终端设置）

    python scripts/test_hotkey.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.hotkey import (  # noqa: E402
    CTRL_C,
    ENTER_KEYS,
    ESC,
    INTERRUPT_KEYS,
    KeyWatcher,
    keys_from_spec,
)

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


class FakeKeys:
    """假键盘：可以随时 push 一个按键，也可以提前排队列。

    真实场景是「开始说话之后才按」，所以测试里也是先 push 再等回调，
    免得被「开口时丢掉积压按键」那一步吃掉（那是另一个测试）。
    """

    def __init__(self, keys: list[str] | None = None) -> None:
        self.keys = list(keys or [])
        self.reads = 0
        self.lock = threading.Lock()

    def push(self, *keys: str) -> None:
        with self.lock:
            self.keys.extend(keys)

    def read(self) -> str | None:
        with self.lock:
            self.reads += 1
            if not self.keys:
                return None
            return self.keys.pop(0)


def wait_for(pred, seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_spec() -> None:
    print("\n[1] 配置里的按键写法")
    check(keys_from_spec("esc"), (ESC, CTRL_C), "esc")
    check(keys_from_spec("esc+enter"), (ESC, *ENTER_KEYS, CTRL_C), "esc+enter")
    check(keys_from_spec("enter"), (*ENTER_KEYS, CTRL_C), "enter")
    check_true(CTRL_C in keys_from_spec("随便写点什么"), "认不出来时也保留 Ctrl+C")
    check_true(ESC in keys_from_spec("ESC, ENTER"), "大小写与逗号都认")
    check(INTERRUPT_KEYS, (ESC, *ENTER_KEYS, CTRL_C), "默认集合")


def test_fires() -> None:
    print("\n[2] 该触发才触发")
    hits: list[float] = []
    speaking = threading.Event()
    keys = FakeKeys()
    watcher = KeyWatcher(
        lambda: hits.append(time.monotonic()),
        active=speaking.is_set,
        reader=keys.read,
        poll=0.005,
    )
    watcher.start()

    keys.push(ESC)
    time.sleep(0.1)
    check(hits, [], "没在说话时，按了也不打断")

    speaking.set()
    time.sleep(0.15)  # 等「开口丢积压」那一步过去
    keys.push(ESC)
    check_true(wait_for(lambda: len(hits) >= 1), "说话时按 Esc → 打断")

    speaking.clear()
    watcher.stop()
    check_true(watcher._thread is None, "stop() 后线程句柄清空")


def test_ignores_other_keys() -> None:
    print("\n[3] 其它键不打断")
    hits: list[int] = []
    speaking = threading.Event()
    speaking.set()
    keys = FakeKeys()
    watcher = KeyWatcher(
        lambda: hits.append(1), active=speaking.is_set, reader=keys.read, poll=0.005
    )
    watcher.start()
    time.sleep(0.15)
    keys.push("a", "b", " ")
    time.sleep(0.15)
    check(hits, [], "a / b / 空格 都不触发")
    keys.push(ESC)
    check_true(wait_for(lambda: len(hits) == 1), "紧接着按 Esc 才触发")
    watcher.stop()
    check(len(hits), 1, "总共只触发一次")


def test_drains_backlog() -> None:
    print("\n[4] 刚开口时丢掉积压按键")
    hits: list[int] = []
    speaking = threading.Event()
    keys = FakeKeys(["\r", "\r"])  # 上一轮 ptt 留下的回车
    watcher = KeyWatcher(
        lambda: hits.append(1), active=speaking.is_set, reader=keys.read, poll=0.005
    )
    watcher.start()
    speaking.set()
    time.sleep(0.3)
    watcher.stop()
    check(hits, [], "积压的回车没把这一轮的开头打断")
    check(keys.keys, [], "积压的键确实被读走了")


def test_idle_does_not_read() -> None:
    print("\n[5] 没在说话时完全不读键盘（否则 Ctrl+C 会被吃掉）")
    keys = FakeKeys([CTRL_C, CTRL_C])
    watcher = KeyWatcher(lambda: None, active=lambda: False, reader=keys.read, poll=0.005)
    watcher.start()
    time.sleep(0.3)
    watcher.stop()
    check(keys.reads, 0, "一次都没读，Ctrl+C 还能正常退出")


def test_close_called() -> None:
    print("\n[6] stop() 会收尾（POSIX 下要还原终端设置）")
    done: list[int] = []
    watcher = KeyWatcher(lambda: None, active=lambda: False, reader=lambda: None, poll=0.005)
    watcher._close = lambda: done.append(1)
    watcher.start()
    watcher.stop()
    check(len(done), 1, "收尾回调被调到一次")


def main() -> int:
    test_spec()
    test_fires()
    test_ignores_other_keys()
    test_drains_backlog()
    test_idle_does_not_read()
    test_close_called()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    print("EXIT=" + ("0" if FAIL == 0 else "1"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
