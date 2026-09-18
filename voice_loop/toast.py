"""可视提醒：右下角弹一个置顶小窗。

为什么需要它：提醒只靠语音的话，扬声器关了、音量静音、或者戴耳机离开了，
就完全错过了。这里补一个明显的、点一下就能关掉的窗口。

实现要点
    - 只用标准库 tkinter，不引额外依赖
    - 窗口挂在 :mod:`voice_loop.ui` 的共享 UI 线程上（一个进程只能有一个 Tk 解释器，
      这里和字幕各起一个线程会直接抛 ``main thread is not in main loop``）
    - 窗口置顶但不抢焦点（不会打断你正在打的字）
    - 点「知道了」或按 Esc 关闭；``timeout`` 秒后也会自动消失
    - 没有图形环境 / tkinter 不可用时静默降级，不影响语音主流程
"""

from __future__ import annotations

import logging
import queue
import time

from . import ui


class VisualNotifier:
    def __init__(
        self,
        enabled: bool = True,
        timeout: float = 25.0,
        width: int = 420,
        logger: logging.Logger | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.timeout = max(3.0, float(timeout))
        self.width = max(240, int(width))
        self.log = logger or logging.getLogger("voice_loop")

        self._queue: queue.Queue = queue.Queue()
        self._host: ui.UiHost | None = None
        self._closing = False

        # 以下都是 UI 线程里的状态
        self._popups: list = []
        self._last_render = 0.0

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._host is not None and not self._closing

    @property
    def failed(self) -> bool:
        return self._host is None or self._host.failed

    def start(self) -> bool:
        if not self.enabled or self.running:
            return self.running
        self._closing = False
        h = ui.host(self.log)
        if not h.start():
            return False
        self._host = h
        h.add_tick(self._tick)
        return True

    def stop(self) -> None:
        self._closing = True
        h, self._host = self._host, None
        if h is not None:
            # 先撤掉刷新回调，再让 UI 线程去销毁窗口，顺序不能反
            h.remove_tick(self._tick)
            h.post(self._close_all)

    def show(self, title: str, text: str) -> None:
        """非阻塞地弹一个提醒；开关关闭或不可用时直接忽略。"""
        if not self.enabled or self._closing:
            return
        if self._host is None and not self.start():
            return
        if self._host is None or self._host.failed:
            return
        self._queue.put((str(title), str(text)))

    # ------------------------------------------------------------------ UI 线程
    def _close_all(self, _root) -> None:
        for win in self._popups:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._popups = []

    def _tick(self, root) -> None:  # pragma: no cover - 需要图形环境
        if self._closing:
            return
        now = time.monotonic()
        if self._queue.empty() and now - self._last_render < 0.05:
            return
        self._last_render = now

        try:
            while True:
                title, text = self._queue.get_nowait()
                self._popups.append(self._popup(root, title, text))
        except queue.Empty:
            pass

        # 丢掉已经关掉 / 自动消失的
        alive = []
        for win in self._popups:
            try:
                if win.winfo_exists():
                    alive.append(win)
            except Exception:  # noqa: BLE001
                pass
        self._popups = alive
        self._layout(root)

    # ------------------------------------------------------------------ 窗口
    def _popup(self, root, title: str, text: str):
        import tkinter as tk

        win = tk.Toplevel(root)
        win.title(title)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg="#1b1f2a")

        header = tk.Label(
            win, text=title, bg="#1b1f2a", fg="#8fd3ff",
            font=("Microsoft YaHei UI", 11, "bold"), anchor="w",
        )
        header.pack(fill="x", padx=14, pady=(10, 2))

        body = tk.Label(
            win, text=text, bg="#1b1f2a", fg="#f2f4f8",
            font=("Microsoft YaHei UI", 12), wraplength=self.width - 30,
            justify="left", anchor="w",
        )
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        def close(_event=None):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

        btn = tk.Button(
            win, text="知道了", command=close, relief="flat",
            bg="#2f6feb", fg="white", activebackground="#1d5ad4",
            activeforeground="white", font=("Microsoft YaHei UI", 10),
            padx=16, pady=2, cursor="hand2",
        )
        btn.pack(anchor="e", padx=14, pady=(0, 10))

        win.bind("<Escape>", close)
        win.after(int(self.timeout * 1000), close)
        return win

    def _layout(self, root) -> None:
        """从屏幕右下角往上堆叠。"""
        if not self._popups:
            return
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
        except Exception:  # noqa: BLE001
            return
        y_offset = 0
        for win in reversed(self._popups):
            try:
                win.update_idletasks()
                h = win.winfo_height()
                w = win.winfo_width()
                x = sw - w - 24
                y = sh - h - 60 - y_offset
                win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
                y_offset += h + 10
            except Exception:  # noqa: BLE001
                continue
