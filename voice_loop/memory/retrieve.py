"""检索：既要「按内容找」，也要「按时间找」。

用户要的那句原话是：**「过去【时间】中我们对于【事情】的讨论」** —— 所以有两条路：

    ① 时间过滤  把候选先卡到一个时间窗里（`parse_when` 认中文：昨天/上周/上周三/最近 7 天…）
    ② 内容排序  剩下的按「主题重合 + 有效重要度 + 被用过 + 新鲜度」打分

★为什么不能只做向量/关键词相似度★：记忆检索的答案常常取决于**什么时候**，
「上次组会」和「上上次组会」的主题一模一样，区别只在时间。

打分权重都摊在下面（`W_*`），调的时候一眼能看见；每条结果带 `why`，
所以出现「它怎么没想起来」时，可以把它拆开看哪一项拖了后腿。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from .levels import keywords_of, parse_ts, relevance, salience_eff, tokenize
from .model import Chunk, Episode, Fact, Hit

W_TOPIC = 1.6          # 主题重合（主项）
W_SALIENCE = 1.0       # 有效重要度
W_RECALL = 0.4         # 被想起过的次数（越用越牢）
W_FRESH = 0.3          # 新鲜度（近期的小加分）
W_CONFIDENCE = 0.5     # 事实/摘要的可信度
W_PINNED = 0.6         # 身份类事实的基本盘

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DAYS_AGO = {"大前天": 3, "前天": 2, "昨天": 1, "今日": 0, "今天": 0, "那天": None}


def _day_start(moment: datetime) -> datetime:
    return datetime.combine(moment.date(), time.min)


def parse_when(text: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """把「上周三」「最近三天」「上个月」解析成时间窗（左闭右开/含末日）。

    ★认不出来就返回 None★（= 不做时间过滤），而不是猜一个窗口 ——
    猜错的时间窗会**静默地**把正确答案排除掉，比不筛更糟。
    """
    moment = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        return None

    # 最近 / 过去 N 天
    found = re.search(r"(?:最近|过去|近)\s*([0-9一二三四五六七八九十两]{1,3})\s*(天|日|周|星期|月)", raw)
    if found:
        amount = _cn_int(found.group(1))
        unit = found.group(2)
        if amount:
            if unit in ("天", "日"):
                return _day_start(moment - timedelta(days=amount - 1)), moment
            if unit in ("周", "星期"):
                return _day_start(moment - timedelta(days=amount * 7 - 1)), moment
            return _day_start(moment - timedelta(days=amount * 30)), moment

    for word, days in _DAYS_AGO.items():
        if word in raw and days is not None:
            start = _day_start(moment - timedelta(days=days))
            return start, start + timedelta(days=1)

    # 上/这/本周 + 可选的星期几
    found = re.search(r"(上上|上|这|本|当)\s*(?:个)?\s*(周|星期)([一二三四五六日天])?", raw)
    if found:
        which, _, weekday = found.group(1), found.group(2), found.group(3)
        monday = _day_start(moment) - timedelta(days=moment.weekday())
        if which in ("上", "上上"):
            monday -= timedelta(days=7 if which == "上" else 14)
        if weekday:
            start = monday + timedelta(days=_WEEKDAY[weekday])
            return start, start + timedelta(days=1)
        return monday, monday + timedelta(days=7)

    # 上/这个月
    found = re.search(r"(上上|上|这|本)\s*(?:个)?\s*月", raw)
    if found:
        first = _day_start(moment).replace(day=1)
        if found.group(1) in ("上", "上上"):
            for _ in range(1 if found.group(1) == "上" else 2):
                first = (first - timedelta(days=1)).replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt

    # 去年 / 今年
    found = re.search(r"(前年|去年|今年|明年)", raw)
    if found:
        year = moment.year + {"去年": -1, "前年": -2, "今年": 0, "明年": 1}[found.group(1)]
        return datetime(year, 1, 1), datetime(year + 1, 1, 1)

    # 认不出来
    return None


def _cn_int(text: str) -> int | None:
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10}
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in digits:
        return digits[text]
    if text.startswith("十"):
        return 10 + digits.get(text[1:], 0)
    if "十" in text:
        head, _, tail = text.partition("十")
        return digits.get(head, 0) * 10 + digits.get(tail, 0)
    return None


def _freshness(ts: str, now: datetime, half_life_days: float = 30.0) -> float:
    got = parse_ts(ts)
    if got is None:
        return 0.0
    age = max(0.0, (now - got).total_seconds() / 86400.0)
    return 0.5 ** (age / half_life_days) if half_life_days > 0 else 0.0


def in_window(ts: str, window: tuple[datetime, datetime] | None) -> bool:
    """时间戳是否落在窗口里（窗口为 None 时一律通过）。"""
    if window is None:
        return True
    got = parse_ts(ts)
    if got is None:
        return False
    return window[0] <= got < window[1]


def score_episode(ep: Episode, query_tokens: set[str], now: datetime,
                  window: tuple[datetime, datetime] | None = None) -> tuple[float, dict[str, float]]:
    """给一条情景记忆打分；返回（总分, 拆解）。"""
    topic = relevance(query_tokens, tokenize(
        " ".join(ep.keywords) + " " + ep.title + " " + ep.summary + " " + " ".join(ep.who)))
    sal = salience_eff(ep, now)
    recall = min(ep.recalls, 5) / 5.0
    fresh = _freshness(ep.ts, now)
    # ★给了时间窗就是在做时间检索★：主题完全不通也要留一点分（用户可能只记得时间）
    if window is not None and in_window(ep.ts, window):
        fresh = max(fresh, 0.35)
    why = {"topic": topic, "salience": sal, "recall": recall, "fresh": fresh}
    total = (W_TOPIC * topic + W_SALIENCE * sal + W_RECALL * recall + W_FRESH * fresh) \
        * max(0.2, ep.confidence)
    return total, why


def score_fact(fact: Fact, query_tokens: set[str], now: datetime) -> tuple[float, dict[str, float]]:
    """给一条事实打分：命中 key 或 value 就算相关（事实很短，重合度天然低）。"""
    tokens = tokenize(fact.key.replace(".", " ") + " " + fact.value)
    topic = relevance(query_tokens, tokens)
    if not topic:
        topic = 0.5 * relevance(query_tokens, tokenize(fact.value))
    eff = fact.confidence if fact.pinned else _freshness(fact.last_seen, now, 60.0) * fact.confidence
    why = {"topic": topic, "confidence": eff, "pinned": 1.0 if fact.pinned else 0.0}
    total = W_TOPIC * topic + W_CONFIDENCE * eff + (W_PINNED if fact.pinned else 0.0)
    return total, why


def score_chunk(chunk: Chunk, query_tokens: set[str], now: datetime) -> tuple[float, dict[str, float]]:
    """给知识库的一段打分（标题权重高一点：标题是人写的索引）。"""
    body = relevance(query_tokens, tokenize(chunk.text))
    head = relevance(query_tokens, tokenize(chunk.title + " " + " ".join(chunk.keywords)))
    why = {"body": body, "title": head}
    return W_TOPIC * (body + 1.5 * head), why


def rank(hits: list[Hit], limit: int = 8, min_score: float = 0.12) -> list[Hit]:
    """统一的收口：按分数排序、去掉太弱的、去重（同样一句话只留最高分那条）。"""
    kept: list[Hit] = []
    seen: set[str] = set()
    for hit in sorted(hits, key=lambda h: h.score, reverse=True):
        if hit.score < min_score:
            continue
        key = hit.text[:40]
        if key in seen:
            continue
        seen.add(key)
        kept.append(hit)
        if len(kept) >= limit:
            break
    return kept


def keywords_of_query(query: str) -> list[str]:
    return keywords_of(query)
