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
    """把 :class:`ApprovalQueue` 的队首画在右下角。"""

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
        self._canvas: Any = None
        self._hint_id: Any = None
        # 邮件草稿 / 日程提议共用这一张卡：只换文案，不换交互（``kind`` 由队列自己声明）
        is_event = str(getattr(queue, "kind", "mail")) == "event"
        self.title = title or ("要不要加进日程？" if is_event else "要发一封邮件")
        self.ok_text = ok_text or ("加入" if is_event else "发送")
        self.no_text = no_text or ("忽略" if is_event else "取消")
        self.hint = hint or ("想改时间就说「改成…」" if is_event else "要改就说「把这封改成…」")
        self.who_label = "时间" if is_event else "收件人"
        self._host = host
        self.timeout = max(10.0, float(timeout))
        self.width = max(280, int(width))
        self.log = log or logging.getLogger("voice_loop.mail")
        self.on_empty = on_empty
        self._win: Any = None
        self._shown: str = ""
        self._deadline = 0.0
        self._result: tuple[bool, str] | None = None
        self._busy = False

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
        """收到一封待批草稿（MCP 工具建完草稿后会调它）。"""
        self.queue.submit(draft_id)
        if self._host is None:
            self.start()

    # ---------------------------------------------------------------- UI 线程
    def _destroy(self, _root=None) -> None:
        win, self._win = self._win, None
        self._shown = ""
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _tick(self, root) -> None:  # pragma: no cover - 需要图形环境
        if self._host is None:
            return
        if self._result is not None:
            # 上一步的结果：成功才收窗；**失败把窗口留着**并把原因写在卡片上
            # （以前一律 destroy → 下一个 tick 又因为「队首还在」重建一次，
            #  看起来就是「点了按钮窗口反复弹出」，实测被用户抓到过）。
            ok, message = self._result
            self._result = None
            self.log.info(f"[mail] 审批结果：{message}")
            if ok:
                self._destroy()
            else:
                try:
                    th = ui_kit_theme(self.theme_name)
                    self._canvas.itemconfigure(self._hint_id, text=ui_kit_ellipsize(message, 30),
                                               fill=th["danger"])
                except Exception:  # noqa: BLE001
                    pass
                self._deadline = time.monotonic() + self.timeout

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
            self._win = self._build(root, draft)
            self._shown = draft_id
            self._deadline = time.monotonic() + self.timeout
        else:
            left = self._deadline - time.monotonic()
            if left <= 0 and not self._busy:
                # 超时 = 不发送（草稿留在队列里，下次还能批）
                self.queue.decide(draft_id, "cancel")
                self._destroy()
                return
            try:
                verb = "自动忽略" if str(getattr(self.queue, "kind", "")) == "event" else "自动取消"
                self._canvas.itemconfigure(
                    self._countdown, text=f"{int(max(0, left))} 秒后{verb}")
            except Exception:  # noqa: BLE001
                pass
        self._place(root)

    def _build(self, root, draft: dict) -> Any:  # pragma: no cover - 需要图形环境
        """画一张圆角审批卡（和提醒弹窗 / 字幕同一套设计件，参照 dsh-translate 浮层）。"""
        import tkinter as tk

        from .. import ui_kit  # noqa: PLC0415

        th = ui_kit.theme(self.theme_name)
        previewer = getattr(self.queue, "preview", None)
        preview = previewer(draft) if callable(previewer) else self.queue.outbox.preview(draft)

        pad = 16
        s = 3
        width = self.width
        inner = width - pad * 2 - s * 2
        body_chars = max(24, int(inner / 13.5))
        body_lines = min(6, max(1, -(-len(preview["body"]) // body_chars)))
        height = (s * 2 + 12 + 20 + 10        # 头
                  + 20 + 22                    # 收件人 / 主题
                  + 8 + body_lines * 20 + 8   # 正文
                  + 30 + 12)                   # 底栏

        win = tk.Toplevel(root)
        win.title(self.title)
        win.resizable(False, False)
        win.configure(bg=ui_kit.TRANSPARENT)
        ui_kit.style_popup(win, alpha=0.99, topmost=True)

        canvas = tk.Canvas(win, width=width, height=height, bg=ui_kit.TRANSPARENT,
                           highlightthickness=0, bd=0)
        canvas.pack()
        ui_kit.draw_card(canvas, width, height, th, shadow=s, inset=s)

        x0, x1 = s + pad, width - s - pad
        y = s + 14
        canvas.create_oval(x0, y, x0 + 7, y + 7, fill=th["accent"], outline="")
        canvas.create_text(x0 + 13, y + 3, text=self.title, anchor="w", fill=th["fg"],
                           font=(ui_kit.FONT, ui_kit.SIZE_TITLE, "bold"))
        cross = canvas.create_text(x1 - 2, y + 3, text="×", anchor="e",
                                   fill=th["fg_mute"], font=(ui_kit.FONT, 13))
        y += 18
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)
        y += 10

        canvas.create_text(x0, y, text=f"{self.who_label}", anchor="w",
                           fill=th["fg_mute"], font=(ui_kit.FONT, ui_kit.SIZE_META))
        canvas.create_text(x0 + 40, y, text=ui_kit.ellipsize(preview["to"], 46),
                           anchor="w", fill=th["fg"], font=(ui_kit.FONT, ui_kit.SIZE_BODY))
        y += 22
        canvas.create_text(x0, y, text="主题", anchor="w",
                           fill=th["fg_mute"], font=(ui_kit.FONT, ui_kit.SIZE_META))
        canvas.create_text(x0 + 40, y, text=ui_kit.ellipsize(preview["subject"], 40),
                           anchor="w", fill=th["fg"],
                           font=(ui_kit.FONT, ui_kit.SIZE_BODY, "bold"))
        y += 14
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)
        y += 8

        canvas.create_text(x0, y, text=ui_kit.ellipsize(preview["body"], body_chars * body_lines),
                           anchor="nw", width=inner, justify="left",
                           fill=th["fg"], font=(ui_kit.FONT, ui_kit.SIZE_BODY))
        y += body_lines * 20 + 8
        ui_kit.draw_hairline(canvas, x0 - 6, x1 + 6, y, th)

        def send() -> None:
            self._decide("send")

        def cancel() -> None:
            self._decide("cancel")

        btn_h, gap = 26, 8
        ok_w = max(70, 16 + 12 * len(self.ok_text))
        no_w = max(62, 16 + 12 * len(self.no_text))
        y_btn = y + 8
        ui_kit.CanvasButton(canvas, th, x=x1 - ok_w, y=y_btn, width=ok_w, height=btn_h,
                            text=self.ok_text, command=send, primary=True)
        ui_kit.CanvasButton(canvas, th, x=x1 - ok_w - gap - no_w, y=y_btn, width=no_w,
                            height=btn_h, text=self.no_text, command=cancel)

        self._hint_id = canvas.create_text(
            x0, y_btn + btn_h / 2,
            text=ui_kit.ellipsize(self.hint, 22), anchor="w",
            fill=th["fg_mute"], font=(ui_kit.FONT, ui_kit.SIZE_META),
        )
        self._countdown = canvas.create_text(
            x0, y_btn + btn_h / 2 + 13, text="", anchor="w",
            fill=th["fg_mute"], font=(ui_kit.FONT, ui_kit.SIZE_META),
        )
        self._canvas = canvas
        for item in (cross,):
            canvas.tag_bind(item, "<Button-1>", lambda _e: cancel())
            canvas.tag_bind(item, "<Enter>",
                            lambda _e: canvas.itemconfigure(cross, fill=th["fg"]))
            canvas.tag_bind(item, "<Leave>",
                            lambda _e: canvas.itemconfigure(cross, fill=th["fg_mute"]))
        win.bind("<Escape>", lambda _e: cancel())
        return win

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
