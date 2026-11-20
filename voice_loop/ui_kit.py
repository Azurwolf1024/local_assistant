"""UI 设计件：**一套 token + 几个画法**，给弹窗 / 字幕 / 审批卡共用。

参照物是 DeepSeek Harness 的翻译浮层（`dsh-translate`）：
    - 12px 圆角卡片 + 1px 细描边 + 多层柔和投影（不是一块直角黑板）
    - 头部：小标题左、关闭 × 右，下面一条发丝分割线
    - 正文分层：次要信息（来源原文）用灰色小字，主要信息（译文/结论）用亮色大字
    - 底栏：左边 11px 灰色元信息，右边「纯文字按钮 + 淡填充按钮」
    - 点缀色 blue-500 #3b82f6 / 悬停 blue-600 #2563eb

Tk 的限制与对策
    - ``Frame`` 画不出圆角 → 一律用 ``Canvas`` 自己画（:func:`rounded_points`）
    - Tk 没有阴影模糊 → 用「多层同色圆角矩形 + 窗口透明色」伪造柔和边
      （``-transparentcolor`` 让圆角外面是真的透明，不是一块黑底）
    - 没有 hover 态 → Canvas 上绑 ``<Enter>``/``<Leave>`` 自己换填充色

主题
    ``[ui] theme = "auto" | "dark" | "light"``；auto 读 Windows 的
    ``AppsUseLightTheme``（跟随系统深/浅色）。
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# 颜色 / 尺寸 token（深色、浅色两套，取自 dsh-translate 的实际取值）
# --------------------------------------------------------------------------- #
DARK = {
    "bg": "#1f2023",          # 卡片底
    "bg_soft": "#26272b",     # 次要区块 / 按钮底
    "border": "#3a3b40",
    "fg": "#f2f4f7",          # 主要文字
    "fg_mute": "#9aa0a6",     # 次要文字（原文、元信息）
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "on_accent": "#ffffff",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "shadow": "#101114",
}
LIGHT = {
    "bg": "#ffffff",
    "bg_soft": "#f4f5f7",
    "border": "#e3e5e8",
    "fg": "#1b1c1e",
    "fg_mute": "#8b9096",
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "on_accent": "#ffffff",
    "ok": "#16a34a",
    "warn": "#d97706",
    "danger": "#dc2626",
    "shadow": "#d7d9dd",
}

#: 圆角统一 12px，小控件 8px —— 和参照物一致
RADIUS = 12
RADIUS_SM = 8
FONT = "Microsoft YaHei UI"
FONT_MONO = "Consolas"
SIZE_TITLE = 12
SIZE_BODY = 13
SIZE_META = 11

#: 透明色：给 ``-transparentcolor`` 用（随便挑一个不会被用到的颜色）
TRANSPARENT = "#010203"


def system_is_light() -> bool:
    """Windows 的「应用使用浅色主题」开关；读不到按深色处理。"""
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        value = ctypes.windll.winreg  # 只是确认在 Windows 上
        del value
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            raw, _ = winreg.QueryValueEx(handle, "AppsUseLightTheme")
        return bool(int(raw))
    except Exception:  # noqa: BLE001 - 非 Windows 或没这个键：默认深色
        return False


@dataclass(frozen=True)
class Theme:
    colors: dict[str, str]
    name: str

    def __getitem__(self, key: str) -> str:
        return self.colors.get(key, "#ff00ff")


def theme(choice: str = "auto") -> Theme:
    want = str(choice or "auto").strip().lower()
    if want == "light" or (want == "auto" and system_is_light()):
        return Theme(LIGHT, "light")
    return Theme(DARK, "dark")


# --------------------------------------------------------------------------- #
# 画法
# --------------------------------------------------------------------------- #
def rounded_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """圆角矩形的 polygon 顶点（Tk 的 ``create_polygon`` 用 smooth 就能圆弧化）。"""
    r = max(0.0, min(float(r), (x1 - x0) / 2, (y1 - y0) / 2))
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


def draw_card(
    canvas: Any,
    width: float,
    height: float,
    th: Theme,
    *,
    radius: float = RADIUS,
    fill: str | None = None,
    border: str | None = None,
    shadow: int = 3,
    inset: float = 0.0,
) -> None:
    """在 canvas 上画「阴影 + 卡片」。``inset`` 给阴影留边（窗口比卡片大一点）。"""
    fill = fill or th["bg"]
    border = border or th["border"]
    x0, y0 = inset, inset
    x1, y1 = width - inset, height - inset
    # 阴影：几层同色圆角依次放大 + 向下偏移，模拟柔和投影（Tk 没有真模糊）
    for i in range(shadow, 0, -1):
        pad = i * 1.6
        canvas.create_polygon(
            rounded_points(x0 - pad * 0.4, y0 + pad, x1 + pad * 0.4, y1 + pad, radius + pad * 0.5),
            smooth=True, splinesteps=24,
            fill=_mix(th["shadow"], fill, 1 - 0.22 / i), outline="",
        )
    canvas.create_polygon(
        rounded_points(x0, y0, x1, y1, radius), smooth=True, splinesteps=24,
        fill=fill, outline=border, width=1,
    )


def draw_hairline(canvas: Any, x0: float, x1: float, y: float, th: Theme) -> None:
    canvas.create_line(x0, y, x1, y, fill=th["border"], width=1)


def _mix(fg: str, bg: str, alpha: float) -> str:
    """把 ``fg`` 以 ``alpha`` 混到 ``bg`` 上（Tk 没有透明度，只能自己算颜色）。"""
    alpha = max(0.0, min(1.0, float(alpha)))
    try:
        f = _rgb(fg)
        b = _rgb(bg)
    except ValueError:
        return fg
    out = tuple(int(round(f[i] * alpha + b[i] * (1 - alpha))) for i in range(3))
    return "#%02x%02x%02x" % out


def _rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(value)
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 让窗口真的「圆角透明 + 无边框 + 置顶」
# --------------------------------------------------------------------------- #
def style_popup(win: Any, *, alpha: float = 1.0, topmost: bool = True,
                focusable: bool = False) -> bool:
    """把一个 Toplevel 变成「无边框卡片窗口」。返回是否支持透明色。"""
    try:
        win.overrideredirect(True)
    except Exception:  # noqa: BLE001
        pass
    try:
        if topmost:
            win.attributes("-topmost", True)
        if alpha < 1.0:
            win.attributes("-alpha", max(0.2, min(1.0, alpha)))
    except Exception:  # noqa: BLE001
        pass
    if not focusable:
        try:
            win.attributes("-disabled", False)
        except Exception:  # noqa: BLE001
            pass
    try:
        win.attributes("-transparentcolor", TRANSPARENT)
        return True
    except Exception:  # noqa: BLE001 - 个别 Tk 版本不支持：退化成不透明直角窗口
        logging.getLogger("voice_loop").debug("这个 Tk 不支持 -transparentcolor，圆角会是底色")
        return False


class CanvasButton:
    """Canvas 上的按钮：圆角底 + hover 变色 + 点击回调（Tk 的 Button 做不出圆角）。"""

    def __init__(
        self,
        canvas: Any,
        th: Theme,
        *,
        x: float,
        y: float,
        width: float,
        height: float,
        text: str,
        command: Any = None,
        primary: bool = False,
        font_size: int = SIZE_TITLE,
    ) -> None:
        self.canvas = canvas
        self.th = th
        self.primary = primary
        self.command = command
        self._box = (x, y, x + width, y + height)
        self._text = text
        self._hover = False
        self._bg = self._fill()
        self._shape = canvas.create_polygon(
            rounded_points(x, y, x + width, y + height, RADIUS_SM),
            smooth=True, splinesteps=20, fill=self._bg,
            outline="" if primary else th["border"], width=1,
        )
        self._label = canvas.create_text(
            x + width / 2, y + height / 2, text=text,
            fill=th["on_accent"] if primary else th["fg"],
            font=(FONT, font_size, "bold" if primary else "normal"),
        )
        for item in (self._shape, self._label):
            canvas.tag_bind(item, "<Enter>", lambda _e: self._set_hover(True))
            canvas.tag_bind(item, "<Leave>", lambda _e: self._set_hover(False))
            canvas.tag_bind(item, "<Button-1>", lambda _e: self._click())

    def _fill(self) -> str:
        if self.primary:
            return self.th["accent_hover"] if self._hover else self.th["accent"]
        return self.th["bg_soft"] if not self._hover else _mix(
            self.th["accent"], self.th["bg_soft"], 0.18)

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        try:
            self.canvas.itemconfigure(self._shape, fill=self._fill())
            self.canvas.configure(cursor="hand2" if on else "")
        except Exception:  # noqa: BLE001 - 窗口已经销毁
            pass

    def _click(self) -> None:
        if callable(self.command):
            self.command()

    def set_text(self, text: str) -> None:
        try:
            self.canvas.itemconfigure(self._label, text=text)
        except Exception:  # noqa: BLE001
            pass


def ellipsize(text: str, limit: int, *, tail: str = "…") -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + tail
