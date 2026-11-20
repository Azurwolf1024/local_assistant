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
        queue: ApprovalQueue,
        *,
        host: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        width: int = 460,
        log: logging.Logger | None = None,
        on_empty: Callable[[], None] | None = None,
    ) -> None:
        self.queue = queue
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
            # 把上一步的结果再说一遍（不弹新窗，只在卡片上显示一句）
            ok, message = self._result
            self._result = None
            if self._win is not None:
                try:
                    self._win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            self._win = None
            self._shown = ""
            self.log.info(f"[mail] 审批结果：{message}")

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
                self._countdown.config(text=f"{int(max(0, left))} 秒后自动取消")
            except Exception:  # noqa: BLE001
                pass
        self._place(root)

    def _build(self, root, draft: dict) -> Any:  # pragma: no cover - 需要图形环境
        import tkinter as tk

        preview = self.queue.outbox.preview(draft)
        win = tk.Toplevel(root)
        win.title("要发一封邮件")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg="#1b1f2a")

        tk.Label(win, text="要发一封邮件", bg="#1b1f2a", fg="#8fd3ff",
                 font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(
            fill="x", padx=14, pady=(10, 2))
        to_line = preview["to"] + (f"（抄送 {preview['cc']}）" if preview["cc"] else "")
        tk.Label(win, text=f"收件人：{to_line}", bg="#1b1f2a", fg="#f2f4f8",
                 font=("Microsoft YaHei UI", 10), anchor="w",
                 wraplength=self.width - 28, justify="left").pack(fill="x", padx=14)
        tk.Label(win, text=f"主题：{preview['subject']}", bg="#1b1f2a", fg="#f2f4f8",
                 font=("Microsoft YaHei UI", 10, "bold"), anchor="w",
                 wraplength=self.width - 28, justify="left").pack(fill="x", padx=14, pady=(2, 6))
        tk.Frame(win, bg="#2c3444", height=1).pack(fill="x", padx=14)
        tk.Label(win, text=preview["body"], bg="#1b1f2a", fg="#dfe4ee",
                 font=("Microsoft YaHei UI", 10), anchor="w", justify="left",
                 wraplength=self.width - 28).pack(fill="both", expand=True, padx=14, pady=(6, 4))
        self._countdown = tk.Label(win, text="", bg="#1b1f2a", fg="#8b93a7",
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self._countdown.pack(fill="x", padx=14)
        tk.Label(win, text="要改就说「把这封改成…」", bg="#1b1f2a", fg="#8b93a7",
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", padx=14)

        row = tk.Frame(win, bg="#1b1f2a")
        row.pack(fill="x", padx=14, pady=(4, 10))

        def send() -> None:
            self._decide("send")

        def cancel() -> None:
            self._decide("cancel")

        tk.Button(row, text="发送", command=send, relief="flat", bg="#2f6feb", fg="white",
                  activebackground="#1d5ad4", activeforeground="white",
                  font=("Microsoft YaHei UI", 10), padx=18, pady=2,
                  cursor="hand2").pack(side="right")
        tk.Button(row, text="取消", command=cancel, relief="flat", bg="#39404f", fg="#e6eaf2",
                  activebackground="#2b3240", activeforeground="white",
                  font=("Microsoft YaHei UI", 10), padx=16, pady=2,
                  cursor="hand2").pack(side="right", padx=(0, 8))
        win.bind("<Escape>", lambda _e: cancel())
        return win

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
            finally:
                self._busy = False

        threading.Thread(target=work, name="mail-approve", daemon=True).start()

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
