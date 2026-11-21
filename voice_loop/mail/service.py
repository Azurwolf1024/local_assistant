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

from . import attachments as attachments_kit
from .contacts import Contacts
from .auth import Auth, AuthError, DeviceCode
from .delta import Syncer
from .graph import Graph, GraphError
from .link import STATE_PENDING, EventProposer, EventQueue
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
        #: 建完「日程提议」后的回调（同上一张卡，只是文案不同）
        self.on_proposal: Callable[[str], None] | None = None
        self.proposer = EventProposer(self.cfg, log=self.log, llm=llm)
        #: 收件人匹配（从邮件索引现算；别名表来自 [mail.contacts]）
        self.contacts = Contacts(
            self.store,
            aliases=dict(getattr(self.cfg, "contacts", {}) or {}),
            log=self.log,
        )
        self._owner_name = ""
        #: ★索引新鲜度★：上次同步是什么时候、成没成 —— 模型据此说「我这边可能不是最新的」
        #: （持久化在 meta 里，服务重启后也知道；见 freshness() / stamp()）
        self.last_sync_at: float = 0.0
        self.last_sync: dict[str, Any] = {}
        #: 日程提议队列（``add_event`` 由流水线注入；没接就只提示「接口没接上」）
        self.queue_event = EventQueue(self.store, log=self.log)
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

    def refresh_identity(self) -> str:
        """取一次「我是谁」（Graph 的 displayName + 邮箱），用来排除自己和默认落款。"""
        if self._owner_name:
            return self._owner_name
        try:
            me = self.graph.me() or {}
        except Exception as exc:  # noqa: BLE001 - 取不到不影响收发
            self.log.info(f"[mail] 读不到自己的账号信息（忽略）：{exc}")
            return ""
        address = str(me.get("mail") or me.get("userPrincipalName") or "").strip().lower()
        if address:
            self.contacts.self_addresses.add(address)
        self._owner_name = str(me.get("name") or me.get("displayName") or "").strip()
        if address:
            self.store.meta_set("owner_address", address)
        return self._owner_name

    @property
    def signature(self) -> str:
        """落款用谁：配置写了就用配置，否则用你自己的显示名（读不到就空着）。"""
        configured = str(getattr(self.cfg, "signature", "") or "").strip()
        return configured or self._owner_name or self.store.meta_get("owner_name", "")

    def resolve_recipient(self, query: str) -> dict[str, Any]:
        """「给 AIAA3102 的老师发」→ 具体地址。分不清就返回候选、不硬猜。"""
        if self._owner_name == "":
            name = self.refresh_identity()
            if name:
                self.store.meta_set("owner_name", name)
        self._owner_name = self._owner_name or self.store.meta_get("owner_name", "")
        owner = self.store.meta_get("owner_address", "")
        if owner:
            self.contacts.self_addresses.add(owner)
        return self.contacts.resolve(query)

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
            # 索引新鲜度：控制台 / `main.py mail status` 都靠它回答"到底同没同到最新"
            "freshness": self.freshness(),
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
        self._note_sync(result)
        return result

    def _note_sync(self, result: dict[str, Any]) -> None:
        """记下这一轮同步（成功失败都记：失败也要能看出"索引不是最新的"）。"""
        self.last_sync_at = float(self.clock())
        self.last_sync = dict(result or {})
        try:
            self.store.meta_set("last_sync_at", str(self.last_sync_at))
            self.store.meta_set("last_sync_ok", "1" if result.get("ok") else "0")
        except Exception:  # noqa: BLE001 - 记账失败不该影响同步结果
            self.log.debug("记同步时间失败（忽略）", exc_info=True)

    def freshness(self) -> dict[str, Any]:
        """索引有多新鲜：上次同步 / 索引里最新一封 / 落后多久。**只读本地，不联网。**

        为什么要它：模型只能看见索引。索引落后时它必须说「我这边可能不是最新的」，
        而不是把"索引里没有"讲成"没有新邮件"（2026-11-21 真机就是这么翻车的）。
        """
        now = float(self.clock())
        last = float(self.last_sync_at or 0.0)
        if not last:
            raw = self.store.meta_get("last_sync_at") or ""
            try:
                last = float(raw)
            except (TypeError, ValueError):
                last = 0.0
        newest = self.store.newest_at()
        newest_ts = 0.0
        moment = _to_local(newest)
        if moment is not None:
            newest_ts = moment.timestamp()
        interval = max(20.0, float(getattr(self.cfg, "sync_minutes", 3) or 3) * 60.0)
        # 超过 3 个周期（至少 15 分钟）没同步过 = 不可信
        limit = max(900.0, interval * 3.0)
        age = (now - last) if last else None
        ok = str(self.store.meta_get("last_sync_ok") or "1") == "1"
        return {
            "last_sync_at": last,
            "last_sync_age": age,
            "last_sync_ok": ok,
            "newest_at": newest,
            "newest_age": (now - newest_ts) if newest_ts else None,
            "total": int(self.store.counts().get("total") or 0),
            "broken": self.store.broken_count(),
            "stale": (not last) or (not ok) or (age is not None and age > limit),
            "limit": limit,
        }

    def stamp(self) -> str:
        """给工具结果用的一句话：「（索引同步于 20:55，最新一封 20:50）」。

        ★别小看这句★：模型看见"索引最新一封是昨天"就不会斩钉截铁说"没有新邮件"。
        """
        f = self.freshness()
        if not f["total"]:
            return "（本地索引还是空的：先让它同步一次，或确认 [mail] 已登录）"
        parts = []
        if f["last_sync_at"]:
            parts.append("索引同步于 " + time.strftime("%H:%M", time.localtime(f["last_sync_at"])))
        elif f["stale"]:
            parts.append("这个进程还没同步过")
        if f["newest_at"]:
            parts.append("最新一封 " + local_stamp(f["newest_at"]))
        text = "（" + "，".join(parts) + "）"
        if f["stale"]:
            text += "★这份索引可能不是最新的，先别断言「没有新邮件」★"
        return text

    # ---------------------------------------------------------------- 读
    def search(self, query: str = "", *, limit: int = 20, unread_only: bool = False,
               level: str = "", sender: str = "", since_hours: float = 0) -> list[dict]:
        since = ""
        if since_hours:
            moment = datetime.now(timezone.utc) - timedelta(hours=float(since_hours))
            since = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.store.search(query, limit=limit, unread_only=unread_only,
                                 level=level, sender=sender, since_iso=since)
        out = []
        for row in rows:
            brief = self._brief(row)
            if brief["attachments"] is None:
                brief["attachments"] = []
            if row.get("has_attachments") and not brief["attachments"]:
                # 索引里存过清单就直接用（同步时不拉，第一次问到时才拉）
                brief["attachments"] = [
                    {"name": a.get("name"), "size": a.get("size"),
                     "kind": attachments_kit.kind_of(a.get("name") or "",
                                                     a.get("content_type") or "")}
                    for a in self.store.attachments_of(str(row.get("id") or ""))
                    if not a.get("is_inline")
                ]
            out.append(brief)
        return out

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
        attachments = self.attachments(message_id, with_text=True)
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
            "attachments": attachments,
            "web_link": row.get("web_link") or "",
        }

    # ---------------------------------------------------------------- 附件
    def attachments(self, message_id: str, *, with_text: bool = False) -> list[dict]:
        """这封邮件的附件清单；``with_text`` 时顺便按需抽正文（有硬上限）。

        ★只在真的要看这封邮件时才下载★：同步阶段只记 ``has_attachments``，
        正文一个字节都不拉。
        """
        row = self.store.get(message_id)
        if row is None:
            return []
        cached = self.store.attachments_of(message_id)
        if not cached and row.get("has_attachments"):
            cached = self._fetch_attachment_list(message_id)
        if not with_text:
            return cached
        budget = int(getattr(self.cfg, "attachment_chars", 4000) or 4000)
        max_mb = float(getattr(self.cfg, "max_attachment_mb", 8) or 8)
        out = []
        left = budget
        for item in cached:
            got = dict(item)
            if left > 0 and not got.get("text") and attachments_kit.can_extract(
                    got.get("name") or "", got.get("content_type") or ""):
                if int(got.get("size") or 0) > max_mb * 1024 * 1024:
                    got["note"] = f"太大（超过 {max_mb:.0f} MB），没有下载"
                else:
                    text = self._download_attachment_text(
                        message_id, got, min(left, budget))
                    if text:
                        got["text"] = text
                        self.store.set_attachment_text(message_id, str(got.get("att_id") or ""), text)
                        left -= len(text)
            out.append(got)
        return out

    def _fetch_attachment_list(self, message_id: str) -> list[dict]:
        try:
            items = self.graph.attachments(message_id)
        except GraphError as exc:
            self.log.info(f"[mail] 拉附件列表失败（忽略）：{exc.reason}")
            return []
        self.store.set_attachments(message_id, items)
        return self.store.attachments_of(message_id)

    def _download_attachment_text(self, message_id: str, item: dict, max_chars: int) -> str:
        try:
            data = self.graph.attachment_bytes(message_id, str(item.get("att_id") or ""))
        except GraphError as exc:
            self.log.info(f"[mail] 下载附件失败（{item.get('name')}）：{exc.reason}")
            return ""
        return attachments_kit.extract_text(
            data, str(item.get("name") or ""), str(item.get("content_type") or ""),
            max_chars=max_chars, log=self.log,
        )

    def summary(self, *, since_hours: float = 24, only_important: bool = True) -> str:
        """一句话清单：给人念，也给模型做二次归纳。

        ★「没有重要邮件」≠「没有邮件」★：只要索引里这段时间有普通邮件，就必须说出来 ——
        2026-11-21 真机上模型把前者讲成了"没有新邮件"，用户当场发现信箱里有货。
        """
        rows = self.search("", limit=30, since_hours=since_hours,
                           level="high" if only_important else "")
        if not rows and only_important:
            others = self.search("", limit=3, since_hours=since_hours)
            if others:
                newest = others[0]
                who = newest.get("sender_name") or newest.get("sender") or "未知发件人"
                what = newest.get("one_line") or newest.get("subject") or "（无主题）"
                many = "几封" if len(others) >= 3 else f"{len(others)} 封"
                return (f"这段时间没有重要邮件，但有{many}普通邮件；最新一封是 "
                        f"{_short_time(newest.get('received_at') or '')}"
                        f"{who}：{what}。")
            return "这段时间没有新邮件。"
        if not rows:
            return "这段时间没有新邮件。"
        head = f"共 {len(rows)} 封重要邮件：" if only_important else f"共 {len(rows)} 封邮件："
        parts = []
        for row in rows[:10]:
            when = _short_time(row.get("received_at") or "")
            who = row.get("sender_name") or row.get("sender") or "未知发件人"
            what = row.get("one_line") or row.get("subject") or "（无主题）"
            flag = "，需要回复" if row.get("needs_reply") else ""
            parts.append(f"{when}{who}：{what}{flag}")
        return head + "；".join(parts) + "。"

    # ---------------------------------------------------------------- 事件联动
    def propose_event(self, message_id: str, *, body: str = "") -> dict[str, Any]:
        """把一封邮件读成一条「日程提议」（★不写日程★，等人点「加入」）。"""
        row = self.store.get(message_id)
        if row is None:
            return {"ok": False, "error": "本地索引里没有这封邮件（先同步一下）"}
        text = body
        if not text:
            got = self.read(message_id)
            text = str(got.get("body") or "") if got.get("ok") else ""
        proposal = self.proposer.propose(row, body=text)
        if proposal is None:
            return {"ok": False,
                    "error": "这封邮件里没看出「有明确时间、要你去做」的事"}
        item = self.queue_event.create(row, proposal)
        if self.on_proposal is not None:
            try:
                self.on_proposal(str(item["id"]))
            except Exception as exc:  # noqa: BLE001 - 弹卡片失败不影响提议落库
                self.log.warning(f"[mail] 通知日程卡失败（忽略）：{exc}")
        return {"ok": True, "proposal": item, "note": "等你确认后才会加进日程"}

    def proposals(self) -> list[dict]:
        return self.store.proposals(states=(STATE_PENDING,))

    # ---------------------------------------------------------------- 发信
    def draft(self, *, to: str | list[str], subject: str, body: str,
              cc: str | list[str] = "", reply_to: str = "") -> dict[str, Any]:
        ok, why = self.ready()
        if not ok:
            return {"ok": False, "error": why}
        to_list = [to] if isinstance(to, str) else list(to or [])
        cc_list = [cc] if isinstance(cc, str) else list(cc or [])
        # ★收件人匹配★：没有 @ 的一律按「名字/机构/别名」解析（绝不瞎编地址）；
        # 分不清就把候选抛回去，让模型去问用户，而不是赌一个
        resolved: list[str] = []
        for item in to_list:
            text = str(item or "").strip()
            if not text:
                continue
            if "@" in text:
                resolved.append(text)
                continue
            got = self.resolve_recipient(text)
            if not got.get("ok"):
                return {"ok": False, "error": str(got.get("reason") or "认不出收件人"),
                        "candidates": got.get("candidates") or [],
                        "hint": "问用户是哪一个（把候选念出来），不要自己编地址"}
            resolved.append(str(got.get("address") or ""))
        to_list = resolved
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
            "deadline": row.get("deadline") or "",
            "one_line": row.get("one_line") or "",
            "preview": (row.get("preview") or "")[:120],
            "attachments": list(row.get("attachments") or []),
        }

    def close(self) -> None:
        self.store.close()


def _to_local(iso: str) -> datetime | None:
    """Graph 给的时间都是 UTC（``2026-11-20T06:12:00Z``）→ 换成本机时区。"""
    text = str(iso or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def local_stamp(iso: str) -> str:
    """``2026-11-20T06:12:00Z`` → ``11-20 14:12``（本机时区，列表用）。"""
    moment = _to_local(iso)
    return moment.strftime("%m-%d %H:%M") if moment else ""


def _short_time(iso: str) -> str:
    """``2026-11-20T06:12:00Z`` → ``14:12 ``（本机时区；念出来时不用管日期）。"""
    moment = _to_local(iso)
    return moment.strftime("%H:%M ") if moment else ""
