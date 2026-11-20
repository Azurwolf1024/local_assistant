"""邮件 → 日程提议：**模型只提议，人点了才进日程**。

为什么要单独一层
    邮件里常有「11月24日 23:59 前提交」「周三下午三点组会」这类信息，把它变成
    ``data/events.json`` 里的提醒非常有用；但**写日程是会打扰人的副作用**（到点会开口），
    所以和发信一样：模型只能「起草一条提议」，真正落库必须人点一下。

两半分开（能不能离线自测就看这个）
    :class:`EventProposer`  —— 从邮件文本里读出 ``{title, when_text, when_iso, place}``
    :class:`EventQueue`     —— 待批提议的队列 + 「加入 / 忽略」，和 ``ApprovalQueue`` 同形，
                               所以复用右下角那张卡片（``ApprovalCard`` 只认 head/decide 协议）。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Callable

STATE_PENDING = "pending"
STATE_ACCEPTED = "accepted"
STATE_SKIPPED = "skipped"

#: 从正文里捞时间线索（规则兜底；模型可用时以模型为准）
_TIME_HINT = re.compile(
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"|(\d{1,2}\s*月\s*\d{1,2}\s*[日号])"
    r"|((?:下|本|这)?周[一二三四五六日天])"
    r"|(今天|明天|后天|今晚|明晚)"
    r"|(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b)"
    r"|(\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)"
    r"|(\b(?:today|tomorrow|next\s+\w+day)\b)",
    re.IGNORECASE,
)
#: 明显是「事情」的动词/名词（用来判断这封邮件值不值得提议）
_ACTION_HINT = re.compile(
    r"提交|截止|报名|签到|开会|会议|组会|讲座|报告|答辩|考试|面试|缴费|注册|领取|上课|调课|补课"
    r"|submit|deadline|due\b|register|registration|sign[-\s]?up|exam|quiz|defen[cs]e|interview"
    r"|report|seminar|lecture|meeting|appointment",
    re.IGNORECASE,
)


class EventProposer:
    """把一封邮件读成一条「日程提议」。"""

    def __init__(
        self,
        cfg: Any = None,
        *,
        log: logging.Logger | None = None,
        llm: Callable[[str, str], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = log or logging.getLogger("voice_loop.mail")
        self.llm = llm

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "link_events", True)) if self.cfg is not None else True

    # ---------------------------------------------------------------- 提议
    def propose(self, message: dict, *, body: str = "") -> dict | None:
        """返回 ``{title, when_text, when_iso, place, source, reason}``，不值得提议就 ``None``。"""
        if not self.enabled:
            return None
        subject = str(message.get("subject") or "").strip()
        # ★正文 + 预览都要看★：正文可能被去引用/截断，而「12月14日截止」这种关键句
        # 有时只留在 preview 里（实测：正文取到的是别的段落，时间线索反而丢了）
        text = "\n".join(x for x in (subject, body, str(message.get("preview") or "")) if x)
        deadline = str(message.get("deadline") or "").strip()
        worth = bool(deadline) or bool(_ACTION_HINT.search(text)) or bool(
            _TIME_HINT.search(subject))
        if self.llm is not None:
            # ★先问模型★：它认得英文邮件、也读得懂「考试期 12/14–12/25」这种区间。
            # 规则只在模型没给出结论、且文本里确实有时间+动作线索时兜底
            # （模型的偶发漏判不该直接让一件真事丢掉）。
            got = self._ask_llm(message, body)
            if got is not None:
                return got
            if not (deadline and _ACTION_HINT.search(text)):
                return None
        elif not worth:
            return None
        return self._by_rules(message, body)

    def _ask_llm(self, message: dict, body: str) -> dict | None:
        if self.llm is None:
            return None
        subject = str(message.get("subject") or "")[:120]
        snippet = ("\n".join(x for x in (
            body, str(message.get("preview") or "")) if x))[:600]
        system = (
            "你是日程提取器。判断这封邮件里有没有一件**有明确时间、需要用户去做**的事。"
            "只输出一个 JSON 对象，不要解释、不要 markdown：\n"
            '{"has_event":true/false,"title":"不超过 20 字","when":"YYYY-MM-DD HH:MM 或空串",'
            '"place":"地点或空串","reason":"为什么值得记（不超过 20 字）"}\n'
            "没有明确时间、或者只是通知/宣传的，has_event 就是 false。"
        )
        user = (f"今天：{datetime.now().strftime('%Y-%m-%d %A')}\n"
                f"主题：{subject}\n正文开头：{snippet}")
        try:
            raw = self.llm(system, user)
        except Exception as exc:  # noqa: BLE001 - 提议失败不能挡住读信
            self.log.info(f"[mail] 事件提议模型失败（改用规则）：{exc}")
            return None
        match = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
        if not match:
            return None
        import json  # noqa: PLC0415

        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("has_event"):
            return None
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        return {
            "title": title[:40],
            "when_text": str(data.get("when") or "").strip(),
            "when_iso": str(data.get("when") or "").strip(),
            "place": str(data.get("place") or "").strip()[:30],
            "source": "model",
            "reason": str(data.get("reason") or "").strip()[:40],
        }

    def _by_rules(self, message: dict, body: str) -> dict | None:
        text = "\n".join(x for x in (
            str(message.get("subject") or ""), body,
            str(message.get("preview") or "")) if x)
        when = str(message.get("deadline") or "").strip()
        if not when:
            hit = _TIME_HINT.search(text)
            when = hit.group(0) if hit else ""
        if not when:
            return None
        title = re.sub(r"^(回复|答复|re:|fw:|fwd:|转发)[:：]?\s*", "",
                       str(message.get("subject") or ""), flags=re.IGNORECASE).strip()
        return {
            "title": (title or "邮件提到的事")[:40],
            "when_text": when,
            "when_iso": when,
            "place": "",
            "source": "rules",
            "reason": "邮件里提到了时间和要做的事",
        }


class EventQueue:
    """待批日程提议（和 ``ApprovalQueue`` 同形，能直接喂给 ``ApprovalCard``）。"""

    kind = "event"

    def __init__(
        self,
        store: Any,
        *,
        add_event: Callable[[dict], Any] | None = None,
        log: logging.Logger | None = None,
        on_result: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.store = store
        self.add_event = add_event
        self.log = log or logging.getLogger("voice_loop.mail")
        self.on_result = on_result
        self._queue: list[str] = []

    # ---------------------------------------------------------------- 入队
    def submit(self, proposal_id: str) -> dict | None:
        if not proposal_id:
            return None
        if proposal_id not in self._queue:
            self._queue.append(proposal_id)
        return self.store.get_proposal(proposal_id)

    def create(self, message: dict, proposal: dict) -> dict:
        """落一条待批提议（返回存下来的那条）。"""
        item = {
            "id": f"evt-{uuid.uuid4().hex[:12]}",
            "message_id": str(message.get("id") or ""),
            "title": str(proposal.get("title") or "")[:40],
            "when_text": str(proposal.get("when_text") or ""),
            "when_iso": str(proposal.get("when_iso") or ""),
            "place": str(proposal.get("place") or ""),
            "source": str(proposal.get("source") or ""),
            "reason": str(proposal.get("reason") or ""),
            "state": STATE_PENDING,
            "created_at": datetime.now().timestamp(),
        }
        self.store.create_proposal(item)
        self.submit(item["id"])
        self.log.info(f"[mail] 待批日程提议 {item['id']}：{item['title']}（{item['when_text']}）")
        return item

    # ---------------------------------------------------------------- 队列协议
    def pending(self) -> list[str]:
        return list(self._queue)

    def head(self) -> dict | None:
        while self._queue:
            item = self.store.get_proposal(self._queue[0])
            if item is None or item.get("state") != STATE_PENDING:
                self._queue.pop(0)
                continue
            return item
        return None

    def preview(self, item: dict) -> dict[str, str]:
        """``ApprovalCard`` 用同一套字段（收件人那行换成「时间」）。"""
        message = self.store.get(str(item.get("message_id") or "")) or {}
        return {
            "id": str(item.get("id") or ""),
            "to": str(item.get("when_text") or "（没说时间）"),
            "cc": "",
            "subject": str(item.get("title") or ""),
            "body": (f"来自：{message.get('sender_name') or message.get('sender') or '邮件'}"
                     f"｜{message.get('subject') or ''}"
                     + (f"\n{str(item.get('reason') or '')}" if item.get("reason") else "")),
        }

    def decide(self, proposal_id: str, action: str) -> tuple[bool, str]:
        """``send`` = 加入日程（日志/卡片上叫「加入」）；``cancel`` = 忽略。"""
        item = self.store.get_proposal(proposal_id)
        if item is None:
            return False, "找不到这条提议"
        if action == "cancel":
            self.store.set_proposal(proposal_id, state=STATE_SKIPPED,
                                    decided_at=datetime.now().timestamp())
            self._drop(proposal_id)
            self._report(True, "已忽略")
            return True, "已忽略"
        if action != "send":
            return False, f"不认识的动作：{action}"
        if self.add_event is None:
            return False, "日程接口没接上（只在服务里可用）"
        try:
            event_id = self.add_event(item)
        except Exception as exc:  # noqa: BLE001 - 写日程失败要能说出来
            self.log.warning(f"[mail] 加入日程失败：{exc}")
            self._report(False, f"加入日程失败：{exc}")
            return False, f"加入日程失败：{exc}"
        self.store.set_proposal(proposal_id, state=STATE_ACCEPTED,
                                decided_at=datetime.now().timestamp(),
                                event_id=int(event_id or 0))
        self._drop(proposal_id)
        self._report(True, "已加进日程")
        return True, "已加进日程"

    def _drop(self, proposal_id: str) -> None:
        if proposal_id in self._queue:
            self._queue.remove(proposal_id)

    def _report(self, ok: bool, message: str) -> None:
        (self.log.info if ok else self.log.warning)(f"[mail] 日程提议：{message}")
        if self.on_result is not None:
            try:
                self.on_result(ok, message)
            except Exception:  # noqa: BLE001
                pass
