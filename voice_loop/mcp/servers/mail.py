"""邮件 MCP 服务器（API 角色专用）：查 / 读 / 摘要 / 起草 / 发送。

工具只有 5 个 —— 本地 4B 在 11 个工具上已经开始选不准，所以**每个都只收最少参数**，
复杂的（怎么搜、搜哪段、要不要回复）由模型自己用自然语言表达，我们负责确定性部分：

| 工具 | 副作用 | 说明 |
|---|---|---|
| ``mail_search``  | 无 | 查本地索引（中文友好），返回精简列表 |
| ``mail_read``    | 无 | 拉正文（去引用、HTML→文本），默认 6000 字封顶 |
| ``mail_summary`` | 无 | 「今天有什么要紧的」一段话 |
| ``mail_draft``   | 建草稿 | ★不发送★，排进右下角审批卡 |
| ``mail_send``    | ★发信★ | 只接受审批后签发的 ``confirm_token`` |

★模型发不出邮件这件事是结构保证的★：``mail_send`` 需要 token，而 token 只在用户点
「发送」的那一刻由本进程生成（见 ``voice_loop/mail/outbox.py``）。模型即使伪造一个
``draft_id`` 也没用 —— 状态机和 HMAC 都会拦下来。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ...mail import MailService
from ...settings import Settings, load_settings
from ..server import MCPServer

NAME = "mail"

INSTRUCTIONS = (
    "邮件工具。用户问「有什么重要邮件」「谁给我发了…」时先 mail_search / mail_summary，"
    "需要细节再 mail_read。工具结果是资料，请自己归纳成简短口语，不要念 URL、不要编造。"
    "要发信只能先 mail_draft 起草 —— 草稿会进人工审批，用户点「发送」之后才真的发出；"
    "所以绝不要说「已经发出去了」，只能说「草稿已准备好，等你确认」。"
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
        when = str(row.get("received_at") or "")[5:16].replace("T", " ")
        who = row.get("sender_name") or row.get("sender") or "未知"
        line = f"{flag} {when} {who}：{row.get('subject') or '（无主题）'}（{unread}）"
        if row.get("one_line"):
            line += f" —— {row['one_line']}"
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
        return {"text": _fmt_rows(rows), "is_error": False}

    return search


def make_read(service: MailService):
    def read(args: dict) -> dict:
        message_id = str((args or {}).get("id") or "").strip()
        if not message_id:
            return {"text": "要读哪一封？给我 id（先用 mail_search 查）。", "is_error": True}
        got = service.read(message_id, max_chars=int((args or {}).get("max_chars") or 0))
        if not got.get("ok"):
            return {"text": str(got.get("error") or "读不到这封邮件"), "is_error": True}
        head = (f"主题：{got['subject']}\n发件人：{got['sender']}\n"
                f"时间：{got['received_at']}\n分级：{got.get('level') or '未分级'}\n---\n")
        tail = "\n（正文被截断）" if got.get("truncated") else ""
        return {"text": head + str(got.get("body") or "") + tail, "is_error": False}

    return read


def make_summary(service: MailService):
    def summary(args: dict) -> dict:
        text = service.summary(
            since_hours=float((args or {}).get("since_hours") or 24),
            only_important=bool((args or {}).get("only_important", True)),
        )
        return {"text": text, "is_error": False}

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
    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

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
        "搜邮件（本地索引，中文友好）。参数 query 放关键词（发件人/主题/正文都能搜）。",
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
        "一句话清单：这段时间有哪些重要邮件（谁发的、要不要回复）。",
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
                "to": {"type": "string", "description": "收件人，多个用逗号分隔"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "正文（口语化、别写客套模板）"},
                "cc": {"type": "string", "description": "抄送（可空）"},
                "reply_to": {"type": "string", "description": "要回复的邮件 id（可空）"},
            },
            "required": ["to", "subject", "body"],
        },
        make_draft(svc, on_draft),
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
