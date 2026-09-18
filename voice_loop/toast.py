"""可视提醒：右下角弹一个置顶小窗。

为什么需要它：提醒只靠语音的话，扬声器关了、音量静音、或者戴耳机离开了，
就完全错过了。这里补一个明显的、点一下就能关掉的窗口。

实现要点
    - 只用标准库 tkinter，不引额外依赖
    - Tk 必须在同一个线程里操作，所以起一个专用线程 + 队列
    - 窗口置顶但不抢焦点（不会打断你正在打的字）
    - 点「知道了」或右上角 × 关闭；``timeout`` 秒后也会自动消失
    - 没有图形环境 / tkinter 不可用时静默降级，不影响语音主流程
"""

from __future__ import annotations

import logging
import queue
import threading
import time


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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed = False

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.enabled or self.running:
            return self.running
        self._thread = threading.Thread(target=self._run, daemon=True, name="toast")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def show(self, title: str, text: str) -> None:
        """非阻塞地弹一个提醒；开关关闭或不可用时直接忽略。"""
        if not self.enabled or self._failed:
            return
        if not self.running:
            self.start()
        self._queue.put((str(title), str(text)))

    # ------------------------------------------------------------------ 线程内
    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self.log.warning(f"没有 tkinter（{exc}），可视提醒已关闭")
            return

        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self.log.warning(f"无法创建窗口（{exc}），可视提醒已关闭")
            return

        root.withdraw()
        try:
            root.attributes("-topmost", False)
        except Exception:  # noqa: BLE001
            pass

        popups: list = []
        while not self._stop.is_set():
            try:
                while True:
                    title, text = self._queue.get_nowait()
                    popups.append(self._popup(root, title, text))
            except queue.Empty:
                pass
            # 丢掉已经被关掉 / 自动消失的
            alive = []
            for p in popups:
                try:
                    if p.winfo_exists():
                        alive.append(p)
                except Exception:  # noqa: BLE001
                    pass
            popups = alive
            self._layout(root, popups)
            try:
                root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.05)

        for p in popups:
            try:
                p.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

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

    def _layout(self, root, popups: list) -> None:
        """从屏幕右下角往上堆叠。"""
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
        except Exception:  # noqa: BLE001
            return
        y_offset = 0
        for win in reversed(popups):
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
