"""从对话里提出「值得记住的东西」——L1 → L2 / L3 的那一步。

★什么会被记住★（有意保守：什么都记 = 什么都找不到）：

| 触发 | 变成 | 为什么 |
|---|---|---|
| 「记住…」「别忘了…」 | Episode(salience 0.9) | 用户明确要求 |
| 「说好了…」「我决定…」 | Episode(decision/promise, 0.7) | 决定和承诺是关系的骨架 |
| 「我喜欢/我讨厌 X」 | Fact + Episode(0.6) | 偏好反复用到，落成事实 |
| 「我叫 X」「我在 Y 上班」 | Fact（user 类） | 身份类信息 |
| 日程/提醒/备忘被真的写进去了 | Episode(kind=event) | ★由 pipeline 以 hint 形式传进来★ |
| 「我最近很累」这类感受 | Episode(feeling, 0.5) | 记的是状态，不是事实 |

其余闲聊**不进 L2** —— 它们留在原始对话里（L1），靠滑动窗口自然老去。

## 为什么总结不强制走模型

`summary` 首选 LLM（更像人话、更能抓住重点），但**模型不在线时必须能降级**：
本地模型没起、或超时，就用规则摘要（用户那句 + 去掉语气词）。
★记忆不能因为模型没开就断档★ —— 这是「自清洁」之外的另一条底线：
宁可摘要糙一点，也不能把事丢了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .levels import keywords_of, parse_ts
from .model import Episode, Fact, now_iso

# --- 信号词（中文口语，宁可漏也别滥）--------------------------------------- #
REMEMBER_WORDS = ("记住", "记一下", "记下", "别忘了", "别忘", "帮我记", "要记得", "提醒我记住")
DECISION_WORDS = ("说好", "说好了", "决定", "定了", "答应", "约好", "就这么定", "商量好")
FEELING_WORDS = ("很累", "好累", "开心", "难过", "烦", "焦虑", "害怕", "紧张", "压力大", "睡不着")
PREFERENCE_WORDS = ("我喜欢", "我愛", "我爱", "我不喜欢", "我讨厌", "我最喜欢", "我不吃", "我不喝",
                    "我习惯", "我一般", "我通常", "我从来不")

# --- 事实抽取的正则（都是「我」的第一人称，别扩太宽）------------------------ #
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"我(?:的)?名字(?:是|叫)\s*([^\s，。！？,!.?]{1,12})"), "user.名字", "user"),
    (re.compile(r"叫我\s*([^\s，。！？,!.?]{1,12})"), "user.称呼", "user"),
    (re.compile(r"我(?:在|是在)\s*([^\s，。！？,!.?]{1,18}?)(?:上班|工作|读书|上学|念书)"), "user.工作", "user"),
    (re.compile(r"我(?:住在|住在|家在)\s*([^\s，。！？,!.?]{1,18})"), "user.住处", "user"),
    (re.compile(r"我(?:最近)?(?:很|挺|比较)?(喜欢|爱|不喜欢|讨厌|最怕|不吃|不喝|习惯)\s*([^\s，。！？,!.?]{1,14})"),
     "user.喜好", "user"),
]

# 喜好后面常常跟着一个泛动词（「喜欢**喝**拿铁」）—— 剥掉它，钥匙才干净（user.喜好.拿铁）
_LEADING_VERB = re.compile(r"^(?:喝|吃|玩|看|听|用|穿|收藏|养)\s*")


@dataclass
class Turn:
    """一行原始对话（对应 pipeline 写的 TurnStats，取我们关心的三个字段）。"""

    turn: int = 0
    user: str = ""
    answer: str = ""


@dataclass
class Signals:
    """一句话里的「值得记」信号。"""

    important: bool = False
    kinds: set[str] = field(default_factory=set)
    salience: float = 0.45
    keywords: list[str] = field(default_factory=list)


def read_session(path: str | Path) -> list[Turn]:
    """读原始对话文件（pipeline 写的是 TurnStats 的 JSON 行）。

    ★宽容解析★：字段名变了、行坏了都只跳过，不要让记忆归档因为一个坏行整个失败。
    """
    turns: list[Turn] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return turns
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        user = str(raw.get("user_text") or raw.get("user") or "").strip()
        answer = str(raw.get("answer") or raw.get("reply") or "").strip()
        if not user and not answer:
            continue
        turns.append(Turn(turn=int(raw.get("turn") or len(turns) + 1), user=user, answer=answer))
    return turns


def signals(text: str) -> Signals:
    """扫一遍信号词，决定这句话值不值得进 L2。"""
    got = Signals(keywords=keywords_of(text, prefer=text[:40]))
    if not text.strip():
        return got
    if any(word in text for word in REMEMBER_WORDS):
        got.important = True
        got.kinds.add("talk")
        got.salience = 0.9
    if any(word in text for word in DECISION_WORDS):
        got.important = True
        got.kinds.add("decision")
        got.salience = max(got.salience, 0.7)
    if any(word in text for word in FEELING_WORDS):
        got.important = True
        got.kinds.add("feeling")
        got.salience = max(got.salience, 0.5)
    if any(word in text for word in PREFERENCE_WORDS) or facts_from_text(text):
        got.important = True
        got.kinds.add("preference")
        got.salience = max(got.salience, 0.6)
    return got


def facts_from_text(text: str, source: str = "") -> list[Fact]:
    """规则抽事实（L3）。只有明确的第一人称陈述才抽 —— 猜出来的「事实」比没有更糟。"""
    out: list[Fact] = []
    for pattern, key, about in _PATTERNS:
        found = pattern.search(text or "")
        if not found:
            continue
        if key == "user.喜好":
            verb, value = found.group(1), found.group(2)
            noun = _LEADING_VERB.sub("", value).strip() or value
            out.append(Fact(key=f"user.喜好.{noun}", value=f"{verb}{value}", about=about,
                            confidence=0.6, sources=[source] if source else [],
                            note=f"原话：{text.strip()[:60]}"))
        else:
            value = found.group(1).strip()
            if value and value not in {"谁", "什么", "哪儿", "哪里"}:
                out.append(Fact(key=key, value=value, about=about, confidence=0.7,
                                sources=[source] if source else [],
                                note=f"原话：{text.strip()[:60]}"))
    return out


def rule_summary(turn: Turn, limit: int = 60) -> tuple[str, str, float]:
    """模型不可用时的摘要：标题=用户那句掐头，摘要=「你说了…/我答了…」。

    置信度给 0.6：它是**原文的裁剪**，不是提炼 —— 检索时够用，但不该跟 LLM 摘要同权。
    """
    user = " ".join((turn.user or "").split())
    if not user:
        user = " ".join((turn.answer or "").split())
    title = user[:limit] + ("…" if len(user) > limit else "")
    summary = f"你说：{user[:120]}"
    if turn.answer:
        summary += f"；我答：{' '.join(turn.answer.split())[:120]}"
    return title, summary, 0.6


def llm_summary(call: Callable[[str], str] | None, turn: Turn) -> tuple[str, str, float] | None:
    """用模型总结一次（`call` 是「给提示词、还文本」的函数，由上层注入）。

    ★任何异常都返回 None★：记忆归档绝不能因为模型超时/没起来而失败或卡住。
    """
    if call is None or not (turn.user or "").strip():
        return None
    prompt = (
        "把下面这段对话压缩成一条「记忆条目」，给将来检索用。只输出两行：\n"
        "第一行：不超过 18 字的标题（谁、什么事）；\n"
        "第二行：不超过 60 字的事实性摘要（去掉寒暄和语气词，保留时间/人名/数字）。\n"
        "不要评价，不要添加原文没有的信息。\n\n"
        f"用户：{turn.user}\n助手：{turn.answer}\n"
    )
    try:
        text = (call(prompt) or "").strip()
    except Exception:  # noqa: BLE001 - 模型不可用不是错误
        return None
    lines = [ln.strip(" -·") for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    return lines[0][:40], lines[1][:200], 0.85


def episodes_from_session(turns: Iterable[Turn], source: str, now: datetime | None = None,
                          llm_call: Callable[[str], str] | None = None,
                          hints: Iterable[dict] | None = None) -> tuple[list[Episode], list[Fact]]:
    """一段原始对话 → 若干 Episode + Fact。

    ``hints``：pipeline 传进来的「确实发生了的事」（写了日程/提醒/备忘），
    它们不靠关键词猜，直接落成 kind=event 的高置信度条目（并带 `event_id`，
    这样「记忆」和「日程」是关联的、不是两份互相不知道的东西）。
    """
    stamp = now or datetime.now()
    episodes: list[Episode] = []
    facts: list[Fact] = []

    for hint in hints or []:
        episodes.append(Episode(
            ts=str(hint.get("ts") or now_iso()), kind=str(hint.get("kind") or "event"),
            title=str(hint.get("title") or "")[:80], summary=str(hint.get("summary") or "")[:400],
            detail=str(hint.get("detail") or "")[:600],
            keywords=list(hint.get("keywords") or []) or keywords_of(
                str(hint.get("title") or ""), prefer=str(hint.get("title") or "")),
            salience=float(hint.get("salience") or 0.75), confidence=1.0,
            source=source, turn=int(hint.get("turn") or 0),
            event_id=hint.get("event_id"),
        ))

    for turn in turns:
        got = signals(turn.user)
        facts.extend(facts_from_text(turn.user, source=source))
        if not got.important:
            continue
        kind = sorted(got.kinds)[0] if got.kinds else "talk"
        summary = llm_summary(llm_call, turn)
        if summary is None:
            title, text, confidence = rule_summary(turn)
        else:
            title, text, confidence = summary
        episodes.append(Episode(
            ts=stamp.replace(microsecond=0).isoformat(sep=" "), kind=kind,
            title=title, summary=text, detail=" ".join((turn.user or "").split())[:600],
            keywords=got.keywords, salience=got.salience, confidence=confidence,
            source=source, turn=turn.turn,
        ))
    return episodes, facts
