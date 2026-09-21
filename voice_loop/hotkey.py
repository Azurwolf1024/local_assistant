"""按键打断：默认 **Esc**（也不用按回车）。

为什么不按回车：回车在 ptt 模式里是「开始/结束录音」，两者会抢同一个输入；
而且回车要等一个换行才生效。Esc 是裸按键，按下即触发。

为什么不是「语音自动打断」：那个功能在真实环境里很难调准，实测最容易的失败
方式是**把自己外放的声音当成你在插话**，于是自己打断自己（详见
:mod:`voice_loop.bargein` 里的实测记录）。所以默认关掉，改成按 Esc——
确定性、零误触发，代价是你要抬手按一下。

设计上的一个细节：**只在「正在说话」时才读键盘**。
一直读的话会把 Ctrl+C 也吃掉（装在后台的监听就退不出来了）；
另外每轮开口前会先把积压的按键丢掉，免得上一轮 ptt 的回车把这一轮的开头打断。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable

# Esc 和（顺手也认）回车 / Ctrl+C
ESC = "\x1b"
ENTER_KEYS = ("\r", "\n")
CTRL_C = "\x03"
INTERRUPT_KEYS = (ESC, *ENTER_KEYS, CTRL_C)


def keys_from_spec(spec: str) -> tuple[str, ...]:
    """把配置里的 ``"esc+enter"`` 解析成按键集合（ctrl-c 永远在内）。

    支持 esc / enter / ctrl-c，用 ``+``、``,``、空格连接；认不出来就回到默认集合。
    """
    out: list[str] = []
    for part in re.split(r"[+,/\s]+", (spec or "").strip().lower()):
        if part in ("esc", "escape", "退出"):
            out.append(ESC)
        elif part in ("enter", "return", "回车"):
            out.extend(ENTER_KEYS)
        elif part in ("ctrl-c", "ctrl_c", "ctrl+c"):
            out.append(CTRL_C)
    if CTRL_C not in out:
        out.append(CTRL_C)
    return tuple(dict.fromkeys(out)) or INTERRUPT_KEYS


def make_key_reader() -> tuple[Callable[[], str | None], Callable[[], None]] | None:
    """返回 ``(读一个按键, 收尾)``；当前环境读不了就返回 None。

    「读一个按键」在没有按键时返回 None，**不阻塞**。
    """
    if not (sys.stdin is not None and sys.stdin.isatty()):
        return None
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            def read_key() -> str | None:
                if not msvcrt.kbhit():
                    return None
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # 方向键/功能键的前缀，连后面那个一起吃掉
                    msvcrt.getwch()
                    return None
                return ch

            return read_key, (lambda: None)

        # POSIX：进 cbreak 才能读到裸按键，退出时一定要还原
        import select  # noqa: PLC0415
        import termios  # noqa: PLC0415
        import tty  # noqa: PLC0415

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)

        def read_key_posix() -> str | None:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return None
            data = os.read(fd, 1)
            return data.decode("utf-8", "ignore") or None

        return read_key_posix, (lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved))
    except Exception:  # noqa: BLE001 - 读不了就算了，退化成没关系
        return None


class KeyWatcher:
    """后台线程：按下 Esc（或回车 / Ctrl+C）时回调 ``on_press``。

    ``active`` 决定「现在算不算在说话」——只有为真时才读键盘。
    测试可以直接塞一个假 ``reader`` 进来，不需要真键盘。
    """

    def __init__(
        self,
        on_press: Callable[[], None],
        active: Callable[[], bool],
        reader: Callable[[], str | None] | None = None,
        keys: tuple[str, ...] = INTERRUPT_KEYS,
        poll: float = 0.02,
        logger=None,
    ) -> None:
        self.on_press = on_press
        self.active = active
        self.keys = tuple(keys)
        self.poll = max(0.005, float(poll))
        self.log = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._close: Callable[[], None] = lambda: None
        self._closed = False
        self._close_lock = threading.Lock()
        if reader is not None:
            self.reader: Callable[[], str | None] = reader
        else:
            made = make_key_reader()
            if made is None:
                raise RuntimeError("这个环境读不到裸按键（没有交互终端）")
            self.reader, self._close = made

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._watch, daemon=True, name="hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        self._finish()

    def _finish(self) -> None:
        """收尾（还原终端设置）——stop() 和线程退出都会调，所以只允许做一次。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 内部
    def _drain(self, limit: int = 32) -> int:
        """把积压的按键丢掉（刚开口时用，免得上一轮的残留把自己打断）。"""
        dropped = 0
        for _ in range(limit):
            if self.reader() is None:
                break
            dropped += 1
        return dropped

    def _watch(self) -> None:
        was_active = False
        while not self._stop.is_set():
            try:
                if not self.active():
                    was_active = False
                    time.sleep(self.poll)
                    continue
                if not was_active:
                    dropped = self._drain()
                    was_active = True
                    if dropped and self.log:
                        self.log.debug(f"丢掉 {dropped} 个积压按键")
                key = self.reader()
            except Exception as exc:  # noqa: BLE001 - 键盘出问题不该影响说话
                if self.log:
                    self.log.warning(f"按键监听出错（已忽略）：{exc}")
                time.sleep(self.poll)
                continue
            if key is None:
                time.sleep(self.poll)
                continue
            if key in self.keys:
                self.on_press()
        self._finish()
