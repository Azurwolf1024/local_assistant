"""右下角审批卡：**发信之前，人得看一眼**。

两条线分开写，方便自测：
    - :class:`ApprovalQueue` —— 纯逻辑：排队、批准（签发 token 并发送）、拒绝、超时。
      不碰 Tk，所以离线测试能把它跑遍。
    - :class:`ApprovalCard` —— 只负责把队首那张画出来（复用 ``voice_loop/ui.py`` 的
      单例 Tk 宿主，和字幕/提醒同一套线程规则）。

交互（★一次只批一封★，避免弹窗叠罗汉）
    ┌ 要发一封邮件 ──────────────────────────────┐
    │ 收件人：prof@x.edu.cn                      │
    │ 主题：关于周报                              │
    │ ─────────────────────────────────────      │
    │ 老师您好…（前 300 字）      30 秒后自动取消  │
    │            [ 发送 ]  [ 取消 ]               │
    └────────────────────────────────────────────┘

「改一改」按你说的没做成按钮：卡片上直接提示「要改就说『把正文改短一点』」——
在语音场景里比多一个按钮更顺手，也避免半吊子的就地编辑器。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

#: 卡片自动消失（= 不发送）的秒数
DEFAULT_TIMEOUT = 60.0


class ApprovalQueue:
    """待批队列 + 批准动作（纯逻辑，可在没有图形环境时使用）。"""

    def __init__(
        self,
        outbox: Any,
        *,
        log: logging.Logger | None = None,
        on_result: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self.log = log or logging.getLogger("voice_loop.mail")
        self.on_result = on_result
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self.current: str = ""

    # ---------------------------------------------------------------- 队列
    def submit(self, draft_id: str) -> None:
        with self._lock:
            if draft_id and draft_id not in self._queue:
                self._queue.append(draft_id)

    def pending(self) -> list[str]:
        with self._lock:
            return list(self._queue)

    def head(self) -> dict | None:
        with self._lock:
            if not self._queue:
                return None
            draft_id = self._queue[0]
        draft = self.outbox.get(draft_id)
        if draft is None:
            self._drop(draft_id)
            return None
        return draft

    def _drop(self, draft_id: str) -> None:
        with self._lock:
            if draft_id in self._queue:
                self._queue.remove(draft_id)

    # ---------------------------------------------------------------- 决定
    def decide(self, draft_id: str, action: str) -> tuple[bool, str]:
        """``action`` = ``send`` / ``cancel``。返回 ``(ok, 给人看的一句话)``。

        ★批准与发送都在这里、都在用户点按钮之后★ —— 模型没有这条路径。
        """
        if action == "cancel":
            ok, msg = self.outbox.reject(draft_id, "用户取消")
            self._drop(draft_id)
            self._report(ok, "已取消" if ok else msg)
            return ok, "已取消" if ok else msg
        if action != "send":
            return False, f"不认识的动作：{action}"

        ok, token, msg = self.outbox.approve(draft_id)
        if not ok:
            self._drop(draft_id)
            self._report(False, msg)
            return False, msg
        ok2, info = self.outbox.send(draft_id, token, by="user")
        if ok2:
            self._drop(draft_id)
            self._report(True, "邮件已发出")
            return True, "邮件已发出"
        state = str(info.get("state") or "")
        if state == "unknown":
            # 网络断在半路：不重试，交给用户决定（防重复发信）
            self._drop(draft_id)
            self._report(False, "发送结果不确定（可能已发出），请到 Outlook 的「已发送」里确认")
            return False, "发送结果不确定，请到 Outlook 的「已发送」里确认"
        # 明确失败：留在队列里，可以再点一次
        self._report(False, str(info.get("error") or "发送失败"))
        return False, str(info.get("error") or "发送失败")

    def _report(self, ok: bool, message: str) -> None:
        (self.log.info if ok else self.log.warning)(f"[mail] 审批：{message}")
        if self.on_result is not None:
            try:
                self.on_result(ok, message)
            except Exception:  # noqa: BLE001 - 回调失败不影响结果
                pass


class ApprovalCard:
    """把队首那张画在右下角（邮件草稿 / 日程提议共用，只换文案）。

    三条踩过的坑（都在自测里钉住了）：
    1. ★正文不能溢出到页脚★：以前按「字数 / 13.5px」估行数，Windows 200% 缩放时
       一行其实更宽，正文直接压到按钮上（用户报「显示不全、也没法翻动」）。
       现在用 ``tkfont.measure`` 自己折行，**只画视口里的那几行**，滚轮翻页。
    2. ★点按钮不能没反应★：整张卡用一个 Canvas 画，按钮做成「画布级命中检测」
       （``<Button-1>`` 换算坐标后判断落在哪个按钮框里），不依赖 item 绑定的
       精确命中 —— 之前 ``_decide`` 被改丢时点了就抛 AttributeError，看起来像"点了没用"。
    3. ★发送要二次确认★：第一次点「发送」只把按钮变成「确认发送」（8 秒内有效），
       再点一次才真的发；避免手滑把邮件发出去。
    """

    VIEWPORT_LINES = 6          # 正文区最多显示几行（多出来的滚轮翻）
    CONFIRM_SECONDS = 8.0       # 「确认发送」等第二次点击的时间

    def __init__(
        self,
        queue: Any,
        *,
        host: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        width: int = 460,
        log: logging.Logger | None = None,
        on_empty: Callable[[], None] | None = None,
        title: str = "",
        ok_text: str = "",
        no_text: str = "",
        hint: str = "",
        theme: str = "auto",
    ) -> None:
        self.queue = queue
        self.theme_name = str(theme or "auto")
        is_event = str(getattr(queue, "kind", "mail")) == "event"
        self.title = title or ("要不要加进日程？" if is_event else "要发一封邮件")
        self.ok_text = ok_text or ("加入" if is_event else "发送")
        self.no_text = no_text or ("忽略" if is_event else "取消")
        self.hint = hint or ("想改时间就说「改成…」" if is_event else "要改就说「把这封改成…」")
        self.who_label = "时间" if is_event else "收件人"
        self._host = host
        self.timeout = max(10.0, float(timeout))
        self.width = max(320, int(width))
        self.log = log or logging.getLogger("voice_loop.mail")
        self.on_empty = on_empty

        self._win: Any = None
        self._canvas: Any = None
        self._shown: str = ""
        self._deadline = 0.0
        self._result: tuple[bool, str] | None = None
        self._busy = False
        self._hint_text = ""            # 失败原因之类，写在页脚
        self._scroll = 0                # 正文滚动位置（行）
        self._lines: list[str] = []
        self._viewport = self.VIEWPORT_LINES
        self._confirm_until = 0.0       # >now 表示"等第二次点击"
        self._draft: dict | None = None
        self._buttons: list[tuple[float, float, float, float, str]] = []

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        if self._host is not None:
            return True
        try:
            from .. import ui  # noqa: PLC0415

            host = ui.host(self.log)
            if not host.start():
                return False
            self._host = host
            host.add_tick(self._tick)
            return True
        except Exception as exc:  # noqa: BLE001 - 没图形环境就退化成没有卡片
            self.log.warning(f"[mail] 审批卡起不来（{exc}）")
            return False

    def stop(self) -> None:
        host, self._host = self._host, None
        if host is not None:
            host.remove_tick(self._tick)
            host.post(self._destroy)

    def submit(self, draft_id: str) -> None:
        """收到一封待批草稿 / 日程提议。"""
        self.queue.submit(draft_id)
        if self._host is None:
            self.start()

    # ---------------------------------------------------------------- UI 线程
    def _destroy(self, _root=None) -> None:
        win, self._win = self._win, None
        self._canvas = None
        self._shown = ""
        self._buttons = []
        self._confirm_until = 0.0
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _tick(self, root) -> None:  # pragma: no cover - 需要图形环境
        if self._host is None:
            return
        if self._result is not None:
            ok, message = self._result
            self._result = None
            self.log.info(f"[mail] 审批结果：{message}")
            if ok:
                self._destroy()
            else:
                # ★失败把窗口留着★：写红字原因、重新计时，让人可以再点一次
                self._hint_text = message
                self._confirm_until = 0.0
                self._deadline = time.monotonic() + self.timeout
                self._refresh(root)

        if self._confirm_until and time.monotonic() > self._confirm_until:
            self._confirm_until = 0.0          # 二次确认超时 → 退回「发送」
            self._refresh(root)

        draft = self.queue.head()
        if draft is None:
            if self._win is not None:
                self._destroy()
                if self.on_empty is not None:
                    self.on_empty()
            return

        draft_id = str(draft.get("id") or "")
        if self._win is None or self._shown != draft_id:
            self._destroy()
            self._draft = draft
            self._shown = draft_id
            self._hint_text = ""
            self._scroll = 0
            self._confirm_until = 0.0
            self._deadline = time.monotonic() + self.timeout
            self._build(root, draft)
        else:
            left = self._deadline - time.monotonic()
            if left <= 0 and not self._busy:
                # 超时 = 不发送（草稿留在队列里，下次还能批）
                self.queue.decide(draft_id, "cancel")
                self._destroy()
                return
            self._set_countdown(int(max(0, left)))
        self._place(root)

    # ---------------------------------------------------------------- 画
    def _theme(self) -> Any:
        from .. import ui_kit  # noqa: PLC0415

        return ui_kit.theme(self.theme_name)

    def _wrap(self, text: str, font: Any, max_px: int) -> list[str]:
        """按像素宽度折行（和字幕同一套做法，中文才不会整段挤成一行）。"""
        out: list[str] = []
        for para in str(text or "").split("\n"):
            cur = ""
            for ch in para:
                if not cur or font.measure(cur + ch) <= max_px:
                    cur += ch
                else:
                    out.append(cur)
                    cur = ch
            out.append(cur)
        return out or [""]

    def _build(self, root, draft: dict) -> Any:  # pragma: no cover - 需要图形环境
        import tkinter as tk
        from tkinter import font as tkfont

        from .. import ui_kit  # noqa: PLC0415

        th = self._theme()
        previewer = getattr(self.queue, "preview", None)
        preview = previewer(draft) if callable(previewer) else self.queue.outbox.preview(draft)

        pad, s = 16, 3
        body_font = tkfont.Font(root=root, family=ui_kit.FONT, size=ui_kit.SIZE_BODY)
        line_h = max(18, body_font.metrics("linespace") + 4)
        inner = self.width - pad * 2 - s * 2 - 6
        self._lines = self._wrap(preview["body"], body_font, inner)
        self._viewport = self.VIEWPORT_LINES
        self._line_h = line_h
        self._inner = inner

        height = (s * 2 + 12 + 20 + 10                 # 头
                  + 22 + 22                            # 收件人 / 主题
                  + 6 + self._viewport * line_h + 8    # 正文视口（固定高度）
                  + 30 + 14)                           # 页脚
        win = tk.Toplevel(root)
        win.title(self.title)
        win.resizable(False, False)
        win.configure(bg=ui_kit.TRANSPARENT)
        ui_kit.style_popup(win, alpha=0.99, topmost=True)
        canvas = tk.Canvas(win, width=self.width, height=height, bg=ui_kit.TRANSPARENT,
                           highlightthickness=0, bd=0)
        canvas.pack()
        self._height = height
        self._win, self._canvas = win, canvas
        # 滚轮：鼠标在窗口上就能翻正文（Windows 的 <MouseWheel> 是 ±120 的倍数）
        canvas.bind_all("<MouseWheel>", self._on_wheel)
        win.bind("<Escape>", lambda _e: self._decide("cancel"))
        canvas.bind("<Button-1>", self._on_click)
        canvas.configure(cursor="")
        self._refresh(root)
        return win

    def _refresh(self, root=None) -> None:  # pragma: no cover - 需要图形环境
        """整张重画（滚动 / 二次确认 / 报错都走这里，状态永远一致）。"""
        from .. import ui_kit  # noqa: PLC0415

        canvas = self._canvas
        if canvas is None:
            return
        th = self._theme()
        pad, s = 16, 3
        width, height = self.width, self._height
        canvas.delete("all")
        self._buttons = []
        ui_kit.draw_card(canvas, width, height, th, shadow=s, inset=s)

        x0, x1 = s + pad, width - s - pad
        y = s + 14
        canvas.create_oval(x0, y, x0 + 7, y + 7, fill=th["accent"], outline="")
        canvas.create_text(x0 + 13, y + 3, text=self.title, anchor="w", fill=th["fg"],
                           font=(ui_kit.FONT, ui_kit.SIZE_TITLE, "bold"))
        canvas.create_text(x1 - 2, y + 3, text="×", anchor="e", fill=th["fg_mute"],
                           font=(ui_kit.FONT, 13), tags=("close",))
        y += 18
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)
        y += 10

        preview = self._preview_of_current()
        canvas.create_text(x0, y, text=self.who_label, anchor="w", fill=th["fg_mute"],
                           font=(ui_kit.FONT, ui_kit.SIZE_META))
        canvas.create_text(x0 + 42, y, text=ui_kit.ellipsize(preview.get("to", ""), 40),
                           anchor="w", fill=th["fg"], font=(ui_kit.FONT, ui_kit.SIZE_BODY))
        y += 22
        canvas.create_text(x0, y, text="主题", anchor="w", fill=th["fg_mute"],
                           font=(ui_kit.FONT, ui_kit.SIZE_META))
        canvas.create_text(x0 + 42, y, text=ui_kit.ellipsize(preview.get("subject", ""), 34),
                           anchor="w", fill=th["fg"],
                           font=(ui_kit.FONT, ui_kit.SIZE_BODY, "bold"))
        y += 20
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)
        y += 6

        # ---- 正文视口：只画滚到的那几行 ----
        total = len(self._lines)
        self._scroll = max(0, min(self._scroll, max(0, total - self._viewport)))
        shown = self._lines[self._scroll: self._scroll + self._viewport]
        canvas.create_text(x0, y, text="\n".join(shown), anchor="nw", fill=th["fg"],
                           font=(ui_kit.FONT, ui_kit.SIZE_BODY), justify="left")
        if total > self._viewport:
            # 右侧细滚动条 + 行数提示（让人知道还能滚）
            track_top, track_h = y, self._viewport * self._line_h
            canvas.create_line(x1 + 2, track_top, x1 + 2, track_top + track_h,
                               fill=th["border"], width=2)
            thumb_h = max(12, track_h * self._viewport / total)
            offset = (track_h - thumb_h) * self._scroll / max(1, total - self._viewport)
            canvas.create_line(x1 + 2, track_top + offset, x1 + 2, track_top + offset + thumb_h,
                               fill=th["fg_mute"], width=2)
            canvas.create_text(x0 - 2, y + track_h + 2,
                               text=f"滚轮看全文（{self._scroll + 1}-"
                                    f"{min(total, self._scroll + self._viewport)}/{total} 行）",
                               anchor="nw", fill=th["fg_mute"],
                               font=(ui_kit.FONT, ui_kit.SIZE_META))
        y += self._viewport * self._line_h + 8
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)

        # ---- 页脚：左侧提示 + 倒计时，右侧按钮 ----
        btn_h, gap = 26, 8
        confirming = time.monotonic() < self._confirm_until
        ok_label = "确认发送" if confirming else self.ok_text
        ok_w = max(74, 18 + 12 * len(ok_label))
        no_w = max(62, 16 + 12 * len(self.no_text))
        y_btn = y + 10
        self._buttons = [
            (x1 - ok_w, y_btn, x1, y_btn + btn_h, "send"),
            (x1 - ok_w - gap - no_w, y_btn, x1 - ok_w - gap, y_btn + btn_h, "cancel"),
        ]
        self._btn_labels = {"send": ok_label, "cancel": self.no_text}
        for bx0, by0, bx1, by1, kind in self._buttons:
            primary = kind == "send"
            fill = (th["danger"] if confirming else th["accent"]) if primary else th["bg_soft"]
            canvas.create_polygon(
                ui_kit.rounded_points(bx0, by0, bx1, by1, ui_kit.RADIUS_SM),
                smooth=True, splinesteps=20, fill=fill,
                outline="" if primary else th["border"], width=1,
            )
            canvas.create_text((bx0 + bx1) / 2, (by0 + by1) / 2,
                               text=self._btn_labels[kind],
                               fill=th["on_accent"] if primary else th["fg"],
                               font=(ui_kit.FONT, ui_kit.SIZE_TITLE, "bold" if primary else "normal"))
        hint_text = self._hint_text or (self.hint if not confirming else "再点一次才真的发出去")
        canvas.create_text(x0, y_btn + 4, text=ui_kit.ellipsize(hint_text, 26), anchor="nw",
                           fill=th["danger"] if self._hint_text else th["fg_mute"],
                           font=(ui_kit.FONT, ui_kit.SIZE_META))
        self._countdown_id = canvas.create_text(
            x0, y_btn + 19, text="", anchor="nw", fill=th["fg_mute"],
            font=(ui_kit.FONT, ui_kit.SIZE_META))
        self._set_countdown(int(max(0, self._deadline - time.monotonic())))

    def _preview_of_current(self) -> dict:
        draft = self._draft or {}
        previewer = getattr(self.queue, "preview", None)
        if callable(previewer):
            return previewer(draft)
        return self.queue.outbox.preview(draft)

    def _set_countdown(self, seconds: int) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        verb = "自动忽略" if str(getattr(self.queue, "kind", "")) == "event" else "自动取消"
        try:
            canvas.itemconfigure(self._countdown_id, text=f"{seconds} 秒后{verb}")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 交互
    def _on_wheel(self, event: Any) -> None:  # pragma: no cover - 需要图形环境
        if self._canvas is None:
            return
        total = len(self._lines)
        if total <= self._viewport:
            return
        step = max(1, self._viewport // 2)
        delta = -1 if getattr(event, "delta", 0) > 0 else 1
        self._scroll = max(0, min(total - self._viewport, self._scroll + delta * step))
        self._refresh()

    def _on_click(self, event: Any) -> None:  # pragma: no cover - 需要图形环境
        """画布级命中检测：算坐标落在哪个按钮里（比 item 绑定可靠）。"""
        x, y = int(getattr(event, "x", 0)), int(getattr(event, "y", 0))
        for bx0, by0, bx1, by1, kind in self._buttons:
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                self._click(kind)
                return
        # 右上角的 ×：大致区域，点着也当取消
        if self.width - 44 <= x <= self.width - 6 and 6 <= y <= 36:
            self._decide("cancel")

    def _click(self, kind: str) -> None:
        if kind == "cancel":
            self._decide("cancel")
            return
        # ★二次确认★：第一次只换文案，第二次才真发
        if time.monotonic() < self._confirm_until:
            self._confirm_until = 0.0
            self._decide("send")
            return
        self._confirm_until = time.monotonic() + self.CONFIRM_SECONDS
        self._refresh()

    def _decide(self, action: str) -> None:
        """按钮回调：网络动作挪到线程里，别冻住 UI。"""
        draft = self.queue.head()
        if draft is None or self._busy:
            return
        draft_id = str(draft.get("id") or "")
        self._busy = True

        def work() -> None:
            try:
                ok, message = self.queue.decide(draft_id, action)
                self._result = (ok, message)
            except Exception as exc:  # noqa: BLE001 - 回调里的异常不能把 UI 弄死
                self.log.warning(f"[mail] 审批动作失败：{exc}")
                self._result = (False, f"{type(exc).__name__}: {exc}")
            finally:
                self._busy = False

        threading.Thread(target=work, name="mail-approve", daemon=True).start()

    # ---------------------------------------------------------------- 位置
    def _place(self, root) -> None:  # pragma: no cover - 需要图形环境
        win = self._win
        if win is None:
            return
        try:
            win.update_idletasks()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = win.winfo_width(), win.winfo_height()
            win.geometry(f"{w}x{h}+{max(0, sw - w - 24)}+{max(0, sh - h - 60)}")
        except Exception:  # noqa: BLE001
            pass
