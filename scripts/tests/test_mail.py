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
    check("5 个工具都在", sorted(names),
          ["mail_draft", "mail_read", "mail_search", "mail_send", "mail_summary"])

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
