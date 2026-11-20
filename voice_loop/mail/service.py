"""门面：把 auth / graph / store / delta / triage / outbox 拼成一件能用的事。

上层（MCP 工具、``main.py mail``、巡检线程）只碰这个类，不碰里面的零件 ——
这样换后端（比如以后接 IMAP 或别家）只要换 ``_build_backend``。

★隐私边界在这里落地★
    - token / 索引 / 草稿 / 审计全在 ``data/mail/``（已进 .gitignore）；
    - ``read()`` 拉回来的正文只留在本地索引，不进日志；
    - 分类只发「主题 + 发件人 + 前 N 字」，N 来自 ``[mail] classify_chars``。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .auth import Auth, AuthError, DeviceCode
from .delta import Syncer
from .graph import Graph, GraphError
from .outbox import Outbox, STATE_SENT
from .store import MailStore
from .triage import Triage

SECRET_FILE = "confirm.key"
TOKEN_FILE = "token.json"
DB_FILE = "index.sqlite"
AUDIT_FILE = "audit.jsonl"


def mail_root(settings: Any) -> Path:
    """``[mail] dir``（默认 ``data/mail``），相对项目根。"""
    cfg = getattr(settings, "mail", None)
    rel = str(getattr(cfg, "dir", "data/mail") or "data/mail")
    return settings.resolve(rel)


def _secret(root: Path) -> bytes:
    """confirm_token 的签名密钥：第一次用就生成（不进 git）。"""
    import os  # noqa: PLC0415

    path = root / SECRET_FILE
    try:
        if path.exists():
            data = path.read_bytes()
            if len(data) >= 16:
                return data
        root.mkdir(parents=True, exist_ok=True)
        data = os.urandom(32)
        path.write_bytes(data)
        if os.name != "nt":
            os.chmod(path, 0o600)
        return data
    except OSError:  # noqa: BLE001 - 写不了就用内存里的（重启后 token 失效，可接受）
        return os.urandom(32)


def _html_to_text(html: str) -> str:
    """邮件正文 HTML → 可念的纯文本（去脚本/样式/标签，压空白）。"""
    text = str(html or "")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_quoted(text: str) -> str:
    """去掉「-----Original Message-----」之后、以及整段引用（``>`` 开头）的旧内容。"""
    body = str(text or "")
    cut = re.split(
        r"(?im)^(?:-{2,}\s*(?:original message|原始邮件|forwarded message)|在.*写道[:：]?"
        r"|发件人[:：].*|from:.*|sent:.*)",
        body,
    )
    if cut:
        body = cut[0]
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith(">")]
    return "\n".join(lines).strip()


class MailService:
    def __init__(
        self,
        settings: Any,
        *,
        log: logging.Logger | None = None,
        llm: Callable[[str, str], str] | None = None,
        clock: Callable[[], float] | None = None,
        graph: Graph | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.mail
        self.log = log or logging.getLogger("voice_loop.mail")
        self.root = mail_root(settings)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time

        self.store = MailStore(self.root / DB_FILE, self.log)
        self.auth = Auth(
            str(getattr(self.cfg, "client_id", "") or ""),
            token_file=self.root / TOKEN_FILE,
            tenant=str(getattr(self.cfg, "tenant", "consumers") or "consumers"),
            log=self.log,
            timeout=float(getattr(self.cfg, "timeout", 20) or 20),
        )
        self._graph = graph
        self.llm = llm
        self._me: set[str] = set()
        #: 建完草稿后的回调（流水线接上「右下角审批卡」；没接就只留在库里）
        self.on_draft: Callable[[str], None] | None = None
        self.triage = Triage(self.cfg, self.store, log=self.log, llm=llm, me=self._me)
        self.outbox = Outbox(
            self.store, self.cfg, graph=self.graph, log=self.log,
            secret=_secret(self.root), audit_path=self.root / AUDIT_FILE, clock=self.clock,
        )
        self.syncer = Syncer(
            self.graph, self.store,
            folder=str(getattr(self.cfg, "folder", "inbox") or "inbox"),
            first_days=int(getattr(self.cfg, "first_sync_days", 30) or 30),
            page_size=int(getattr(self.cfg, "page_size", 50) or 50),
            max_pages=int(getattr(self.cfg, "max_pages", 10) or 10),
            log=self.log,
        )

    # ---------------------------------------------------------------- 装配
    @property
    def graph(self) -> Graph:
        if self._graph is None:
            self._graph = Graph(
                self.auth, log=self.log,
                timeout=float(getattr(self.cfg, "timeout", 20) or 20),
            )
        return self._graph

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    def ready(self) -> tuple[bool, str]:
        """能不能真的干活：开了 + 配了 client_id + 登录过。"""
        if not self.enabled:
            return False, "邮件功能关着（[mail] enabled = false）"
        if not self.auth.configured:
            return False, "还没配 [mail] client_id（见 docs/guide/mail.md）"
        tok = self.auth.load()
        if tok is None or not tok.access_token:
            return False, "还没登录（python main.py mail login）"
        return True, ""

    def status(self) -> dict[str, Any]:
        ok, why = self.ready()
        return {
            "enabled": self.enabled,
            "ready": ok,
            "reason": why,
            "auth": self.auth.status(),
            "account": self.auth.load().account if self.auth.load() else "",
            "db": str(self.root / DB_FILE),
            **self.store.counts(),
        }

    # ---------------------------------------------------------------- 登录
    def start_login(self) -> DeviceCode:
        return self.auth.start_device_login()

    def finish_login(self, pending: DeviceCode, *, printer: Callable[[str], None] | None = None,
                     sleeper: Callable[[float], None] | None = None) -> dict[str, Any]:
        token = self.auth.poll_device_login(
            pending, sleeper=sleeper,
            on_wait=(lambda note: printer(f"…{note}") if printer else None),
        )
        self.auth.save(token)
        who = {}
        try:
            who = self.graph.me()
            self.auth.set_account(str(who.get("mail") or ""))
            if who.get("mail"):
                self._me = {str(who["mail"]).lower()}
                self.triage.me = self._me
        except GraphError as exc:
            self.log.warning(f"[mail] 登录成功但读不到账号信息：{exc.reason}")
        return {"ok": True, "account": who.get("mail") or "", "name": who.get("name") or ""}

    # ---------------------------------------------------------------- 同步
    def sync(self, *, force_full: bool = False) -> dict[str, Any]:
        ok, why = self.ready()
        if not ok:
            return {"ok": False, "error": why, "added": 0, "updated": 0}
        if not self._me:
            try:
                who = self.graph.me()
                if who.get("mail"):
                    self._me = {str(who["mail"]).lower()}
                    self.triage.me = self._me
                    self.auth.set_account(str(who["mail"]))
            except GraphError:
                pass
        result = self.syncer.sync(force_full=force_full)
        if result.get("ok"):
            # 新进来的邮件立刻分级（限流：每轮最多 N 封走模型）
            limit = int(getattr(self.cfg, "classify_per_sync", 10) or 10)
            for msg in self.store.untriaged(limit=limit):
                info = self.triage.classify(msg)
                self.store.set_triage(str(msg["id"]), info)
        return result

    # ---------------------------------------------------------------- 读
    def search(self, query: str = "", *, limit: int = 20, unread_only: bool = False,
               level: str = "", sender: str = "", since_hours: float = 0) -> list[dict]:
        since = ""
        if since_hours:
            moment = datetime.now(timezone.utc) - timedelta(hours=float(since_hours))
            since = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.store.search(query, limit=limit, unread_only=unread_only,
                                 level=level, sender=sender, since_iso=since)
        return [self._brief(r) for r in rows]

    def read(self, message_id: str, *, max_chars: int = 0) -> dict[str, Any]:
        row = self.store.get(message_id)
        if row is None:
            return {"ok": False, "error": "本地索引里没有这封邮件（先同步一下）"}
        limit = int(max_chars or getattr(self.cfg, "max_body_chars", 6000) or 6000)
        body = str(row.get("body_text") or "")
        fresh = (self.clock() - float(row.get("body_cached_at") or 0)) < 86400
        if not body or not fresh:
            try:
                full = self.graph.message(message_id)
                body = _strip_quoted(_html_to_text(str(full.get("body_html") or ""))
                                     if str(full.get("body_type") or "").lower() == "html"
                                     else str(full.get("body_html") or ""))
                if not body:
                    body = str(row.get("preview") or "")
                self.store.set_body(message_id, body)
            except GraphError as exc:
                self.log.warning(f"[mail] 拉正文失败（用摘要代替）：{exc.reason}")
                body = str(row.get("preview") or "")
        return {
            "ok": True,
            "id": message_id,
            "subject": row.get("subject") or "",
            "sender": f"{row.get('sender_name') or ''} <{row.get('sender') or ''}>".strip(),
            "received_at": row.get("received_at") or "",
            "level": row.get("level") or "",
            "one_line": row.get("one_line") or "",
            "body": body[:limit] + ("…" if len(body) > limit else ""),
            "truncated": len(body) > limit,
            "web_link": row.get("web_link") or "",
        }

    def summary(self, *, since_hours: float = 24, only_important: bool = True) -> str:
        """一句话清单：给人念，也给模型做二次归纳。"""
        rows = self.search("", limit=30, since_hours=since_hours,
                           level="high" if only_important else "")
        if not rows:
            return ("这段时间没有重要邮件。" if only_important
                    else "这段时间没有新邮件。")
        head = f"共 {len(rows)} 封重要邮件：" if only_important else f"共 {len(rows)} 封邮件："
        parts = [head]
        for row in rows[:10]:
            when = _short_time(row.get("received_at") or "")
            who = row.get("sender_name") or row.get("sender") or "未知发件人"
            what = row.get("one_line") or row.get("subject") or "（无主题）"
            flag = "，需要回复" if row.get("needs_reply") else ""
            parts.append(f"{when}{who}：{what}{flag}")
        return "".join(p + "；" for p in parts).rstrip("；") + "。"

    # ---------------------------------------------------------------- 发信
    def draft(self, *, to: str | list[str], subject: str, body: str,
              cc: str | list[str] = "", reply_to: str = "") -> dict[str, Any]:
        ok, why = self.ready()
        if not ok:
            return {"ok": False, "error": why}
        to_list = [to] if isinstance(to, str) else list(to or [])
        cc_list = [cc] if isinstance(cc, str) else list(cc or [])
        try:
            draft = self.outbox.create(
                to=[a for a in (x.strip() for x in to_list) if a],
                cc=[a for a in (x.strip() for x in cc_list) if a],
                subject=subject, body=body, reply_to=reply_to,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if self.on_draft is not None:
            try:
                self.on_draft(str(draft["id"]))
            except Exception as exc:  # noqa: BLE001 - 弹卡片失败不影响草稿
                self.log.warning(f"[mail] 通知审批卡失败（忽略）：{exc}")
        ok2, why2 = self.outbox.precheck(draft)
        return {
            "ok": True,
            "draft_id": draft["id"],
            "can_send": ok2,
            "note": why2 if not ok2 else "已排入审批，等你在弹窗里点「发送」",
            "preview": self.outbox.preview(draft),
            "state": draft["state"],
        }

    def approve(self, draft_id: str, *, by: str = "user") -> tuple[bool, str, str]:
        return self.outbox.approve(draft_id, by=by)

    def reject(self, draft_id: str, why: str = "") -> tuple[bool, str]:
        return self.outbox.reject(draft_id, why)

    def send(self, draft_id: str, token: str = "") -> tuple[bool, dict]:
        return self.outbox.send(draft_id, token)

    def drafts(self) -> list[dict[str, Any]]:
        return [self.outbox.preview(d) for d in self.outbox.pending()]

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _brief(row: dict) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "subject": row.get("subject") or "",
            "sender": row.get("sender") or "",
            "sender_name": row.get("sender_name") or "",
            "received_at": row.get("received_at") or "",
            "unread": not row.get("is_read"),
            "level": row.get("level") or "",
            "score": int(row.get("score") or 0),
            "category": row.get("category") or "",
            "needs_reply": bool(row.get("needs_reply")),
            "one_line": row.get("one_line") or "",
            "preview": (row.get("preview") or "")[:120],
        }

    def close(self) -> None:
        self.store.close()


def _short_time(iso: str) -> str:
    """``2026-11-20T06:12:00Z`` → ``06:12``（本地时区不好念，就报 UTC+0 的小时分钟）。"""
    text = str(iso or "")
    match = re.search(r"T(\d{2}):(\d{2})", text)
    if not match:
        return ""
    return f"{match.group(1)}:{match.group(2)} "
