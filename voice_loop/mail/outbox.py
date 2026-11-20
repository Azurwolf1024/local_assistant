"""发信审批：**没有人工批准，一个字都发不出去**。

状态机
    ``pending``  草稿已生成，等你看（弹窗 / 控制台 / CLI）
    ``approved`` 你点了「发送」→ 签发一次性 ``confirm_token``（绑定正文 hash，5 分钟过期）
    ``sent``     真发出去了（记下时间；重复调用只会返回同一结果，不会发第二封）
    ``failed``   明确失败（Graph 给了错误码）→ 可以用同一个 token 重试
    ``unknown``  网络断在半路（★不知道发没发出去★）→ 不允许自动重试，
                 由你决定「当作已发」还是「重新发」，避免重复发信
    ``rejected`` 你点了「取消」

四道闸门（全在 :meth:`Outbox.send` 里，任何一道不过就抛错/返回失败）
    1. ``[mail] allow_send`` 必须是 true（默认 false）
    2. 所有收件人都得在 ``[mail] send_allowlist`` 里（默认空 = 谁都发不出去）
    3. ``state`` 必须是 ``approved``（不能用 pending 直接发）
    4. ``confirm_token`` 必须对得上：HMAC(草稿 id + 正文 hash + 过期时间)
       —— 这一条专门防「你批准了 A 版本，agent 偷偷改成 B 版本再发」

为什么 token 由**审批卡**签发而不是模型
    模型能拿到的东西，就可能被它自己伪造/复用。token 只在用户点「发送」的那一刻由
    本进程生成，模型只会在事后被告知「已发出」。``mail_send(draft_id, confirm_token)``
    这个工具因此主要给控制台 / 命令行 / 自测用；正常语音流程里模型根本不需要它。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_SENT = "sent"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"
STATE_REJECTED = "rejected"


def body_hash(to: list[str], cc: list[str], subject: str, body: str) -> str:
    payload = json.dumps(
        {"to": [a.lower() for a in to], "cc": [a.lower() for a in cc],
         "subject": subject, "body": body},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def domain_of(address: str) -> str:
    addr = str(address or "").lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def allowed_by_allowlist(address: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    """``addr`` / ``@domain`` 两种写法；空表 = 谁都不许。"""
    addr = str(address or "").strip().lower()
    if not addr:
        return False
    domain = domain_of(addr)
    for item in allowlist or []:
        rule = str(item or "").strip().lower()
        if not rule:
            continue
        if rule.startswith("@"):
            if domain == rule[1:]:
                return True
        elif rule in (addr, domain):
            return True
    return False


class Outbox:
    def __init__(
        self,
        store: Any,
        cfg: Any,
        *,
        graph: Any = None,
        log: logging.Logger | None = None,
        secret: bytes | None = None,
        audit_path: str | Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.graph = graph
        self.log = log or logging.getLogger("voice_loop.mail")
        self.clock = clock or time.time
        self.secret = secret or os.urandom(32)
        self.audit_path = Path(audit_path) if audit_path else None

    # ---------------------------------------------------------------- 配置面
    @property
    def allow_send(self) -> bool:
        return bool(getattr(self.cfg, "allow_send", False))

    @property
    def allowlist(self) -> list[str]:
        return [str(x) for x in (getattr(self.cfg, "send_allowlist", []) or [])]

    @property
    def approval_ttl(self) -> float:
        return max(30.0, float(getattr(self.cfg, "approval_ttl", 300) or 300))

    # ---------------------------------------------------------------- 草稿
    def create(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: str = "",
        create_graph_draft: bool = True,
    ) -> dict[str, Any]:
        """建一封待批草稿。★不会发送★（连草稿在 Outlook 里都是草稿）。"""
        to = [a.strip().lower() for a in (to or []) if a and a.strip()]
        cc = [a.strip().lower() for a in (cc or []) if a and a.strip()]
        if not to:
            raise ValueError("至少给一个收件人")
        subject = str(subject or "").strip()[:250]
        body = str(body or "").strip()
        if not body:
            raise ValueError("正文是空的")
        draft_id = f"draft-{uuid.uuid4().hex[:12]}"
        graph_draft = ""
        if create_graph_draft and self.graph is not None:
            try:
                got = self.graph.create_draft(to=to, subject=subject, body_text=body,
                                              cc=cc, reply_to=reply_to)
                graph_draft = str(got.get("id") or "")
            except Exception as exc:  # noqa: BLE001 - 建不了 Graph 草稿也能先审
                self.log.warning(f"[mail] 建 Graph 草稿失败（审批后发送时再建）：{exc}")
        draft = {
            "id": draft_id,
            "created_at": self.clock(),
            "to": to,
            "cc": cc,
            "subject": subject,
            "body": body,
            "reply_to": reply_to,
            "graph_draft_id": graph_draft,
            "state": STATE_PENDING,
            "body_hash": body_hash(to, cc, subject, body),
            "expires_at": 0.0,
        }
        self.store.create_draft(draft)
        self.log.info(f"[mail] 待批草稿 {draft_id} → {', '.join(to)}：「{subject[:30]}」")
        return draft

    def pending(self) -> list[dict]:
        return self.store.drafts(states=(STATE_PENDING, STATE_APPROVED))

    def get(self, draft_id: str) -> dict | None:
        return self.store.get_draft(draft_id)

    # ---------------------------------------------------------------- 审批
    def precheck(self, draft: dict) -> tuple[bool, str]:
        """审批前的四道闸门里能提前判的两道（配置 + 白名单）。"""
        if not self.allow_send:
            return False, "发信功能没开（config.toml 的 [mail] allow_send = true 才能启用）"
        if not self.allowlist:
            return False, ("还没有收件人白名单。先在 config.toml 的 [mail] send_allowlist 里"
                           "写上允许发送的地址（例如 \"@example.com\"），空表 = 谁都发不出去")
        for addr in [*draft.get("to", []), *draft.get("cc", [])]:
            if not allowed_by_allowlist(addr, self.allowlist):
                return False, f"收件人 {addr} 不在白名单里（[mail] send_allowlist）"
        return True, ""

    def approve(self, draft_id: str, *, by: str = "user") -> tuple[bool, str, str]:
        """用户点了「发送」：签发一次性 token。返回 ``(ok, token, 说明)``。"""
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return False, "", "找不到这封草稿"
        ok, why = self.precheck(draft)
        if not ok:
            return False, "", why
        if draft.get("state") == STATE_SENT:
            return False, "", "这封已经发出去了"
        expires = self.clock() + self.approval_ttl
        token = self._sign(draft_id, str(draft.get("body_hash") or ""), expires)
        self.store.set_draft(
            draft_id, state=STATE_APPROVED, approved_at=self.clock(),
            approved_by=str(by or "user"), expires_at=expires,
        )
        self.log.info(f"[mail] 草稿 {draft_id} 已批准（{by}），token 有效 {int(self.approval_ttl)}s")
        return True, token, f"已批准「{draft.get('subject', '')[:20]}」，{int(self.approval_ttl)} 秒内有效"

    def reject(self, draft_id: str, why: str = "") -> tuple[bool, str]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return False, "找不到这封草稿"
        if draft.get("state") == STATE_SENT:
            return False, "已经发出去了，撤不回来"
        self.store.set_draft(draft_id, state=STATE_REJECTED, error=str(why or ""))
        self._audit({"event": "rejected", "draft_id": draft_id, "why": str(why or "")})
        return True, "已取消"

    def expire_overdue(self) -> int:
        """把超过审批时限还没点的草稿标记过期（★超时 = 不发★）。"""
        now = self.clock()
        n = 0
        for draft in self.store.drafts(states=(STATE_APPROVED,)):
            if float(draft.get("expires_at") or 0) and float(draft["expires_at"]) < now:
                self.store.set_draft(draft["id"], state=STATE_PENDING, expires_at=0.0)
                n += 1
        return n

    # ---------------------------------------------------------------- 签名
    def _sign(self, draft_id: str, hash_hex: str, expires: float) -> str:
        payload = f"{draft_id}|{hash_hex}|{int(expires)}".encode("utf-8")
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()[:40]

    def verify(self, draft: dict, token: str) -> tuple[bool, str]:
        expires = float(draft.get("expires_at") or 0)
        if not expires:
            return False, "这封还没被批准"
        if self.clock() > expires:
            return False, "批准已经过期（重新点一次发送）"
        expected = self._sign(str(draft.get("id")), str(draft.get("body_hash") or ""), expires)
        if not hmac.compare_digest(expected, str(token or "")):
            return False, "确认码对不上（草稿内容可能被改过，或 token 不是这次批准的）"
        current = body_hash(draft.get("to", []), draft.get("cc", []),
                            str(draft.get("subject") or ""), str(draft.get("body") or ""))
        if current != str(draft.get("body_hash") or ""):
            return False, "★草稿正文在批准后被改动过，拒绝发送★"
        return True, ""

    # ---------------------------------------------------------------- 发送
    def send(self, draft_id: str, token: str = "", *, by: str = "user") -> tuple[bool, dict]:
        """真正发送。返回 ``(ok, {state, message_id, error})``。"""
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return False, {"state": "", "error": "找不到这封草稿"}

        state = str(draft.get("state") or "")
        if state == STATE_SENT:
            return True, {"state": STATE_SENT, "message_id": draft.get("graph_draft_id", ""),
                          "error": "", "already": True}
        if state == STATE_UNKNOWN:
            return False, {"state": state, "error": "上次发送结果不确定（可能已发出），请人工确认后再决定"}
        if not self.allow_send:
            return False, {"state": state, "error": "发信功能没开（[mail] allow_send）"}
        ok, why = self.precheck(draft)
        if not ok:
            return False, {"state": state, "error": why}
        if state not in (STATE_APPROVED, STATE_FAILED):
            # failed 允许用同一个 token 再试（明确失败 = 肯定没发出去）
            return False, {"state": state, "error": "还没批准（要人工点一次发送）"}
        ok, why = self.verify(draft, token)
        if not ok:
            return False, {"state": state, "error": why}
        if self.graph is None:
            return False, {"state": state, "error": "没有可用的 Graph 客户端"}

        graph_draft = str(draft.get("graph_draft_id") or "")
        try:
            if not graph_draft:
                got = self.graph.create_draft(
                    to=list(draft.get("to") or []), subject=str(draft.get("subject") or ""),
                    body_text=str(draft.get("body") or ""), cc=list(draft.get("cc") or []),
                    reply_to=str(draft.get("reply_to") or ""),
                )
                graph_draft = str(got.get("id") or "")
                self.store.set_draft(draft_id, graph_draft_id=graph_draft)
            self.graph.send_draft(graph_draft)
        except Exception as exc:  # noqa: BLE001
            definite = bool(getattr(exc, "status", 0))     # GraphError 带 HTTP 状态 = 明确失败
            state = STATE_FAILED if definite else STATE_UNKNOWN
            self.store.set_draft(draft_id, state=state, error=str(exc)[:300])
            self.log.warning(f"[mail] 发送失败（{state}）：{exc}")
            self._audit({"event": "send_failed", "draft_id": draft_id, "state": state,
                         "error": str(exc)[:300]})
            return False, {"state": state, "message_id": graph_draft, "error": str(exc)}

        now = self.clock()
        self.store.set_draft(draft_id, state=STATE_SENT, sent_at=now, error="")
        self._audit({
            "event": "sent", "draft_id": draft_id, "to": draft.get("to"),
            "cc": draft.get("cc"), "subject": draft.get("subject"),
            "body_hash": draft.get("body_hash"), "approved_at": draft.get("approved_at"),
            "approved_by": draft.get("approved_by") or by, "sent_at": now,
        })
        self.log.info(f"[mail] 已发送 {draft_id} → {', '.join(draft.get('to') or [])}")
        return True, {"state": STATE_SENT, "message_id": graph_draft, "error": ""}

    # ---------------------------------------------------------------- 审计
    def _audit(self, payload: dict) -> None:
        if self.audit_path is None:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **payload}
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001 - 审计写不了也不能让发信流程崩
            self.log.warning(f"[mail] 审计写不进去：{exc}")

    # ---------------------------------------------------------------- 展示
    def preview(self, draft: dict, *, chars: int = 300) -> dict[str, str]:
        """给审批卡 / 控制台用的四行预览。"""
        body = str(draft.get("body") or "")
        return {
            "id": str(draft.get("id") or ""),
            "to": ", ".join(draft.get("to") or []),
            "cc": ", ".join(draft.get("cc") or []),
            "subject": str(draft.get("subject") or ""),
            "body": body[:chars] + ("…" if len(body) > chars else ""),
        }
