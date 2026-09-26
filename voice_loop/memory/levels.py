"""四级记忆的**自清洁**规则——「有生命力」就在这几个纯函数里。

设计原则（都是刻意的，别改成玄学）：

1. ★重要度会衰减，但「用过」会回血★：一件三个月前的琐事会自动退到边缘，
   而一条被反复检索到的记忆越用越牢（`recalls` 加回血）。这就是「活着」的意思：
   记忆的权重由**使用**决定，不是写进去就永远一样重。
2. ★只衰减「保留优先级」，不改写事实★：Episode/Fact 的内容不动，动的是
   `salience`/`confidence` —— 所以「查出什么」永远可复现，只有「先想起什么」在变。
3. ★删除只有两种理由★：超出条数上限（按分数淘汰），或原始对话超过了滑动窗口。
   绝不因为「太旧」直接删事——老人也会记得小时候的事，只是不常想起来。
4. ★清原始对话前必须确认它已经归档过★（见 store 里 `sessions.json`）：
   否则会出现「原始对话删了、事件也没提出来」的信息黑洞。

这些函数都是**纯函数**（输入输出都是 dataclass/字典，不碰文件、不看全局），
所以 `scripts/test_memory.py` 能拿假数据把每条规则钉住。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from .model import Episode, Fact

# --- 调参都在明面上 --------------------------------------------------------- #
EPISODE_HALF_LIFE_DAYS = 60.0     # 事件重要度的半衰期
FACT_HALF_LIFE_DAYS = 45.0        # 事实置信度的半衰期（pinned 不衰减）
RECALL_BONUS = 0.08               # 每被想起一次加多少权重（封顶 0.4）
RECALL_BONUS_CAP = 0.4
MERGE_TOPIC_OVERLAP = 0.3          # 关键词覆盖度超过它就合并（同一时段内）
MERGE_MIN_SHARED = 2               # 且「非通用词」至少共这么多（只共一个「下午」不算）
MERGE_WINDOW_HOURS = 36           # 合并只在同一段时间窗内做（隔天的同类不合并）
PROMOTE_MIN_EVIDENCE = 3          # 同类事件出现这么多次 → 上升为一条「事实」
FACT_MIN_CONFIDENCE = 0.15        # 低于它的事实被清掉（pinned 除外）
DETAIL_KEEP_DAYS = 45             # 超过这么久、又不重要的事件，丢掉原文只留摘要
DETAIL_KEEP_SALIENCE = 0.55       # 重要度高于它的事件，原文保留得久一些

_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "有", "和", "就", "都", "也",
    "不", "没", "很", "会", "要", "把", "被", "给", "到", "对", "把", "着", "过", "吧",
    "呢", "啊", "呀", "吗", "什么", "怎么", "这个", "那个", "我们", "他们", "一下",
    "可以", "已经", "还是", "但是", "因为", "所以", "如果", "然后", "现在", "今天",
}
_ASCII_WORD = re.compile(r"[0-9A-Za-z_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------- #
# 分词 / 相似度（不引入新依赖：中文用字符二元组，英文数字用词）
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> set[str]:
    """把一句话切成检索用的 token。

    ★为什么不用 jieba★：本项目的运行环境（系统 Python 3.13）没装它，
    而「自清洁/检索」不值得为了分词多背一个依赖。中文用**字符二元组**
    对短句足够稳（「日程」「机器学习课」都能抓到），英文数字按词切。
    """
    text = (text or "").lower()
    tokens = {w for w in _ASCII_WORD.findall(text)}
    cjk = [ch for ch in text if _CJK.match(ch)]
    for i in range(len(cjk) - 1):
        bigram = cjk[i] + cjk[i + 1]
        if bigram not in _STOPWORDS:
            tokens.add(bigram)
    # 单个汉字也留一点信号，但去掉虚词（长句里它们会互相污染）
    tokens |= {ch for ch in cjk if ch not in _STOPWORDS}
    return {t for t in tokens if t not in _STOPWORDS}


def overlap(a: set[str], b: set[str]) -> float:
    """重合度（Jaccard，对称）。两边都为空算 0 —— 空对空不该算「一模一样」。"""
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def coverage(query: set[str], doc: set[str]) -> float:
    """查询被文档覆盖的比例（不对称，★检索要看这个★）。

    Jaccard 会把「长记忆」罚得很惨：一句话查询去撞一条四句话的记忆，
    重合 5 个词也只剩 0.15 —— 于是「它怎么没想起来」就发生了。
    但纯 coverage 又太宽（一个字查询能「完全覆盖」一堆文档）→ 两者折中见 `relevance`。
    """
    if not query:
        return 0.0
    return len(query & doc) / float(len(query))


def relevance(query: set[str], doc: set[str]) -> float:
    """检索用的相关度 = 覆盖度与 Jaccard 各一半（都试过，这个最稳）。"""
    return 0.5 * coverage(query, doc) + 0.5 * overlap(query, doc)


def keywords_of(text: str, limit: int = 8, prefer: str = "") -> list[str]:
    """抽关键词：★先取标题里出现过的★，再按长度排。

    以前只按「长度 + 字母序」取前 8 个 —— 结果是**任意**的：同一件事的两条记录
    可能一个共享词都没选进来（实测「组会」两条记录合并失败就是这个原因）。
    标题是作者自己写的主题，优先信它。
    """
    head = tokenize(prefer) if prefer else set()
    tokens = sorted(tokenize(text), key=lambda t: (t not in head, -len(t), t))
    out: list[str] = []
    for tok in tokens:
        if len(tok) >= 2:
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def parse_ts(value: str) -> datetime | None:
    """宽容地解析时间戳（记忆文件是人也能改的，别因为格式怪就崩）。"""
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    # 退回按长度截：'2026-09-26 12:00:00' / '... 12:00' / '2026-09-26'
    for length, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(text[:length].strip(), fmt)
        except ValueError:
            continue
    return None


def _age_days(ts: str, now: datetime) -> float:
    got = parse_ts(ts)
    if got is None:
        return 0.0
    return max(0.0, (now - got).total_seconds() / 86400.0)


def _half_life_factor(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / half_life_days)


# --------------------------------------------------------------------------- #
# L2 情景记忆
# --------------------------------------------------------------------------- #
def salience_eff(episode: Episode, now: datetime) -> float:
    """事件现在的有效重要度 = 写入时的重要度 × 时间衰减 + 被想起的回血。"""
    base = max(0.0, min(1.0, episode.salience))
    decayed = base * _half_life_factor(_age_days(episode.ts, now), EPISODE_HALF_LIFE_DAYS)
    bonus = min(RECALL_BONUS_CAP, RECALL_BONUS * max(0, episode.recalls))
    # 回血不能把「不重要」抬成「重要」：封顶到 0.9，且以 pinned 之外的最高 1.0 为界
    return max(0.0, min(1.0, decayed + bonus * (1.0 - decayed)))


def topics_overlap(a: Episode, b: Episode) -> float:
    """两件事的「主题」重合度：★只看关键词、取两个方向里大的那个★。

    为什么不拿摘要全文算：全文里单字 token 太多（「会/点/下/午」），
    同一件事换个说法（「改到下午两点」vs「确认是下午两点开始」）字面重合会被稀释到 0.25，
    合并永远不触发（实测就是这个坑）。关键词是抽过的主题词，信噪比高得多。
    """
    return max(coverage(set(a.keywords), set(b.keywords)),
               coverage(set(b.keywords), set(a.keywords)))


# 这些词太常见，不能当「同一件事」的证据（「下午」到处都有）
_GENERIC_TOPICS = {"今天", "今日", "明天", "昨天", "后天", "下午", "上午", "早上", "晚上",
                   "时间", "时候", "事情", "东西", "安排", "以后", "下次", "最近"}


def shared_topics(a: Episode, b: Episode) -> int:
    """共同关键词里「**非通用词**」的个数：合并至少要 2 个。

    ★通用词会互相污染★：「下午」+「今天」就判同一件事，会把无关的日程糊成一团。
    """
    shared = (set(a.keywords) & set(b.keywords)) - _GENERIC_TOPICS
    return len(shared)


def _close_in_time(a: Episode, b: Episode, hours: int = MERGE_WINDOW_HOURS) -> bool:
    ta, tb = parse_ts(a.ts), parse_ts(b.ts)
    if ta is None or tb is None:
        return False
    return abs((ta - tb).total_seconds()) <= hours * 3600


def merge_episodes(episodes: list[Episode]) -> tuple[list[Episode], int]:
    """把「同一时段、同一主题」的重复事件合成一条（自清洁第一招）。

    典型来源：同一次组会聊了三轮各记一条。合并时：
      - `merged` 计数 +1（次数本身就是信息：「这事提过三回」）
      - 取更长的摘要（信息多的那个），关键词取并集
      - 重要度取更高的那个（不能因为合并把重要的事冲淡）
      - `links` 记下被合并的 id（想追原始条目还能追）

    返回（新列表, 合并掉几条）。
    """
    kept: list[Episode] = []
    merged_count = 0
    for ep in sorted(episodes, key=lambda e: e.ts):
        target = None
        for cand in kept:
            if cand.kind == ep.kind and _close_in_time(cand, ep) and \
                    topics_overlap(cand, ep) >= MERGE_TOPIC_OVERLAP and \
                    shared_topics(cand, ep) >= MERGE_MIN_SHARED:
                target = cand
                break
        if target is None:
            kept.append(ep)
            continue
        if len(ep.summary) > len(target.summary):
            target.summary = ep.summary
        if len(ep.title) > len(target.title):
            target.title = ep.title
        target.keywords = sorted(set(target.keywords) | set(ep.keywords))[:12]
        target.salience = max(target.salience, ep.salience)
        target.confidence = min(target.confidence, ep.confidence)
        target.merged += ep.merged
        target.links = sorted(set(target.links) | {ep.id} | set(ep.links))[:20]
        if ep.event_id and not target.event_id:
            target.event_id = ep.event_id
        merged_count += 1
    return kept, merged_count


def compact_episodes(episodes: list[Episode], now: datetime,
                     keep_days: float = DETAIL_KEEP_DAYS,
                     keep_salience: float = DETAIL_KEEP_SALIENCE) -> int:
    """旧事丢原文、只留摘要（自清洁第二招）。返回压缩了几条。

    ★为什么丢的是 detail 而不是整条★：摘要已经能回答「当时说了什么」，
    原文只是冗余；而整条删掉会让「那天聊过这个」彻底消失。
    ★判「重不重要」要用**写入时**的重要度，不用衰减后的★：
    不然一条「搬家」这种重要的事过了四个月也会被当成小事把原文删掉
    （衰减管的是「先想起谁」，不是「值不值得留原文」）。
    """
    touched = 0
    for ep in episodes:
        if not ep.detail:
            continue
        old = _age_days(ep.ts, now) > keep_days
        minor = ep.salience < keep_salience
        if old and minor:
            ep.detail = ""
            touched += 1
    return touched


def prune_episodes(episodes: list[Episode], now: datetime, max_count: int) -> tuple[list[Episode], int]:
    """超出上限时按「有效重要度 + 被想起次数」淘汰（自清洁第三招）。

    ★绝不因为「旧」而淘汰★：分数里已经含了时间衰减，但一个 90 天前的高重要度事件
    仍然能压过昨天的琐事。返回（保留的, 删了几条）。
    """
    if max_count <= 0 or len(episodes) <= max_count:
        return episodes, 0
    ranked = sorted(episodes, key=lambda e: (salience_eff(e, now), e.recalls, e.ts), reverse=True)
    kept_ids = {e.id for e in ranked[:max_count]}
    return [e for e in episodes if e.id in kept_ids], len(episodes) - max_count


def promote_to_facts(episodes: list[Episode], facts: list[Fact],
                     min_evidence: int = PROMOTE_MIN_EVIDENCE) -> list[Fact]:
    """反复出现的同一类事件 → 上升成一条「事实」（L2 → L3，模仿人脑）。

    ★只按每条的「主关键词」聚簇★（`keywords[0]`，它是标题优先选出来的，= 这件事的主题）。
    否则一条记录会带着 8 个关键词各自去聚簇，一次巩固能吐出五六条重复的「theme.X」事实
    （实测过），把 L3 灌成一堆噪声。

    判据保守：同一个主题出现在 ≥3 个**不同日子**里才提升 ——
    「每周三都有组会」会变成一条事实，而「昨天说了一次买牛奶」不会。
    """
    buckets: dict[str, list[Episode]] = {}
    for ep in episodes:
        if not ep.keywords:
            continue
        topic = ep.keywords[0]
        if len(topic) < 2 or topic in _GENERIC_TOPICS:
            continue
        buckets.setdefault(topic, []).append(ep)
    known = {f.key for f in facts}
    for kw, eps in buckets.items():
        days = {parse_ts(e.ts).date() for e in eps if parse_ts(e.ts)}
        if len(days) < min_evidence:
            continue
        key = f"theme.{kw}"
        if key in known:
            continue
        titles = "；".join(sorted({e.title for e in eps if e.title})[:3])
        facts.append(Fact(
            key=key, value=titles or kw, about="world",
            confidence=min(0.8, 0.35 + 0.1 * len(days)),
            evidence=len(days), sources=[e.id for e in eps[:5]],
            note=f"由 {len(days)} 次提到「{kw}」归纳（L2→L3）",
        ))
    return facts


# --------------------------------------------------------------------------- #
# L3 语义记忆
# --------------------------------------------------------------------------- #
def fact_eff(fact: Fact, now: datetime) -> float:
    """事实现在的有效置信度：pinned（身份）不衰减，其余按「多久没被确认」衰减。"""
    if fact.pinned:
        return 1.0
    base = max(0.0, min(1.0, fact.confidence))
    return base * _half_life_factor(_age_days(fact.last_seen, now), FACT_HALF_LIFE_DAYS)


def merge_facts(facts: list[Fact], min_confidence: float = FACT_MIN_CONFIDENCE) -> list[Fact]:
    """同一个 key 的重复事实合并 + 清掉弱到没意义的（自清洁第四招）。

    同 key 的规则：**保留证据多的那条**，另一条的 evidence 加到它身上
    （「被确认过两次」比「相信两次」更接近真相）；值不同就是「改主意了」，
    后来的那次 `last_seen` 赢。
    """
    by_key: dict[str, Fact] = {}
    for fact in facts:
        if not fact.key:
            continue
        old = by_key.get(fact.key)
        if old is None:
            by_key[fact.key] = fact
            continue
        winner, loser = (old, fact)
        if fact.evidence > old.evidence or (fact.evidence == old.evidence
                                            and fact.last_seen > old.last_seen):
            winner, loser = fact, old
        winner.evidence += loser.evidence
        winner.confidence = max(winner.confidence, min(1.0, winner.confidence + 0.05 * loser.evidence))
        winner.pinned = winner.pinned or loser.pinned
        winner.first_seen = min(winner.first_seen, loser.first_seen)
        winner.last_seen = max(winner.last_seen, loser.last_seen)
        winner.sources = sorted(set(winner.sources) | set(loser.sources))[:10]
        by_key[fact.key] = winner
    return [f for f in by_key.values() if f.pinned or f.confidence >= min_confidence]


def prune_facts(facts: list[Fact], now: datetime, max_count: int) -> tuple[list[Fact], int]:
    """事实也设条数上限：pinned 永远保留，其余按有效置信度淘汰。"""
    if max_count <= 0 or len(facts) <= max_count:
        return facts, 0
    pinned = [f for f in facts if f.pinned]
    others = sorted([f for f in facts if not f.pinned],
                    key=lambda f: (fact_eff(f, now), f.evidence), reverse=True)
    room = max(0, max_count - len(pinned))
    kept = pinned + others[:room]
    return kept, len(facts) - len(kept)


def touch_fact(fact: Fact, when: datetime | None = None) -> None:
    """事实被用到时「回血」：更新 last_seen（衰减就是按它算的）。"""
    stamp = (when or datetime.now()).replace(microsecond=0).isoformat(sep=" ")
    if stamp > fact.last_seen:
        fact.last_seen = stamp


def touch_episode(episode: Episode, when: datetime | None = None) -> None:
    """事件被检索命中时记一笔：`recalls` 会让它在淘汰时更靠前（越用越牢）。"""
    episode.recalls += 1
    episode.last_recall = (when or datetime.now()).replace(microsecond=0).isoformat(sep=" ")


# --------------------------------------------------------------------------- #
# L1 原始对话：滑动窗口
# --------------------------------------------------------------------------- #
def pick_old_sessions(states: dict[str, dict], now: datetime,
                      keep_days: float = 30.0, keep_max: int = 10) -> list[str]:
    """挑出「可以删掉」的原始对话文件（滑动窗口）。

    ★两条硬规则★：
      1. **没归档过的绝不删**（`extracted` 为假）——否则事件就丢了；
      2. 保底留最近的 `keep_max` 个（★这是保底，不是上限★：
         它要是设得比实际文件数还大，整条规则就形同虚设、滑动窗口永远不动）。

    `states` 形如 ``{文件名: {"extracted": True, "last_ts": "..."}}``。
    """
    rows: list[tuple[str, datetime, bool]] = []
    for name, state in (states or {}).items():
        ts = parse_ts(str((state or {}).get("last_ts") or "")) or parse_ts(str((state or {}).get("first_ts") or ""))
        rows.append((name, ts or now, bool((state or {}).get("extracted"))))
    rows.sort(key=lambda r: r[1], reverse=True)
    protected = {name for name, _, _ in rows[:max(0, keep_max)]}
    cutoff = now - timedelta(days=keep_days)
    return [name for name, ts, extracted in rows
            if extracted and name not in protected and ts < cutoff]
