"""底部居中半透明字幕：把助手的回复显示在屏幕下方（任务栏正上方）。

为什么需要它：扬声器关了、静音了、或者戴着耳机走开了，语音助手就完全没法沟通。
字幕是一条常驻的「监视器」，让你用眼睛也能跟上对话，还能顺带确认它有没有听错你说的话。

实现要点
    - 窗口挂在 :mod:`voice_loop.ui` 的共享 UI 线程上（一个进程只能有一个 Tk 解释器，
      字幕和提醒小窗各起一个线程会直接抛 ``main thread is not in main loop``）
    - 无边框 + 半透明 + 置顶，但**点得穿**：
      它永远不会挡住你下面的操作，也不会抢焦点
    - 位置贴在桌面工作区底边上方居中（用 SPI_GETWORKAREA 取，自动避开任务栏，
      任务栏在左/右/上、或者屏幕有 DPI 缩放都能算对）
    - 流式追加时把渲染合并到 ~25 帧/秒，长回答超出 max_lines 就只显示末尾
    - 中文换行自己按像素宽度算（Tk 的 wraplength 对连续中文不可靠）
    - 说完 ``hold_seconds`` 秒自动隐藏；没有图形环境 / tkinter 不可用时静默降级
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

from . import ui

# --------------------------------------------------------------------------- #
# Win32：取工作区 + 让窗口点得穿
# --------------------------------------------------------------------------- #
_SPI_GETWORKAREA = 0x0030
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_GA_ROOT = 2

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


if _user32 is not None:  # pragma: no cover - 平台相关
    # 必须声明 argtypes：HWND 是指针大小，不声明会被 ctypes 截成 32 位
    _user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    _user32.SystemParametersInfoW.restype = wintypes.BOOL
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND


def work_area() -> tuple[int, int, int, int] | None:
    """桌面可用区域 (left, top, right, bottom)，已经排除任务栏。"""
    if _user32 is None:
        return None
    try:
        rect = _RECT()
        ok = _user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok and rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        pass
    return None


def make_click_through(hwnd: int) -> bool:
    """让窗口不接收鼠标事件、也不抢焦点。

    两个坑（都实测踩过）：
    1. Tk 的 ``winfo_id()`` 返回的是内部的 **TkChild 子窗口**，真正的顶层窗口
       是它的父窗口。样式必须加在根窗口上，否则 ``WindowFromPoint``
       照样会命中我们，等于没点穿。
    2. **千万不要 OR 进 ``WS_EX_LAYERED``。** Tk 用 ``-alpha`` 已经在根窗口上
       设好了这个位；硬加会让窗口整个变成全透明，肉眼看不见、截图也抓不到。
    """
    if _user32 is None:
        return False
    try:
        handle = wintypes.HWND(hwnd)
        root = _user32.GetAncestor(handle, _GA_ROOT) or handle
        for target in (root, handle):
            h = wintypes.HWND(target) if isinstance(target, int) else target
            style = _user32.GetWindowLongW(h, _GWL_EXSTYLE)
            want = style | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
            _user32.SetWindowLongW(h, _GWL_EXSTYLE, ctypes.c_long(want))
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 字幕窗
# --------------------------------------------------------------------------- #
class SubtitleOverlay:
    """一条常驻的字幕条：``show_user`` 显示「你说」，``update``/``append`` 显示助手回复。"""

    def __init__(
        self,
        enabled: bool = True,
        width: int = 920,
        alpha: float = 0.86,
        hold_seconds: float = 6.0,
        font_size: int = 20,
        max_lines: int = 4,
        show_user_text: bool = True,
        margin: int = 8,
        logger: logging.Logger | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.width = max(320, int(width))
        self.alpha = min(1.0, max(0.2, float(alpha)))
        self.hold_seconds = max(0.5, float(hold_seconds))
        self.font_size = max(10, int(font_size))
        self.max_lines = max(1, int(max_lines))
        self.show_user_text = bool(show_user_text)
        self.margin = max(0, int(margin))
        self.log = logger or logging.getLogger("voice_loop")

        self._queue: queue.Queue = queue.Queue()
        self._host: ui.UiHost | None = None
        self._closing = False

        # 以下都是 UI 线程里的状态
        self._win = None
        self._lbl_main = None
        self._lbl_user = None
        self._font_main = None
        self._area: tuple[int, int, int, int] | None = None
        self._win_width = self.width
        self._wrap_px = self.width
        self._last_geom = ""
        self._hwnd = 0
        self._user_text = ""
        self._body = ""
        self._dirty = False
        self._changed_at = 0.0
        self._visible = False
        self._last_render = 0.0

        # 配色
        self._bg = "#0f131a"
        self._fg = "#f4f7fb"
        self._fg_user = "#8ab4dc"
        self._border = "#2d3644"
        self._family = "Microsoft YaHei UI"

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._host is not None and not self._closing

    @property
    def failed(self) -> bool:
        """tkinter 不可用之类的硬失败，此时调用全都变成空操作。"""
        return self._host is None or self._host.failed

    @property
    def hwnd(self) -> int:
        """字幕窗口的句柄（0 表示还没建好），诊断用。"""
        return self._hwnd

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
            h.post(self._destroy)

    # ------------------------------------------------------------------ 外部接口
    # 下面这些方法可能从任意线程（识别线程 / 调度器线程）调用，所以只往队列里塞
    def show_user(self, text: str) -> None:
        """显示「你说：…」的那一行（用来确认有没有听错）。"""
        if not self.show_user_text:
            return
        self._put("user", text)

    def update(self, text: str) -> None:
        """直接设置助手回复的全文。"""
        self._put("text", text)

    def append(self, delta: str) -> None:
        """流式追加一段（LLM 边生成边显示）。"""
        self._put("delta", delta)

    def clear(self) -> None:
        self._put("clear", "")

    def _put(self, kind: str, payload: str) -> None:
        if not self.enabled or self._closing:
            return
        if self._host is None and not self.start():
            return
        if self._host is None or self._host.failed:
            return
        self._queue.put((kind, payload))

    # ------------------------------------------------------------------ UI 线程
    def _destroy(self, _root) -> None:
        win, self._win = self._win, None
        self._visible = False
        self._lbl_main = None
        self._lbl_user = None
        self._hwnd = 0
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _build(self, root) -> None:  # pragma: no cover - 需要图形环境
        import tkinter as tk
        from tkinter import font as tkfont

        # 内边距统一交给 grid，label 自己不设 padx/pady，折行宽度就好算
        pad = max(12, self.font_size // 2)
        pad_v = max(5, pad // 3)

        # 桌面可用区域（已排除任务栏）。任务栏在左/右/上、或者屏幕有 DPI 缩放，
        # 这里算出来的位置都是对的。
        area = work_area()
        if area is None:
            area = (
                0,
                0,
                root.winfo_screenwidth(),
                max(0, root.winfo_screenheight() - 48),
            )
        self._area = area
        self._win_width = min(self.width, max(320, area[2] - area[0]) - 2 * self.margin)
        self._wrap_px = self._win_width - 2 * pad - 6

        # 显式传 root：只有一个 Tk 解释器时最稳，也免得依赖 _default_root
        self._font_main = tkfont.Font(
            root=root, family=self._family, size=self.font_size
        )
        font_user = tkfont.Font(
            root=root, family=self._family, size=max(10, self.font_size - 6)
        )

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.configure(bg=self._bg)
        # 一圈细边框，浅色墙纸下也能看清面板边缘
        try:
            win.configure(highlightthickness=1, highlightbackground=self._border)
        except Exception:  # noqa: BLE001
            pass
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", self.alpha)
        except Exception:  # noqa: BLE001
            pass

        win.grid_columnconfigure(0, weight=1)
        self._lbl_user = tk.Label(
            win, text="", bg=self._bg, fg=self._fg_user, font=font_user,
            justify="left", anchor="w", wraplength=0,
        )
        self._lbl_main = tk.Label(
            win, text="", bg=self._bg, fg=self._fg, font=self._font_main,
            justify="left", anchor="w", wraplength=0,
        )
        # 固定行号：上行「你说」，下行助手回复。用 grid_remove/grid 显隐，
        # 不会像 pack 那样一加一减就把顺序搞乱。
        self._lbl_user.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad_v, 0))
        self._lbl_main.grid(row=1, column=0, sticky="ew", padx=pad, pady=(pad_v, pad_v))
        self._lbl_user.grid_remove()

        self._win = win
        self._hwnd = int(win.winfo_id())
        root.update_idletasks()
        make_click_through(self._hwnd)
        self.log.debug(f"字幕窗口已建立 hwnd={self._hwnd:#x} 宽度={self._win_width}px")

    def _tick(self, root) -> None:  # pragma: no cover - 需要图形环境
        if self._closing:
            return
        now = time.monotonic()
        # 渲染合并到 ~25 帧/秒：LLM 一秒能吐几十个 delta，不必每个都重排
        idle = self._queue.empty()
        if idle and now - self._last_render < 0.05:
            return
        self._last_render = now

        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "user":
                self._user_text = payload
            elif kind == "text":
                self._body = payload
            elif kind == "delta":
                self._body += payload
            elif kind == "clear":
                self._user_text = ""
                self._body = ""
            self._dirty = True
            self._changed_at = now

        if self._win is None:
            if not self._dirty and not self._body and not self._user_text:
                return
            self._build(root)

        if self._dirty:
            self._dirty = False
            if not self._body and not self._user_text:
                if self._visible:
                    self._win.withdraw()
                    self._visible = False
            else:
                lines = self._wrap(
                    self._body, self._font_main, self._wrap_px, self.max_lines
                )
                self._lbl_main.configure(text="\n".join(lines))
                if self._user_text and self.show_user_text:
                    self._lbl_user.configure(text=f"你说：{self._user_text}")
                    self._lbl_user.grid()
                else:
                    self._lbl_user.grid_remove()
                if not self._visible:
                    self._win.deiconify()
                    self._win.attributes("-topmost", True)
                    self._win.lift()
                    make_click_through(self._hwnd)
                    self._visible = True
                self._place(root)
        elif self._visible and (now - self._changed_at) > self.hold_seconds:
            self._win.withdraw()
            self._visible = False
            self.log.debug("字幕已自动隐藏")

    # ------------------------------------------------------------------ 窗口细节
    def _place(self, root) -> None:
        """贴在桌面工作区底边上方、水平居中。"""
        win = self._win
        if win is None:
            return
        try:
            win.update_idletasks()
            height = max(1, win.winfo_reqheight())
        except Exception:  # noqa: BLE001
            return
        area = self._area or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        left, _top, right, bottom = area
        width = self._win_width
        x = left + (max(320, right - left) - width) // 2
        y = bottom - height - self.margin
        geom = f"{width}x{height}+{max(0, x)}+{max(0, y)}"
        if geom == self._last_geom:
            return
        try:
            win.geometry(geom)
        except Exception:  # noqa: BLE001
            return
        self._last_geom = geom
        self.log.debug(f"字幕位置 {geom}")

    @staticmethod
    def _wrap(text: str, font, max_px: int, max_lines: int) -> list[str]:
        """按像素宽度手动折行（Tk 的 wraplength 对连续中文会不折行）。"""
        if not text:
            return []
        lines: list[str] = []
        cur = ""
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            if not cur or font.measure(cur + ch) <= max_px:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        if max_lines > 0 and len(lines) > max_lines:
            lines = lines[-max_lines:]
            lines[0] = "…" + lines[0]
        return lines
