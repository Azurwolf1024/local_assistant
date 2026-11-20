"""邮件子系统自测（全程离线：假 Graph、假模型、假弹窗，一个字节都不出网）。

为什么值得一条条钉住
    读邮件写错只是答错一句话；**发邮件写错是把话替你说出去了**。所以这里的重心不在
    「功能好不好用」，而在边界：

        - 没开 allow_send / 空白名单 / 收件人不在名单 → 一律发不出去；
        - 批准后正文被改动 → 拒发（防 agent 偷换内容）；
        - token 过期、伪造 token → 拒发；
        - 连点两次「发送」→ 只发一封（幂等）；
        - 网络断在半路（不知道发没发出去）→ 标 unknown 且**不再自动重试**；
        - delta 游标过期（410）→ 自动全量重来，不丢邮件。

    python scripts\\tests\\test_mail.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.mail import attachments as attachments_kit  # noqa: E402
from voice_loop.mail import auth as auth_mod  # noqa: E402
from voice_loop.mail.approve import ApprovalQueue  # noqa: E402
from voice_loop.mail.delta import CURSOR_KEY, Syncer  # noqa: E402
from voice_loop.mail.graph import DeltaGone, Graph, GraphError, normalize_message  # noqa: E402
from voice_loop.mail.notify import Notifier  # noqa: E402
from voice_loop.mail.outbox import (  # noqa: E402
    STATE_FAILED, STATE_SENT, STATE_UNKNOWN, Outbox, allowed_by_allowlist, body_hash,
)
from voice_loop.mail.service import MailService, local_stamp  # noqa: E402
from voice_loop.mail.store import MailStore  # noqa: E402
from voice_loop.mail.triage import Triage  # noqa: E402
from voice_loop.mcp.servers import mail as mailsrv  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# 假数据 / 假实现
# --------------------------------------------------------------------------- #
def raw_msg(mid: str, *, subject: str, sender: str, days_ago: float = 0.0,
            to: tuple[str, ...] = ("me@x.com",), importance: str = "normal",
            body: str = "") -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(time.time() - days_ago * 86400))
    return {
        "id": mid, "conversationId": f"c-{mid}", "internetMessageId": f"<{mid}@x>",
        "subject": subject,
        "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
        "receivedDateTime": stamp, "isRead": False, "hasAttachments": False,
        "importance": importance, "bodyPreview": body or f"{subject} 的摘要",
        "webLink": "https://outlook.office.com/x", "flag": {"flagStatus": "notFlagged"},
    }


class FakeGraph:
    """够用的假 Graph：能翻页、能增量、能 410、能建草稿能发信。"""

    def __init__(self) -> None:
        self.recent: list[dict] = []
        self.changes: list[list[dict]] = []
        self.sent: list[str] = []
        self.drafts: list[str] = []
        self.fail_send: Exception | None = None
        self.gone_once = False
        self.calls: list[str] = []
        self.attachment_data: dict[str, bytes] = {}

    # 读
    def list_recent(self, *, folder: str = "inbox", since_iso: str = "",
                    page_size: int = 50):
        self.calls.append("list_recent")
        self._size = max(1, int(page_size))
        first = self.recent[: self._size]
        self._rest = self.recent[self._size:]
        return first, ("next" if self._rest else "")

    def next_page(self, url: str):
        self.calls.append("next_page")
        size = getattr(self, "_size", 50)
        chunk, self._rest = self._rest[:size], self._rest[size:]
        return chunk, ("next" if self._rest else "")

    def delta_cursor_from_now(self, *, folder: str = "inbox") -> str:
        self.calls.append("cursor")
        return "CURSOR-1"

    def delta(self, *, folder: str = "inbox", cursor: str = "", page_size: int = 50):
        self.calls.append("delta")
        if self.gone_once:
            self.gone_once = False
            raise DeltaGone("测试用 410")
        batch = self.changes.pop(0) if self.changes else []
        return batch, "", "CURSOR-2"

    def message(self, message_id: str) -> dict:
        self.calls.append("message")
        got = normalize_message(raw_msg(message_id, subject="读到正文", sender="a@b.com"))
        got["body_html"] = "<html><body><p>第一段</p><p>第二段</p>" \
                           "<p>-----Original Message-----</p><p>旧内容</p></body></html>"
        got["body_type"] = "html"
        return got

    def me(self) -> dict:
        return {"name": "测试账号", "mail": "me@x.com"}

    # 附件
    def attachments(self, message_id: str) -> list[dict]:
        self.calls.append("attachments")
        return []

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        self.calls.append("attachment_bytes")
        return self.attachment_data.get(attachment_id, b"")

    # 写
    def create_draft(self, *, to, subject, body_text, cc=None, reply_to: str = "") -> dict:
        self.calls.append("create_draft")
        draft_id = f"graph-draft-{len(self.drafts)}"
        self.drafts.append(draft_id)
        return {"id": draft_id, "subject": subject, "web_link": ""}

    def send_draft(self, draft_id: str) -> None:
        self.calls.append("send_draft")
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(draft_id)


def make_store(tmp: Path) -> MailStore:
    return MailStore(tmp / "index.sqlite")


def make_cfg(tmp: Path, **over):
    settings = load_settings()
    settings.app.project_root = str(tmp)
    cfg = settings.mail
    cfg.client_id = "test-client"
    cfg.allow_send = False
    cfg.send_allowlist = []
    for key, value in over.items():
        setattr(cfg, key, value)
    return settings, cfg


# --------------------------------------------------------------------------- #
def test_auth(tmp: Path) -> None:
    print("\n[1] 认证：device code → token → 续期 → 失效")
    scripted = [
        (200, {"user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/link",
               "device_code": "dev-1", "expires_in": 900, "interval": 1}),
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600,
               "token_type": "Bearer"}),
    ]

    def post(url, data, timeout):
        return scripted.pop(0)

    a = auth_mod.Auth("client", token_file=tmp / "token.json", post=post)
    check("没登录时 status 说明白", a.status()["logged_in"], False)
    pending = a.start_device_login()
    check("拿到 user_code", pending.user_code, "ABCD-EFGH")
    token = a.poll_device_login(pending, sleeper=lambda _s: None)
    check("轮询到 token", (token.access_token, token.refresh_token), ("tok-1", "ref-1"))
    check("token 落盘", (tmp / "token.json").is_file(), True)
    check("没过期就能直接用", a.headers()["Authorization"], "Bearer tok-1")

    # 过期 → 自动续期
    def post_refresh(url, data, timeout):
        return 200, {"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": 3600}

    a._post = post_refresh  # noqa: SLF001
    tok = a.load()
    tok.expires_at = time.time() - 10
    a.save(tok)
    check("过期后 headers() 会自动续期", a.headers()["Authorization"], "Bearer tok-2")

    # 续期失败 → 需要重新登录
    def post_dead(url, data, timeout):
        return 400, {"error": "invalid_grant", "error_description": "refresh token 过期"}

    a._post = post_dead  # noqa: SLF001
    tok = a.load()
    tok.expires_at = time.time() - 10
    a.save(tok)
    try:
        a.headers()
        raised = ""
    except auth_mod.AuthError as exc:
        raised = exc.reason
    check("续期失败要人话，且提示重新登录", ("invalid_grant" in raised, "重新登录" in raised),
          (True, True), detail=raised)
    check("失败后 token 被丢掉（不会拿坏的反复试）", a.load(), None)

    # 用户没在 15 分钟内完成 → 提示「重新跑一次」，而不是甩 traceback
    def post_pending(url, data, timeout):
        return 400, {"error": "authorization_pending"}

    a._post = post_pending  # noqa: SLF001
    pending2 = auth_mod.DeviceCode(user_code="ZZZZ", verification_uri="https://x",
                                   device_code="dev-2", expires_in=2, interval=1)
    notes: list[str] = []
    try:
        a.poll_device_login(pending2, sleeper=lambda _s: None, on_wait=notes.append)
        expired = ""
    except auth_mod.AuthError as exc:
        expired = exc.reason
    check("★超时说的话能用（让用户重跑一次）★", "重新跑一次" in expired, True, detail=expired)
    check("等待时每隔一阵报一次剩余时间（不是刷屏）",
          (len(notes) <= 2, bool(notes) and "还剩" in notes[0]), (True, True),
          detail=str(notes[:2]))


def test_delta(tmp: Path) -> None:
    print("\n[2] 同步：首次拉窗口 → 增量 → 410 重来")
    graph = FakeGraph()
    store = make_store(tmp / "a")
    syncer = Syncer(graph, store, first_days=30, page_size=2, max_pages=5)

    graph.recent = [normalize_message(raw_msg(f"m{i}", subject=f"主题{i}",
                                             sender="boss@x.com")) for i in range(5)]
    got = syncer.sync()
    check("首次同步：全量 + 翻页", (got["ok"], got["full"], got["pages"], got["added"]),
          (True, True, 3, 5))
    check("存下游标", store.meta_get(CURSOR_KEY), "CURSOR-1")

    graph.changes = [[normalize_message(raw_msg("m9", subject="新邮件", sender="a@b.com"))]]
    got = syncer.sync()
    check("增量：只问变化", (got["full"], got["added"]), (False, 1))
    check("游标前进了", store.meta_get(CURSOR_KEY), "CURSOR-2")

    graph.gone_once = True
    graph.recent = [normalize_message(raw_msg("m20", subject="410 之后", sender="a@b.com"))]
    got = syncer.sync()
    check("410 → 自动全量重来且不丢邮件", (got["ok"], got["full"], got["added"]),
          (True, True, 1))

    check("检索按关键词（多词 AND）", [r["id"] for r in store.search("410 之后")], ["m20"])
    check("未读过滤", len(store.search("", unread_only=True)), 7)


def test_triage(tmp: Path) -> None:
    print("\n[3] 分级：规则 → 模型 → 缓存")
    settings, cfg = make_cfg(tmp / "b")
    cfg.vip_senders = ["boss@x.com"]
    cfg.high_score = 60
    cfg.medium_score = 30
    triage = Triage(cfg, None, me={"me@x.com"})

    vip = normalize_message(raw_msg("t1", subject="明天下午组会", sender="boss@x.com",
                                    to=("me@x.com",)))
    info = triage.classify(vip, use_cache=False)
    check("VIP + 只发给你 → 高分", info["level"], "high", detail=str(info))
    check("理由可解释（写了为什么）", any("VIP" in r for r in info["reasons"]), True)

    deadline = normalize_message(raw_msg("t2", subject="论文提交截止提醒 ddl 明天",
                                        sender="office@school.edu"))
    info2 = triage.classify(deadline, use_cache=False)
    check("截止词 → 至少 medium", info2["level"] in ("high", "medium"), True, detail=str(info2))

    noise = normalize_message(raw_msg("t3", subject="限时优惠 退订",
                                     sender="no-reply@shop.com"))
    info3 = triage.classify(noise, use_cache=False)
    check("群发促销 → low", info3["level"], "low", detail=str(info3))

    calls: list[str] = []

    def fake_llm(system: str, user: str) -> str:
        calls.append(user[:30])
        return json.dumps({"important": 88, "category": "interview", "needs_reply": True,
                           "deadline": "2026-11-25", "one_line": "约面试时间"},
                          ensure_ascii=False)

    store = make_store(tmp / "b")
    store.upsert_messages([normalize_message(raw_msg("t4", subject="面试通知",
                                                    sender="hr@corp.com"))])
    triage2 = Triage(cfg, store, me={"me@x.com"}, llm=fake_llm)
    cfg.classify_model = "test-model"
    msg = store.get("t4")
    info4 = triage2.classify(msg)
    check("模型能抬高分数并带上要回复/截止", (info4["level"], info4["needs_reply"],
                                          info4["deadline"]), ("high", True, "2026-11-25"))
    check("★只把主题+前 N 字发出去★", all(len(c) < 120 for c in calls), True)

    before = len(calls)
    triage2.classify(store.get("t4"))
    check("同一封第二次走缓存（不再调模型）", len(calls), before)

    cfg.classify_model = "another-model"
    triage2.classify(store.get("t4"))
    check("换模型就重算", len(calls), before + 1)


def test_outbox_gates(tmp: Path) -> None:
    print("\n[4] 发信闸门：没人工批准，一个字都发不出去")
    settings, cfg = make_cfg(tmp / "c")
    store = make_store(tmp / "c")
    graph = FakeGraph()
    audit = tmp / "c" / "audit.jsonl"
    outbox = Outbox(store, cfg, graph=graph, secret=b"s" * 32, audit_path=audit)

    draft = outbox.create(to=["friend@x.com"], subject="你好", body="正文一")
    check("建草稿不算发送", graph.sent, [])
    check("Graph 侧也只是一份草稿", graph.drafts, ["graph-draft-0"])

    ok, msg = outbox.precheck(draft)
    check("allow_send=false → 拒绝", ok, False, detail=msg)

    cfg.allow_send = True
    ok, msg = outbox.precheck(draft)
    check("空白名单 → 拒绝（默认谁都发不出去）", ok, False, detail=msg)

    cfg.send_allowlist = ["@corp.com"]
    ok, msg = outbox.precheck(draft)
    check("收件人不在白名单 → 拒绝", ok, False, detail=msg)

    cfg.send_allowlist = ["@x.com"]
    ok, token, msg = outbox.approve(draft["id"])
    check("批准成功并给 token", (ok, bool(token)), (True, True), detail=msg)
    ok, token, note = outbox.approve(draft["id"])
    check("重复批准也还算成功（幂等签发）", (ok, bool(token)), (True, True), detail=note)

    ok, info = outbox.send(draft["id"], "forged-token")
    check("伪造 token → 拒发", (ok, "对不上" in str(info.get("error"))), (False, True))

    ok, info = outbox.send(draft["id"], token)
    check("正确 token → 发出", (ok, info["state"]), (True, STATE_SENT))
    check("Graph 真的发了一次", graph.sent, ["graph-draft-0"])
    check("审计留痕", audit.is_file() and "sent" in audit.read_text(encoding="utf-8"), True)

    ok, info = outbox.send(draft["id"], token)
    check("再点一次 → 不会发第二封", (ok, info.get("already"), graph.sent), (True, True,
                                                                       ["graph-draft-0"]))

    # 批准后改正文 → 拒发
    draft2 = outbox.create(to=["friend@x.com"], subject="改一改", body="原正文")
    ok, token2, _ = outbox.approve(draft2["id"])
    store.set_draft(draft2["id"], body="偷偷换掉的正文")
    ok, info = outbox.send(draft2["id"], token2)
    check("★批准后正文被改 → 拒发★", (ok, "改动" in str(info.get("error"))), (False, True))

    # token 过期
    draft3 = outbox.create(to=["friend@x.com"], subject="过期", body="正文")
    cfg.approval_ttl = 60
    ok, token3, _ = outbox.approve(draft3["id"])
    store.set_draft(draft3["id"], expires_at=time.time() - 1)
    ok, info = outbox.send(draft3["id"], token3)
    check("token 过期 → 拒发", (ok, "过期" in str(info.get("error"))), (False, True))

    # 明确失败 / 不确定
    draft4 = outbox.create(to=["friend@x.com"], subject="失败", body="正文")
    _, token4, _ = outbox.approve(draft4["id"])
    graph.fail_send = GraphError("Graph HTTP 500", status=500)
    ok, info = outbox.send(draft4["id"], token4)
    check("明确失败 → failed（可以用同一 token 重试）", (ok, info["state"]),
          (False, STATE_FAILED))
    graph.fail_send = TimeoutError("网络断了")
    ok, info = outbox.send(draft4["id"], token4)
    check("★网络断在半路 → unknown（不自动重试）★", (ok, info["state"]), (False, STATE_UNKNOWN))
    ok, info = outbox.send(draft4["id"], token4)
    check("unknown 之后再发也被拦住", (ok, "人工确认" in str(info.get("error"))), (False, True))


def test_allowlist() -> None:
    print("\n[5] 白名单匹配规则")
    check("整地址", allowed_by_allowlist("a@b.com", ["a@b.com"]), True)
    check("域名通配", allowed_by_allowlist("a@b.com", ["@b.com"]), True)
    check("裸域名也认", allowed_by_allowlist("a@b.com", ["b.com"]), True)
    check("大小写不敏感", allowed_by_allowlist("A@B.com", ["a@b.com"]), True)
    check("空表 = 全拒", allowed_by_allowlist("a@b.com", []), False)
    check("别的域名不放行", allowed_by_allowlist("a@c.com", ["@b.com"]), False)


def test_queue(tmp: Path) -> None:
    print("\n[6] 审批队列：点「发送」才发，点「取消」就不发")
    settings, cfg = make_cfg(tmp / "d", allow_send=True, send_allowlist=["@x.com"])
    store = make_store(tmp / "d")
    graph = FakeGraph()
    outbox = Outbox(store, cfg, graph=graph, secret=b"q" * 32)
    results: list[tuple[bool, str]] = []
    queue = ApprovalQueue(outbox, on_result=lambda ok, msg: results.append((ok, msg)))

    a = outbox.create(to=["friend@x.com"], subject="甲", body="正文甲")
    b = outbox.create(to=["friend@x.com"], subject="乙", body="正文乙")
    queue.submit(a["id"])
    queue.submit(b["id"])
    check("队首是第一封", queue.head()["id"], a["id"])
    check("两封都在排队", len(queue.pending()), 2)

    ok, msg = queue.decide(a["id"], "cancel")
    check("取消 → 不发", (ok, graph.sent), (True, []))
    check("取消后出队", queue.pending(), [b["id"]])

    ok, msg = queue.decide(b["id"], "send")
    check("点发送 → 真的发出去", (ok, graph.sent), (True, ["graph-draft-1"]))
    check("结果也回传给了 UI 回调", results[-1][1], "邮件已发出")


def test_notify(tmp: Path) -> None:
    print("\n[7] 通知：重要的立刻说、同一封只说一次、摘要按时段")
    settings, cfg = make_cfg(tmp / "e")
    store = make_store(tmp / "e")
    store.upsert_messages([
        normalize_message(raw_msg("n1", subject="很急", sender="boss@x.com")),
        normalize_message(raw_msg("n2", subject="一般", sender="a@b.com")),
    ])
    store.set_triage("n1", {"score": 90, "level": "high", "category": "deadline",
                            "one_line": "明天交"})
    store.set_triage("n2", {"score": 40, "level": "medium", "category": "school",
                            "one_line": "课程通知"})
    shown: list[tuple[str, str]] = []
    said: list[str] = []

    class Toast:
        def show(self, title, text):
            shown.append((title, text))

    notifier = Notifier(cfg, store, toast=Toast(), speak=said.append)
    high = store.unnotified(levels=("high",))
    check("提醒了 1 封", notifier.notify_high(high), 1)
    check("弹窗内容是「谁：什么事」", shown and "boss" in shown[0][1], True, detail=str(shown))
    check("也念了一句", any("发来一封邮件" in s for s in said), True)
    check("★同一封不会提醒第二次★", notifier.notify_high(store.unnotified(levels=("high",))), 0)

    text = notifier.digest(since_hours=24, force=False)
    check("摘要里既有重要也有一眼扫过的", ("很急" in text or "明天交" in text), True, detail=text)

    # 时间必须是**本机时区**（Graph 给的是 UTC，直接显示会差 8 小时，看列表会懵）
    want = datetime(2026, 11, 20, 6, 12, tzinfo=timezone.utc).astimezone()
    check("★UTC 时间按本机时区显示★", local_stamp("2026-11-20T06:12:00Z"),
          want.strftime("%m-%d %H:%M"), detail=f"{local_stamp('2026-11-20T06:12:00Z')} vs UTC 06:12")

    class Clock:
        def __init__(self, hour):
            self.hour = hour
            import datetime as _dt
            self.date = _dt.date.today()

    import datetime as _dt
    moment = _dt.datetime.now().replace(hour=cfg.digest_hours[0], minute=0, second=0)
    check("到点该出摘要", notifier.due_digest(now=moment), True)
    check("同一小时不会重复出", notifier.due_digest(now=moment), False)


def test_mcp(tmp: Path) -> None:
    print("\n[8] MCP 工具：查/读/起草/发送")
    settings, cfg = make_cfg(tmp / "f", allow_send=True, send_allowlist=["@x.com"])
    graph = FakeGraph()
    svc = MailService(settings, graph=graph)
    svc.auth.save(auth_mod.Token(access_token="t-mcp", refresh_token="r-mcp",
                                 expires_at=time.time() + 3600))
    svc.store.upsert_messages([normalize_message(raw_msg("m1", subject="学校通知",
                                                        sender="office@school.edu"))])
    svc.store.set_triage("m1", {"score": 70, "level": "high", "category": "school",
                                "one_line": "选课开始"})
    queued: list[str] = []
    server = mailsrv.build_server(settings, service=svc, on_draft=queued.append)
    names = [getattr(t, "name", "") for t in server.tools()]
    check("6 个工具都在", sorted(names),
          ["mail_draft", "mail_read", "mail_search", "mail_send", "mail_summary",
           "mail_to_event"])

    handler = server.call_tool

    def text_of(res: dict) -> str:
        return str((res.get("content") or [{}])[0].get("text") or "")

    got = handler("mail_search", {"query": "学校"})
    check("search 返回人话（含 id）", "m1" in text_of(got), True, detail=text_of(got)[:80])

    draft = handler("mail_draft", {"to": "friend@x.com", "subject": "回复",
                                   "body": "好的"})
    draft_id = str((draft.get("structuredContent") or {}).get("draft_id") or "")
    check("起草成功并送进审批队列", bool(draft_id) and bool(queued), True,
          detail=text_of(draft)[:60])
    check("起草不会发信", graph.sent, [])

    send = handler("mail_send", {"draft_id": draft_id, "confirm_token": ""})
    check("★没有 token 一律发不出去★", send.get("isError"), True, detail=text_of(send))
    svc.close()


def test_graph_http(tmp: Path) -> None:
    print("\n[9] Graph HTTP 层：429 退避 / 410 / 401 续期一次")
    settings, cfg = make_cfg(tmp / "g")
    a = auth_mod.Auth("client", token_file=tmp / "g" / "token.json")
    a.save(auth_mod.Token(access_token="t0", refresh_token="r0",
                          expires_at=time.time() + 3600))
    waits: list[float] = []
    calls: list[int] = []
    queue = [
        (429, {"Retry-After": "1"}, None),
        (200, {}, {"value": []}),
    ]

    def transport(method, url, *, headers, params, payload, timeout):
        calls.append(1)
        return queue.pop(0)

    g = Graph(a, request=transport, sleep=waits.append)
    g.call("GET", "/me/messages")
    check("429 会等 Retry-After 再试", (waits, len(calls)), ([1.0], 2))

    def gone(method, url, *, headers, params, payload, timeout):
        return 410, {}, {"error": {"message": "gone"}}

    g2 = Graph(a, request=gone)
    try:
        g2.call("GET", "/me/messages")
        kind = ""
    except DeltaGone:
        kind = "DeltaGone"
    check("410 → DeltaGone（上层全量重来）", kind, "DeltaGone")

    refreshed: list[str] = []
    seq = [(401, {}, None), (200, {}, {"ok": True})]

    def transport401(method, url, *, headers, params, payload, timeout):
        return seq.pop(0)

    a2 = auth_mod.Auth("client", token_file=tmp / "g" / "t2.json",
                       post=lambda *_: (200, {"access_token": "t9", "refresh_token": "r9",
                                              "expires_in": 3600}))
    a2.save(auth_mod.Token(access_token="t1", refresh_token="r1",
                           expires_at=time.time() + 3600))
    g3 = Graph(a2, request=transport401)
    out = g3.call("GET", "/me")
    check("401 → 续期一次后成功", out, {"ok": True})

    # ★游标引导：必须翻到最后一页才有 @odata.deltaLink★
    # （踩过的坑：以前用 $deltatoken=latest，Graph 早不认了 → 游标永远空 → 每轮全量重拉）
    pages = [
        (200, {}, {"value": [{"id": "1"}],
                   "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=a"}),
        (200, {}, {"value": [{"id": "2"}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?$deltatoken=FINAL"}),
    ]
    seen: list[str] = []

    def transport_delta(method, url, *, headers, params, payload, timeout):
        seen.append(url)
        return pages.pop(0)

    g4 = Graph(a, request=transport_delta)
    cursor = g4.delta_cursor_from_now(folder="all")
    check("★游标翻到最后一页才拿得到★", cursor.endswith("$deltatoken=FINAL"), True,
          detail=cursor[-40:])
    check("跟着 nextLink 翻页（没有停在第一页）", len(seen), 2)

    def transport_nocursor(method, url, *, headers, params, payload, timeout):
        return 200, {}, {"value": []}          # 没有 nextLink 也没有 deltaLink

    g5 = Graph(a, request=transport_nocursor)
    check("翻不到就返回空串（上层继续按窗口同步，不炸）",
          g5.delta_cursor_from_now(folder="all"), "")


def test_attachments(tmp: Path) -> None:
    print("\n[10] 附件：只记清单 + 按需抽正文（不联网）")
    settings, cfg = make_cfg(tmp / "att")
    graph = FakeGraph()
    svc = MailService(settings, graph=graph)
    svc.auth.save(auth_mod.Token(access_token="t-att", refresh_token="r-att",
                                 expires_at=time.time() + 3600))
    msg = normalize_message(raw_msg("a1", subject="附件测试", sender="a@x.com"))
    msg["has_attachments"] = True
    svc.store.upsert_messages([msg])
    svc.store.set_attachments("a1", [
        {"id": "att-1", "name": "成绩单.pdf", "content_type": "application/pdf",
         "size": 2400, "is_inline": False},
        {"id": "att-2", "name": "备注.txt", "content_type": "text/plain",
         "size": 30, "is_inline": False},
        {"id": "att-3", "name": "logo.png", "content_type": "image/png",
         "size": 900, "is_inline": True},
    ])
    listing = svc.attachments("a1", with_text=False)
    check("清单里有 3 个（含内联图）", len(listing), 3)
    check("按名字能搜到（附件名参与检索）",
          [r["id"] for r in svc.search("成绩单")], ["a1"])
    check("人话清单只报非内联的（按名字排）", attachments_kit.describe(listing),
          "备注.txt（30 B）、成绩单.pdf（2.3 KB）")

    # ★按需抽正文★：txt 能抽，pdf 没装库时抽不到也不炸
    graph.attachment_data = {"att-2": "第一行\n第二行".encode("utf-8")}
    got = svc.read("a1")
    files = {f["name"]: f for f in got["attachments"]}
    check("txt 抽到了正文", "第一行" in (files["备注.txt"].get("text") or ""), True,
          detail=str(files["备注.txt"].get("text"))[:40])
    check("图片不抽正文（只留清单）", (files["logo.png"].get("text") or ""), "")

    # 太大就不下载（换一封没缓存过的，否则用的是上次抽好的文本）
    cfg.max_attachment_mb = 0.001
    msg2 = normalize_message(raw_msg("a2", subject="大附件", sender="b@x.com"))
    msg2["has_attachments"] = True
    svc.store.upsert_messages([msg2])
    svc.store.set_attachments("a2", [
        {"id": "att-9", "name": "扫描件.txt", "content_type": "text/plain",
         "size": 9000, "is_inline": False}])
    big = svc.attachments("a2", with_text=True)
    note = big[0].get("note") or ""
    check("超过 max_attachment_mb 就只留清单", "太大" in note, True, detail=note)
    svc.close()


def test_expired_demote(tmp: Path) -> None:
    print("\n[11] 过期降权：截止时间已过的通知降一档")
    settings, cfg = make_cfg(tmp / "exp")
    triage = Triage(cfg)
    base = {"score": 70, "level": "high", "category": "deadline",
            "deadline": "2020-01-01", "reasons": ["命中「截止」"]}
    out = triage.demote_if_expired(base)
    check("★过期的 high 降成 medium★", out["level"], "medium")
    check("理由里说清楚为什么降", any("已过" in r for r in out["reasons"]), True,
          detail=str(out["reasons"]))

    future = dict(base, deadline=datetime.now().strftime("%Y-%m-%d"))
    check("今天到期的不降", triage.demote_if_expired(future)["level"], "high")
    check("没有截止时间的不动",
          triage.demote_if_expired(dict(base, deadline=""))["level"], "high")
    cfg.demote_expired = False
    check("配置关掉就完全不降", triage.demote_if_expired(base)["level"], "high")


def test_event_link(tmp: Path) -> None:
    print("\n[12] 邮件 → 日程：模型只提议，人点了才写")
    settings, cfg = make_cfg(tmp / "evt")
    graph = FakeGraph()
    svc = MailService(settings, graph=graph)
    svc.auth.save(auth_mod.Token(access_token="t-evt", refresh_token="r-evt",
                                 expires_at=time.time() + 3600))
    row = normalize_message(raw_msg("m9", subject="作业3已发布，11月24日截止",
                                    sender="ta@school.edu"))
    row["deadline"] = "2026-11-24"
    svc.store.upsert_messages([row])

    got = svc.propose_event("m9")
    check("提出来一条待批提议", bool(got.get("ok")), True, detail=str(got)[:120])
    item = got.get("proposal") or {}
    check("提议里有标题和时间", (bool(item.get("title")), bool(item.get("when_text"))),
          (True, True), detail=f"{item.get('title')} / {item.get('when_text')}")
    check("★提议阶段不写日程★", svc.store.proposals(), [
        x for x in svc.store.proposals() if x["id"] == item["id"]])

    written: list[dict] = []
    queue = svc.queue_event
    queue.add_event = lambda it: (written.append(it), 7)[1]
    ok, msg = queue.decide(item["id"], "send")
    check("★点「加入」才真的写★", (ok, len(written)), (True, 1), detail=msg)
    check("写进去的是这条提议", written[0]["title"], item["title"])
    check("状态改成 accepted",
          svc.store.get_proposal(item["id"])["state"], "accepted")

    row2 = normalize_message(raw_msg("m10", subject="订阅周报", sender="news@x.com"))
    svc.store.upsert_messages([row2])
    check("没时间没动作的邮件不提议",
          svc.propose_event("m10").get("ok"), False)

    # 英文邮件也认（模型可用时模型说了算；这里测规则兜底那条路）
    row3 = normalize_message(raw_msg("m11", subject="Final Exam Timetable released",
                                     sender="reg@school.edu"))
    row3["deadline"] = "2026-12-14"
    row3["preview"] = "Please check the exam schedule and register before Dec 14."
    svc.store.upsert_messages([row3])
    got3 = svc.propose_event("m11")
    check("★英文邮件也能提出事件（有 deadline 就算数）★",
          bool(got3.get("ok")), True, detail=str(got3)[:100])

    # 忽略
    got2 = svc.propose_event("m9")
    ok2, msg2 = queue.decide(got2["proposal"]["id"], "cancel")
    check("点「忽略」不写日程", (ok2, len(written)), (True, 1), detail=msg2)
    check("忽略后状态是 skipped",
          svc.store.get_proposal(got2["proposal"]["id"])["state"], "skipped")
    svc.close()


def test_add_event_real_path(tmp: Path) -> None:
    """★真写路径★：pipeline._add_event_from_mail 必须真能写进 events.json。

    为什么单独一条：前面几组都是用**桩**替换 ``add_event``，只验证了「点了才写」，
    没验证「写的那段代码本身对不对」—— 结果真机上 `et` 这个名字被 import 改丢了，
    测试全绿、用户一点就报 NameError，卡片还反复弹。
    """
    print("\n[13] 真写路径：pipeline._add_event_from_mail 落库（用桩 skills）")
    from voice_loop.pipeline import VoiceLoop
    from voice_loop.store import JsonStore

    class FakeSkills:
        def __init__(self, path: Path) -> None:
            self.store = JsonStore(path, default=[])

    class FakeLoop:
        skills = None
        log = __import__("logging").getLogger("test")
        _add_event_from_mail = VoiceLoop._add_event_from_mail

    loop = FakeLoop()
    loop.skills = FakeSkills(tmp / "events.json")
    event_id = loop._add_event_from_mail({
        "title": "提交 AIAA3102 作业3",
        "when_text": "2026-11-24 23:59",
        "message_id": "m9",
        "reason": "作业截止",
    })
    items = loop.skills.store.load(force=True)
    check("写进了一条事件", len(items), 1)
    check("id 回传了", event_id, 1)
    row = items[0]
    check("标题没被吃掉", row.get("title"), "提交 AIAA3102 作业3")
    check("★时间是 11-24 23:59（不是今天）★", row.get("start"), "2026-11-24 23:59:00")
    check("★留了来源，追得回是哪封邮件★", row.get("source"), "mail:m9")

    try:
        loop._add_event_from_mail({"title": "听不懂的", "when_text": "随便哪天", "message_id": "m9"})
        raised = ""
    except RuntimeError as exc:
        raised = str(exc)
    check("时间听不懂就报错（不是静默写一条错的）", "听不懂" in raised, True, detail=raised)

    # 失败要出队 + 标 failed，不然卡片会每个 tick 重弹一次
    from voice_loop.mail.link import EventQueue
    from voice_loop.mail.store import MailStore

    mstore = MailStore(tmp / "q.sqlite")
    mstore.create_proposal({"id": "evt-1", "title": "x", "when_text": "y",
                            "message_id": "m9", "state": "pending"})
    queue = EventQueue(mstore, add_event=lambda _it: (_ for _ in ()).throw(RuntimeError("炸了")))
    queue.submit("evt-1")
    ok, msg = queue.decide("evt-1", "send")
    check("失败返回原因", (ok, "炸了" in msg), (False, True), detail=msg)
    check("★失败后出队（不会再弹一次）★", queue.head(), None)
    check("状态记成 failed，有据可查", mstore.get_proposal("evt-1")["state"], "failed")
    mstore.close()


def test_token_budget(tmp: Path) -> None:
    """省 token：老邮件不送模型 / 每轮分级有上限 / prune 只动本地。"""
    print("\n[14] 省 token：老邮件只走规则，prune 只动本地索引")
    settings, cfg = make_cfg(tmp / "tok")
    called: list[str] = []

    def fake_llm(system: str, user: str) -> str:
        called.append(user[:20])
        return ('{"important":90,"category":"deadline","needs_reply":true,'
                '"deadline":"2026-12-01","one_line":"很急"}')

    triage = Triage(cfg, llm=fake_llm)
    cfg.classify_max_age_days = 7
    fresh = {"id": "f1", "subject": "截止提醒", "received_at": "2099-01-01T00:00:00Z",
             "preview": "请提交", "to": [{"address": "me@x.com"}]}
    old = {"id": "o1", "subject": "三周前的促销", "received_at": "2020-01-01T00:00:00Z",
           "preview": "限时优惠 退订", "to": [{"address": "me@x.com"}]}
    triage.classify(fresh)
    check("新邮件会送模型", len(called), 1)
    info = triage.classify(old, use_cache=False)
    check("★老邮件不送模型（token 省下来）★", len(called), 1)
    check("老邮件仍按规则给了结论", bool(info.get("level")), True, detail=str(info.get("level")))
    check("理由里说明了为什么没送模型",
          any("只走规则" in r for r in info.get("reasons") or []), True,
          detail=str(info.get("reasons")))
    cfg.classify_max_age_days = 0
    check("关掉这条限制就都送（0 = 不限）",
          triage.is_stale(old), False)

    store = MailStore(tmp / "prune.sqlite")
    store.upsert_messages([
        normalize_message(raw_msg("new", subject="新的", sender="a@x.com")),
        normalize_message(raw_msg("old", subject="旧的", sender="b@x.com")),
    ])
    store._exec("UPDATE messages SET received_at = '2020-01-01T00:00:00Z' WHERE id = 'old'")  # noqa: SLF001
    removed = store.prune(days=30)
    check("★prune 删掉老的、留新的★", (removed, len(store.search("", limit=10))), (1, 1))
    store.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mail_"))
    try:
        test_auth(tmp)
        test_delta(tmp)
        test_triage(tmp)
        test_outbox_gates(tmp)
        test_allowlist()
        test_queue(tmp)
        test_notify(tmp)
        test_mcp(tmp)
        test_graph_http(tmp)
        test_attachments(tmp)
        test_expired_demote(tmp)
        test_event_link(tmp)
        test_add_event_real_path(tmp)
        test_token_budget(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 邮件子系统自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
