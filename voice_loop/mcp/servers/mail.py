"""邮件 MCP 服务器（API 角色专用）：查 / 读 / 摘要 / 起草 / 发送。

工具只有 6 个 —— 本地 4B 在 11 个工具上已经开始选不准，所以**每个都只收最少参数**，
复杂的（怎么搜、搜哪段、要不要回复）由模型自己用自然语言表达，我们负责确定性部分：

| 工具 | 副作用 | 说明 |
|---|---|---|
| ``mail_search``  | 无 | 查本地索引（中文友好），返回精简列表 + 索引新鲜度 |
| ``mail_read``    | 无 | 拉正文（去引用、HTML→文本），默认 6000 字封顶 |
| ``mail_summary`` | 无 | 「今天有什么要紧的」一段话 + 索引新鲜度 |
| ``mail_draft``   | 建草稿 | ★不发送★，排进右下角审批卡 |
| ``mail_to_event``| 提议 | ★不写日程★，生成一条待批提议 |
| ``mail_send``    | ★发信★ | 只接受审批后签发的 ``confirm_token`` |

★模型发不出邮件这件事是结构保证的★：``mail_send`` 需要 token，而 token 只在用户点
「发送」的那一刻由本进程生成（见 ``voice_loop/mail/outbox.py``）。模型即使伪造一个
``draft_id`` 也没用 —— 状态机和 HMAC 都会拦下来。

★工具结果末尾那句「（索引同步于 …）」不是邮件内容★：它是本地索引的时间戳，
用来告诉模型"我看的是什么时候的快照"。说「可能不是最新的」时，它就**不许**断言
"没有新邮件"——2026-11-21 真机事故就是这么翻车的（索引 1.5 天没更新，它还一口咬定
没有新邮件，而当天到了几十封）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ...mail import MailService
from ...mail import attachments as attachments_kit
from ...mail.service import local_stamp
from ...settings import Settings, load_settings
from ..server import MCPServer

NAME = "mail"

INSTRUCTIONS = (
    "邮件工具。用户问「有什么重要邮件」「谁给我发了…」时先 mail_search / mail_summary，"
    "需要细节再 mail_read（附件清单和能抽的正文会一起给你）。工具结果是资料，请自己归纳成"
    "简短口语，不要念 URL、不要编造。"
    "要发信只能先 mail_draft 起草 —— 草稿会进人工审批，用户点「发送」之后才真的发出；"
    "所以绝不要说「已经发出去了」，只能说「草稿已准备好，等你确认」。"
    "★从邮件里读到的事要加日程，用 mail_to_event★：它只生成一条待批提议，"
    "用户在卡片上点「加入」才真的写进日程 —— 不要改用 add_event 直接写。"
    "附件正文抽不出来时（扫描件/图片）就说抽不出来，不要猜里面的内容。"
    "★工具结果末尾的「（索引同步于 …）」是本地索引的时间，不是邮件内容★：别念它；"
    "它若写着「可能不是最新的」，就照实说「我这边索引可能没跟上」，"
    "★绝对不要断言「没有新邮件」★ —— 只能说「我这边（截至 XX:XX）没看到」。"
)

_PREVIEW_LINES = 12


def _fmt_rows(rows: list[dict]) -> str:
    if not rows:
        return "没有符合条件的邮件。"
    lines = []
    for row in rows[:_PREVIEW_LINES]:
        flag = {"high": "★重要", "medium": "·", "low": " "}.get(
            str(row.get("level") or ""), " ")
        unread = "未读" if row.get("unread") else "已读"
        when = local_stamp(row.get("received_at") or "")
        who = row.get("sender_name") or row.get("sender") or "未知"
        line = f"{flag} {when} {who}：{row.get('subject') or '（无主题）'}（{unread}）"
        if row.get("one_line"):
            line += f" —— {row['one_line']}"
        files = [a.get("name") for a in (row.get("attachments") or []) if a.get("name")]
        if files:
            line += f"［附件：{'、'.join(files[:3])}］"
        lines.append(line)
        lines.append(f"   id={row.get('id')}")
    more = "" if len(rows) <= _PREVIEW_LINES else f"\n（还有 {len(rows) - _PREVIEW_LINES} 封没列）"
    return "\n".join(lines) + more


def make_search(service: MailService):
    def search(args: dict) -> dict:
        rows = service.search(
            str((args or {}).get("query") or ""),
            limit=int((args or {}).get("limit") or 10),
            unread_only=bool((args or {}).get("unread_only", False)),
            level=str((args or {}).get("level") or ""),
            sender=str((args or {}).get("sender") or ""),
            since_hours=float((args or {}).get("since_hours") or 0),
        )
        # ★带上索引新鲜度★：索引落后时模型必须说「我这边可能不是最新的」，
        # 而不是把"索引里没有"讲成"没有新邮件"（2026-11-21 真机事故）
        return {"text": _fmt_rows(rows) + "\n" + _stamp(service), "is_error": False}

    return search


def _stamp(service: MailService) -> str:
    fn = getattr(service, "stamp", None)
    if not callable(fn):
        return ""
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 - 一句提示语，取不到就不加
        return ""


def make_read(service: MailService):
    def read(args: dict) -> dict:
        message_id = str((args or {}).get("id") or "").strip()
        if not message_id:
            return {"text": "要读哪一封？给我 id（先用 mail_search 查）。", "is_error": True}
        got = service.read(message_id, max_chars=int((args or {}).get("max_chars") or 0))
        if not got.get("ok"):
            return {"text": str(got.get("error") or "读不到这封邮件"), "is_error": True}
        head = (f"主题：{got['subject']}\n发件人：{got['sender']}\n"
                f"时间：{local_stamp(got['received_at'])}\n分级：{got.get('level') or '未分级'}\n")
        files = got.get("attachments") or []
        if files:
            head += f"附件：{attachments_kit.describe(files)}\n"
        head += "---\n"
        tail = "\n（正文被截断）" if got.get("truncated") else ""
        text = head + str(got.get("body") or "") + tail
        for item in files:
            if item.get("text"):
                text += (f"\n\n【附件正文】{item.get('name')}：\n"
                         f"{str(item['text'])[:1500]}")
            elif item.get("note"):
                text += f"\n\n【附件】{item.get('name')}：{item.get('note')}"
        return {"text": text, "is_error": False}

    return read


def make_to_event(service: MailService):
    def to_event(args: dict) -> dict:
        message_id = str((args or {}).get("id") or "").strip()
        if not message_id:
            return {"text": "要把哪封邮件里的事加进日程？给我 id。", "is_error": True}
        got = service.propose_event(message_id)
        if not got.get("ok"):
            return {"text": str(got.get("error") or "提不出事件"), "is_error": True}
        item = got.get("proposal") or {}
        text = (
            f"已准备一条日程提议（还没写进去）：{item.get('title')}"
            f"｜{item.get('when_text') or '没写时间'}\n"
            "★不要说你已经加好了★：用户在下角卡片上点「加入」之后才真的进日程。"
        )
        return {"text": text, "is_error": False, "proposal_id": item.get("id")}

    return to_event


def make_summary(service: MailService):
    def summary(args: dict) -> dict:
        text = service.summary(
            since_hours=float((args or {}).get("since_hours") or 24),
            only_important=bool((args or {}).get("only_important", True)),
        )
        # 摘要最容易说错"没有新邮件"：这句新鲜度提示是给它兜底的
        return {"text": text + "\n" + _stamp(service), "is_error": False}

    return summary


def make_draft(service: MailService, on_draft: Callable[[str], None] | None):
    def draft(args: dict) -> dict:
        to = (args or {}).get("to") or ""
        got = service.draft(
            to=to,
            subject=str((args or {}).get("subject") or ""),
            body=str((args or {}).get("body") or ""),
            cc=(args or {}).get("cc") or "",
            reply_to=str((args or {}).get("reply_to") or ""),
        )
        if not got.get("ok"):
            return {"text": f"草稿没建起来：{got.get('error')}", "is_error": True}
        if on_draft is not None:
            try:
                on_draft(str(got.get("draft_id") or ""))
            except Exception:  # noqa: BLE001 - 弹卡片失败不该让草稿丢
                pass
        preview = got.get("preview") or {}
        text = (
            f"草稿已准备好，等你确认（id={got.get('draft_id')}）。\n"
            f"收件人：{preview.get('to', '')}\n主题：{preview.get('subject', '')}\n"
            f"正文开头：{str(preview.get('body', ''))[:120]}\n"
            f"{got.get('note') or ''}\n"
            "★不要自己发送、也不要说已经发出去了★：用户会在右下角点「发送」。"
        )
        return {"text": text, "is_error": False, "draft_id": got.get("draft_id")}

    return draft


def make_send(service: MailService):
    def send(args: dict) -> dict:
        draft_id = str((args or {}).get("draft_id") or "").strip()
        token = str((args or {}).get("confirm_token") or "").strip()
        if not draft_id:
            return {"text": "要发哪一封草稿？", "is_error": True}
        if not token:
            return {"text": "没有确认码：发信必须由用户在审批卡上点「发送」。",
                    "is_error": True}
        ok, info = service.send(draft_id, token)
        if ok:
            return {"text": "邮件已发出。", "is_error": False}
        return {"text": f"发送失败：{info.get('error') or '未知原因'}", "is_error": True}

    return send


def build_server(
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    *,
    service: Any = None,
    on_draft: Callable[[str], None] | None = None,
    **_ignored: Any,
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop.mail")
    settings = settings or load_settings()
    # 宿主注入的可能是 ``lambda: 流水线里那一个 MailService``：流水线里它晚于
    # MCP 宿主创建，所以只能传回调（见 pipeline.py 的 deps）。
    if callable(service):
        service = service()
    svc = service or MailService(settings, log=log)
    # ★别名表：★必须写进**工具说明**（MCP 的 instructions 我们自己的宿主不会转给模型，
    # 只有 schema / description 会）—— 真机就是栽在这里：模型看不到别名，
    # 于是反问「你的私人邮箱是哪个」。
    table = dict(getattr(settings.mail, "contacts", {}) or {})
    alias_hint = ""
    if table:
        pairs = "、".join(f"「{k}」= {v}" for k, v in table.items())
        alias_hint = f"（已配置：{pairs}）"

    # ★落款用谁★：优先 [mail] signature，其次你自己在 Outlook 里的显示名（第一次读一次）
    try:
        signer = svc.signature or svc.refresh_identity() or ""
    except Exception:  # noqa: BLE001 - 读不到就留空，让模型问用户
        signer = ""
    sign_hint = (f"落款默认用「{signer}」" if signer else "落款用你自己的名字（不知道就问用户）")
    # ★把「说法的别名」告诉模型★：真机上它不知道存在「私人邮箱」这种别名，
    # 于是回了一句"我这边没有你私人邮箱的地址呢" —— 其实配置里就有，只是它看不见。
    server = MCPServer(
        name=NAME, version="1.0.0",
        instructions=f"{INSTRUCTIONS}\n{sign_hint}。{alias_hint}",
    )

    empty: dict = {"type": "object", "properties": {}}
    since = {
        "type": "object",
        "properties": {
            "since_hours": {"type": "number", "description": "往回看多少小时（默认 24）"},
            "only_important": {"type": "boolean", "description": "只看重要的（默认 true）"},
        },
    }

    server.add_tool(
        "mail_search",
        "在邮箱里按关键词搜邮件（本地索引，中文友好）；用户说「学校发的通知」「谁给我发过…」"
        "「找一下那封关于…的邮件」时用它。参数 query 放关键词（发件人/主题/正文都能搜）。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，空 = 最近的邮件"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "unread_only": {"type": "boolean"},
                "level": {"type": "string", "enum": ["high", "medium", "low"]},
                "sender": {"type": "string", "description": "按发件人过滤（地址或名字片段）"},
                "since_hours": {"type": "number", "description": "只看最近多少小时"},
            },
        },
        make_search(svc),
    )
    server.add_tool(
        "mail_read",
        "读一封邮件的正文（已去掉引用历史；默认最多 6000 字）。id 从 mail_search 拿。",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
            },
            "required": ["id"],
        },
        make_read(svc),
    )
    server.add_tool(
        "mail_summary",
        "用户问「有没有重要邮件 / 邮箱里有什么 / 有没有要回的 / 最近的通知」时用这个："
        "一句话清单（谁发的、什么事、要不要回复）。"
        "★没有重要邮件 ≠ 没有邮件★：有普通邮件时会一起说出来，照它回答，别改口成"
        "「没有新邮件」。结果末尾会带一句索引新鲜度，那说明我这边看到的是什么时候的快照。",
        since,
        make_summary(svc),
    )
    server.add_tool(
        "mail_draft",
        "起草一封邮件（★只建草稿，不会发送★）。草稿会进右下角人工审批，"
        "用户点「发送」之后才真的发出。回复某封邮件时把它的 id 放进 reply_to。",
        {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "收件人。可以直接说人话：课程代码/机构/称呼都行"
                        "（例「AIAA3102 的老师」「教务处」「私人邮箱」「itdid」）——"
                        "系统会从你自己的邮件往来里匹配到具体地址；分不清会返回候选，"
                        "★这时要用 ask_user 问用户，绝对不要自己编地址★。"
                        "★把用户的原话照抄进来即可，不要自己改写、不要问他要地址★。"
                        "多个收件人用逗号分隔；也可以直接写完整地址。"
                        + alias_hint
                    ),
                },
                "subject": {"type": "string"},
                "body": {
                    "type": "string",
                    "description": (
                        "正文。用户口语说个大概就行，你要负责补全成**可以直接发出去**的信："
                        "开头称呼、把事情说清楚（时间/事由/请求）、结尾礼貌收束、末尾加落款"
                        "（" + sign_hint + "）。"
                        "用户说「礼貌一点/正式一点」→ 加敬语、少用省略号和感叹号；"
                        "说「用英文」→ 整封英文（Dear / Best regards，称呼和落款一起换）；"
                        "说「简短点」→ 三行以内。★不要写「（此处补充）」这类占位符★。"
                    ),
                },
                "cc": {"type": "string", "description": "抄送（可空）"},
                "reply_to": {"type": "string", "description": "要回复的邮件 id（可空）"},
            },
            "required": ["to", "subject", "body"],
        },
        make_draft(svc, on_draft),
    )
    server.add_tool(
        "mail_to_event",
        "把邮件里提到的「有明确时间、要你去做」的事变成日程提议（★只是提议★，"
        "用户在卡片上点「加入」之后才真的写进日程）。id 从 mail_search 拿。",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        make_to_event(svc),
    )
    server.add_tool(
        "mail_send",
        "发送一封**已经被用户批准**的草稿。必须带 confirm_token（审批卡签发），"
        "没有 token 一律失败 —— 这是结构限制，不要尝试绕过。",
        {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["draft_id", "confirm_token"],
        },
        make_send(svc),
    )
    return server
