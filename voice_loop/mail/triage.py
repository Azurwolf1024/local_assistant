"""重要度分级：**规则先、模型后、反馈可学**。

为什么不是「全交给模型」
    模型对「这封重不重要」的判断力不错，但**不稳定也不可解释**：同一封邮件换个模型、
    换个温度就可能从「重要」变「一般」。而漏掉一封导师的截止通知，代价远大于多看几条噪音。
    所以：

    1. **规则层**先给一个可解释的底分（VIP 发件人、只发给你、截止词、offer/面试、发票…），
       这些事实不需要模型也能判定，而且你能在配置里改；
    2. **模型层**只看「主题 + 发件人 + 前 N 字」（默认 400 字，★正文不出本机★），
       给出分数/类别/是否要回/截止时间/一句话摘要；
    3. 两者合并：**模型可以抬高，也可以在明确判定为群发/促销时压低**，
       但不能把「VIP 发件人」判成不重要（规则兜底）。
    4. 结果按 ``(message_id, model)`` 缓存 —— 同一封只算一次，模型换了才重算。

分级阈值（``[mail] high_score / medium_score``，默认 60 / 30）：
    ``high``   立刻提醒（弹窗）
    ``medium`` 进摘要（每天两次）
    ``low``    静默入库，只可检索
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

#: 类别 → 中文（也用来判断「群发/促销」这类可以压分的类别）
CATEGORIES = {
    "deadline": "截止/待办",
    "interview": "面试/录用",
    "money": "钱（发票/报销/账单）",
    "school": "学业",
    "meeting": "会议/日程",
    "personal": "私人",
    "notification": "系统通知",
    "newsletter": "订阅/群发",
    "promotion": "促销",
    "other": "其它",
}

_BULK_CATEGORIES = ("newsletter", "promotion", "notification")

#: 关键词 → (加分, 类别)。★顺序有意义★：先匹配到的类别更容易被采纳。
KEYWORDS: tuple[tuple[str, int, str], ...] = (
    (r"截止|deadline|ddl|due\s*date|最后期限|务必|过期", 25, "deadline"),
    (r"面试|interview|录用|offer|offer\s*letter|录取|admission|笔试|二面", 30, "interview"),
    (r"合同|contract|发票|invoice|报销|reimburse|账单|bill|缴费|退款|refund", 20, "money"),
    (r"退课|选课|成绩|grade|答辩|defen[cs]e|论文|thesis|导师|supervisor|课程|考试|exam", 15, "school"),
    (r"会议|meeting|日程|schedule|邀请|invit|calendar|组会|例会", 12, "meeting"),
    (r"紧急|urgent|asap|尽快|马上|今天之内|明天之前", 12, "deadline"),
)

#: 明显是噪音的信号（减分）
_NOISE = re.compile(
    r"unsubscribe|退订|newsletter|促销|优惠|折扣|限时|广告|no-?reply|do-?not-?reply"
    r"|秒杀|抽奖|积分|会员日",
    re.IGNORECASE,
)


def _addr_set(items: Any) -> set[str]:
    out: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            out.add(item.lower())
        elif isinstance(item, dict):
            address = str(item.get("address") or "").lower()
            if address:
                out.add(address)
    return out


def domain_of(address: str) -> str:
    addr = str(address or "").lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


class Triage:
    """打分器。``llm`` 是 ``Callable[[str, str], str]``（system, user）→ 文本 JSON。"""

    def __init__(
        self,
        cfg: Any,
        store: Any = None,
        *,
        log: logging.Logger | None = None,
        llm: Callable[[str, str], str] | None = None,
        me: set[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.log = log or logging.getLogger("voice_loop.mail")
        self.llm = llm
        self.me = {a.lower() for a in (me or set()) if a}

    # ---------------------------------------------------------------- 配置面
    @property
    def high_score(self) -> int:
        return int(getattr(self.cfg, "high_score", 60) or 60)

    @property
    def medium_score(self) -> int:
        return int(getattr(self.cfg, "medium_score", 30) or 30)

    @property
    def vip(self) -> set[str]:
        return {str(x).strip().lower() for x in (getattr(self.cfg, "vip_senders", []) or [])
                if str(x).strip()}

    @property
    def priority_domains(self) -> set[str]:
        return {str(x).strip().lower() for x in (getattr(self.cfg, "priority_domains", []) or [])
                if str(x).strip()}

    @property
    def classify_chars(self) -> int:
        return max(0, int(getattr(self.cfg, "classify_chars", 400) or 0))

    @property
    def model(self) -> str:
        return str(getattr(self.cfg, "classify_model", "") or "")

    # ---------------------------------------------------------------- 规则层
    def rules(self, msg: dict) -> dict[str, Any]:
        """纯规则打分（不联网、可解释）。"""
        score = 0
        reasons: list[str] = []
        category = "other"

        sender = str(msg.get("sender") or "").lower()
        sender_domain = domain_of(sender)
        subject = str(msg.get("subject") or "")
        preview = str(msg.get("preview") or "")
        blob = f"{subject}\n{preview}"

        if sender and (sender in self.vip or sender_domain in self.vip):
            score += 40
            reasons.append("VIP 发件人")
        if sender_domain and sender_domain in self.priority_domains:
            score += 15
            reasons.append(f"重要域名 {sender_domain}")

        to = _addr_set(msg.get("to"))
        cc = _addr_set(msg.get("cc"))
        if self.me and to & self.me and not (cc & self.me):
            score += 10
            reasons.append("直接发给你")

        for pattern, weight, cat in KEYWORDS:
            if re.search(pattern, blob, re.IGNORECASE):
                score += weight
                reasons.append(f"命中「{re.search(pattern, blob, re.IGNORECASE).group(0)}」")
                if category == "other":
                    category = cat
                break

        if str(msg.get("importance") or "").lower() == "high":
            score += 20
            reasons.append("发件人标了高重要性")
        if msg.get("has_attachments"):
            score += 5
            reasons.append("带附件")
        if re.match(r"^(回复|答复|re:|fw:|fwd:|转发)", subject.strip(), re.IGNORECASE):
            score += 8
            reasons.append("是一封回复/转发")
        if msg.get("flagged"):
            score += 10
            reasons.append("被你加了旗标")

        if _NOISE.search(blob) or _NOISE.search(sender):
            score -= 30
            category = "newsletter" if category == "other" else category
            reasons.append("像群发/促销（减分）")

        score = max(0, min(100, score))
        return {"score": score, "category": category, "reasons": reasons}

    # ---------------------------------------------------------------- 模型层
    def _prompt(self, msg: dict) -> tuple[str, str]:
        subject = str(msg.get("subject") or "")[:120]
        sender = f"{msg.get('sender_name') or ''} <{msg.get('sender') or ''}>".strip()
        body = str(msg.get("preview") or "")[: self.classify_chars]
        system = (
            "你是邮件分诊器。只输出一个 JSON 对象，不要解释、不要 markdown。字段：\n"
            '{"important":0-100,"category":"deadline|interview|money|school|meeting|'
            'personal|notification|newsletter|promotion|other","needs_reply":true/false,'
            '"deadline":"YYYY-MM-DD 或 空串","one_line":"不超过 20 字的中文摘要"}\n'
            "判定标准：需要你采取行动、有明确时限、来自真人上级/老师/客户 → 高分；"
            "群发通知、订阅、促销 → 低分。"
        )
        user = f"发件人：{sender}\n主题：{subject}\n正文开头：{body}"
        return system, user

    def _ask_llm(self, msg: dict) -> dict | None:
        if self.llm is None:
            return None
        system, user = self._prompt(msg)
        try:
            raw = self.llm(system, user)
        except Exception as exc:  # noqa: BLE001 - 分类失败不能挡住收信
            self.log.warning(f"[mail] 分级模型调用失败（只用规则）：{exc}")
            return None
        text = str(raw or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            self.log.warning("[mail] 分级模型没给 JSON（只用规则）")
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            self.log.warning("[mail] 分级模型的 JSON 解析失败（只用规则）")
            return None
        if not isinstance(data, dict):
            return None
        try:
            score = int(float(data.get("important") or 0))
        except (TypeError, ValueError):
            score = 0
        return {
            "score": max(0, min(100, score)),
            "category": str(data.get("category") or "other"),
            "needs_reply": bool(data.get("needs_reply")),
            "deadline": str(data.get("deadline") or "")[:32],
            "one_line": str(data.get("one_line") or "")[:60],
        }

    # ---------------------------------------------------------------- 合并
    def classify(self, msg: dict, *, use_cache: bool = True) -> dict[str, Any]:
        """返回 ``{score, level, category, needs_reply, deadline, one_line, reasons, model}``。"""
        message_id = str(msg.get("id") or "")
        cached = self._cached(message_id)
        if use_cache and cached is not None:
            return cached

        base = self.rules(msg)
        score = base["score"]
        category = base["category"]
        needs_reply = False
        deadline = ""
        one_line = ""
        model_used = ""
        reasons = list(base["reasons"])

        llm = self._ask_llm(msg)
        if llm is not None:
            model_used = self.model or "llm"
            # 模型能抬高；但如果它明确说是群发/促销，且规则分不高 → 压下来
            if llm["category"] in _BULK_CATEGORIES and base["score"] < 50:
                score = min(llm["score"], 25)
                reasons.append("模型判定为群发/通知")
            else:
                score = max(score, llm["score"])
            category = llm["category"] if llm["category"] != "other" else category
            needs_reply = bool(llm["needs_reply"])
            deadline = llm["deadline"]
            one_line = llm["one_line"]

        info = {
            "score": score,
            "level": self.level_of(score),
            "category": category,
            "needs_reply": needs_reply,
            "deadline": deadline,
            "one_line": one_line or (str(msg.get("subject") or "")[:40]),
            "reasons": reasons,
            "model": model_used,
        }
        if self.store is not None and message_id:
            # 只缓存「模型真实跑过」的结果；纯规则结果每次都现算（改配置能立刻生效）
            if model_used:
                self.store.set_triage(message_id, info)
            else:
                self.store.set_triage(message_id, info)
        return info

    def _cached(self, message_id: str) -> dict | None:
        if self.store is None or not message_id:
            return None
        row = self.store.get(message_id)
        if not row or not row.get("triaged_at"):
            return None
        # 换了模型就重算
        if self.model and row.get("triaged_model") not in ("", self.model):
            return None
        return {
            "score": int(row.get("score") or 0),
            "level": str(row.get("level") or ""),
            "category": str(row.get("category") or ""),
            "needs_reply": bool(row.get("needs_reply")),
            "deadline": str(row.get("deadline") or ""),
            "one_line": str(row.get("one_line") or ""),
            "reasons": ["（缓存）"],
            "model": str(row.get("triaged_model") or ""),
        }

    def level_of(self, score: int) -> str:
        if score >= self.high_score:
            return "high"
        if score >= self.medium_score:
            return "medium"
        return "low"

    def classify_many(self, rows: list[dict], *, limit: int = 20) -> list[dict]:
        """给一批新邮件打标（巡检每轮用；``limit`` 限制模型调用次数）。"""
        out = []
        for msg in rows[:limit]:
            info = self.classify(msg)
            msg = dict(msg)
            msg["triage"] = info
            out.append(msg)
        return out
