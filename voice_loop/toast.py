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
        theme: str = "auto",
        shadow: int = 3,
    ) -> None:
        self.enabled = bool(enabled)
        self.timeout = max(3.0, float(timeout))
        self.width = max(280, int(width))
        self.theme_name = str(theme or "auto")
        self._shadow = max(0, int(shadow))
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
        """画一张圆角卡片（参照 dsh-translate 的浮层：头部 / 发丝线 / 正文 / 底栏）。"""
        import tkinter as tk

        from . import ui_kit  # noqa: PLC0415

        th = ui_kit.theme(self.theme_name)
        width = self.width
        pad = 14
        inner = width - pad * 2 - self._shadow * 2

        wrap = max(180, inner - 8)
        chars_per_line = max(8, int(wrap / 13.5))
        lines = max(1, -(-len(text) // chars_per_line))     # 向上取整
        lines = min(lines, 4)
        title_h = 20
        body_h = lines * 21
        footer_h = 34
        height = self._shadow * 2 + 10 + title_h + 8 + body_h + 6 + footer_h + 8

        win = tk.Toplevel(root)
        win.title(title)
        win.resizable(False, False)
        win.configure(bg=ui_kit.TRANSPARENT)
        ui_kit.style_popup(win, alpha=0.98, topmost=True)

        canvas = tk.Canvas(win, width=width, height=height, bg=ui_kit.TRANSPARENT,
                          highlightthickness=0, bd=0)
        canvas.pack()
        s = self._shadow
        ui_kit.draw_card(canvas, width, height, th, shadow=s, inset=s)

        x0 = s + pad
        y = s + 12
        # 头部：小圆点 + 标题（右边一个 ×）
        canvas.create_oval(x0, y + 6, x0 + 7, y + 13, fill=th["accent"], outline="")
        canvas.create_text(x0 + 13, y + 9, text=title, anchor="w",
                           fill=th["fg"], font=(ui_kit.FONT, ui_kit.SIZE_TITLE, "bold"))
        close_id = canvas.create_text(width - s - pad - 4, y + 9, text="×", anchor="e",
                                      fill=th["fg_mute"],
                                      font=(ui_kit.FONT, 13))
        y += title_h + 2
        ui_kit.draw_hairline(canvas, x0 - 4, width - s - pad + 4, y, th)
        y += 8

        canvas.create_text(x0, y, text=ui_kit.ellipsize(text, chars_per_line * lines),
                           anchor="nw", width=wrap, justify="left",
                           fill=th["fg"], font=(ui_kit.FONT, ui_kit.SIZE_BODY))
        y += body_h + 4
        ui_kit.draw_hairline(canvas, x0 - 4, width - s - pad + 4, y, th)

        def close(_event=None):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

        btn_w, btn_h = 74, 26
        ui_kit.CanvasButton(
            canvas, th, x=width - s - pad - btn_w, y=y + 3, width=btn_w, height=btn_h,
            text="知道了", command=close, primary=True, font_size=ui_kit.SIZE_TITLE,
        )
        canvas.create_text(x0, y + 3 + btn_h / 2, text="提醒", anchor="w",
                           fill=th["fg_mute"], font=(ui_kit.FONT, ui_kit.SIZE_META))
        for item in (close_id,):
            canvas.tag_bind(item, "<Button-1>", close)
            canvas.tag_bind(item, "<Enter>",
                            lambda _e: canvas.itemconfigure(close_id, fill=th["fg"]))
            canvas.tag_bind(item, "<Leave>",
                            lambda _e: canvas.itemconfigure(close_id, fill=th["fg_mute"]))

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
