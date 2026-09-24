"""一句话 ↔ 一个事件：**文本侧唯一的家**。

为什么要有这个模块
------------------
旧代码把「闹钟」和「日程」当成两种东西，配了两个 handler，中间靠三条让位规则
互相踢球：说了重复 / 说了多个提前量 / 说了日期+钟点还带事件名词 → 从闹钟踢给日程。
后果是「每个工作日八点半叫我起床」这种再普通不过的话被踢到日程那边，而日程只认
「每周X / 每N天」，最后只能存成一条**不会重复**的闹钟。

现在只有一条规则：

    ★kind 不是「有没有重复」决定的★
    闹钟和日程都能重复、都能有提前量、都能带地点/时长/截止日期；
    kind 只决定**播报口径**和**默认提前量**：
        reminder（闹钟）→ 「时间到了，起床。」        默认到点提醒（[0]）
        event（日程）  → 「提醒你：10 分钟后…有课」  默认提前 10 分钟

    ★分界看「这是不是一件占时间的事」★
    有事件名词（课/会议/约/活动/考试…）、有地点、有时长、有「A 点到 B 点」
    → event；只说「提醒我 / 叫我 / 喊我」→ reminder。

这里同时收拢了全部**文本清洗**（时间词、提前量说法、口头填充词、地点、标题归一化、
「找你说的是哪一条」）。别的模块不要再自己写正则。

分工：
    :mod:`voice_loop.nlp_time`  时间词解析（什么时候、多久、重复、提前量、截止）
    :mod:`voice_loop.events`    事件模型 + 发生时间引擎 + 到期引擎
    :mod:`voice_loop.event_text`  ← 本模块：一句话 ←→ 事件字段/播报文案
    :mod:`voice_loop.skills`    意图路由（新增/查/改/删/完成）与播报时机
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .events import kind_of, leads_of, new_event, parse_hhmm, repeat_of, until_of
from .nlp_time import (
    _apply_period,
    cn_number,
    humanize,
    humanize_delta,
    parse_clock,
    parse_date_hint,
    parse_datetime,
    parse_duration,
    parse_reminds,
    parse_repeat,
    parse_until,
    parse_weekday,
)

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# --------------------------------------------------------------------------- #
# 事件名词 / 类别
# --------------------------------------------------------------------------- #
# category 用来筛选（「有哪些课」「所有会议」）和决定播报用词，不影响它怎么发生。
CATEGORY_NOUNS: dict[str, tuple[str, ...]] = {
    "course": ("课", "课程", "上课", "讲座", "考试", "测验", "实验", "辅导", "seminar", "lecture"),
    "meeting": ("会议", "开会", "组会", "例会", "见面", "面谈", "面试", "讨论", "汇报", "答辩", "洽谈", "约"),
    "task": ("作业", "任务", "报告", "论文", "ddl", "deadline", "截止", "提交", "复习", "准备", "写"),
    "activity": ("活动", "比赛", "演出", "排练", "社团", "志愿", "体检", "看病", "健身", "锻炼", "跑步",
                 "航班", "火车", "高铁", "机票", "取件", "快递", "聚会", "生日"),
}
# 从句里只剩「有课」这种没动作的说法时，给个像样的提醒名（「时间到了，上课。」）
ACTION_BY_CATEGORY = {"course": "上课", "meeting": "开会", "activity": "去活动"}
# 「原因从句」标记：用户经常说「我工作日九点有课，那就需要定工作日八点半的闹钟」
_REASON_SPLIT = re.compile(r"那就|所以我|因此|于是|所以|这样的话|那(?:我)?就|不如|不如就")
# 只是「下个命令」而不是「说内容」的词：清洗后如果只剩这些，就不算说了主题
_NOT_A_TOPIC = {
    "", "我", "你", "他", "她", "它", "我们", "你们", "这个", "那个",
    "一下", "一个", "事情", "东西", "时候", "时间", "的", "了",
    "闹钟", "提醒", "日程", "安排", "备忘", "事项", "活动", "任务",
}
_ACTION_ONLY = re.compile(
    r"^(?:帮我|给我|替我|麻烦你?|请|要|再|又|去|把|将|和|跟|记得|到时候|提醒我|叫我|喊我)+$"
)


# --------------------------------------------------------------------------- #
# 文本清洗（时间词、提前量、口头填充词）
# --------------------------------------------------------------------------- #
_NUM = r"(?:\d{1,2}|[一二三四五六七八九十两]+)"
# 一个钟点（带「到/至/~」的成对写法算一个整体：「下午3点到4点半」整段剥掉，
# 否则标题里会残留一个「到有一场造物社的活动」）
_CLOCK_TOKEN = rf"(?:{_NUM}\s*[点時时](?:半|\s*{_NUM}\s*分)?|\d{{1,2}}\s*[:：]\s*\d{{1,2}})"
TIME_RANGE = re.compile(rf"({_CLOCK_TOKEN})\s*(?:到|至|~|—|-)\s*({_CLOCK_TOKEN})")
# 「从现在起持续多久」：2小时15分钟 / 一个半小时 / 45分钟
DURATION_WORDS = re.compile(
    rf"{_NUM}?\s*个?\s*半?\s*(?:小时|钟头|分钟|分)(?!钟)|{_NUM}\s*(?:天|周|个月)"
)
TIME_WORDS = re.compile(
    r"(?:大后天|后天|明天|明日|今天|今日|今早|明早|今晚|明晚|"
    r"凌晨|早上|早晨|清晨|上午|中午|正午|下午|傍晚|晚上|夜里|夜晚|半夜|"
    r"(?:下{1,2}个?|上个?|这|本)?(?:周|星期|礼拜)[一二三四五六日天末]|"
    rf"{_CLOCK_TOKEN}\s*(?:到|至|~|—|-)\s*{_CLOCK_TOKEN}|"    # 3点到4点半：整体
    rf"{_NUM}\s*月\s*{_NUM}\s*[号日]?|"                       # 9月23号
    rf"{_NUM}\s*[号日](?![一二三四五六七八九十])|"              # 23号
    rf"{_NUM}\s*[点時时](?:半|{_NUM}\s*分)?|"
    r"\d{1,2}\s*[:：]\s*\d{1,2}|"
    rf"{_NUM}?个?半?(?:小时|钟头|分钟|分|秒)|"
    r"(?:以后|之后|过后|后|过)"
    r")"
)
_FILLER = re.compile(
    # 长词必须放在前面：正则的 | 是「先匹配到就用」，
    # 否则「我说我…」会被先匹配掉开头的「我」，剩下「说我有跆拳道课」这种怪句子
    r"^(?:麻烦你|帮我|我说我|我说|到时候|别忘了|别忘|一定|记得|记着|给我|"
    r"请|你|我|要|去|来|有|一下|一个|一场|一次|一门|一节|一项|个|的|把|给|向|从|和|跟)+"
)
_TAIL_FILLER = re.compile(
    r"(?:这件事|这个事情|记录一下|记下来|记一下|记上|记下|一下|到时候|记得|提醒我|叫我"
    r"|这个|那个|吧|哦|啊|呀|呢|了|的|吗)+$"
)
# 新增事件时要把「帮我记录 / 安排 / 有」这类动词去掉，只留事情本身
SCHEDULE_VERBS = (
    "帮我", "麻烦你", "麻烦", "请", "到时候", "记得", "提醒我", "叫醒我", "叫我", "喊我",
    "帮我记", "记录一下", "记录",
    "记一下", "记下来", "记下", "添加", "新增", "创建", "新建", "排入", "安排", "加",
    "每天", "每", "有个", "有", "我要", "我", "你", "的", "去", "上", "在", "是",
    # 量词与「把字句」：「有一场造物社的活动」→ 标题不该留「一场」；
    # 「把后天下午两点的体检记上」→ 标题不该留「把…记上」
    "一场", "一次", "一门", "一节", "一项", "一个", "把", "将",
    # 「那就需要定工作日八点半的闹钟」→ 标题不该留「需要定…所有的」
    "需要", "要不要", "就得", "得", "定个", "定一个", "订个",
    "设个", "设置", "设", "定", "订",
    "所有的", "所有", "全部的", "全部",
)
# 标题里不该残留周期说法：「每月5号交房租」的标题应该是「交房租」
REPEAT_WORDS = re.compile(
    r"(?:每(?:个)?年\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*月\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*[号日]?"
    r"|每(?:个)?月\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*[号日]?"
    r"|每\s*(?:\d{1,3}|[一二三四五六七八九十两]+|半)\s*(?:个)?\s*(?:天|日|小时|钟头|分钟|分)"
    r"|每\s*(?:\d{1,2}|[一二三四五六七八九十两]+)\s*个?\s*(?:周|星期|礼拜)"   # 每两周
    r"|(?:每个?)?工作日"                                              # ★工作日★
    r"|每(?:个)?(?:周|星期|礼拜)"                                     # 光说「每周」
    r"|每(?:个)?(?:月|年|天))"
)
# 「提前半小时」这类提前量说法：标题里不该留（它属于 remind_before）
LEAD_WORDS = re.compile(
    r"(?:提前|预先|事先)\s*(?:\d+|半|[一二三四五六七八九十两]+)?\s*(?:天|日|小时|钟头|分钟|分|刻)"
    r"\s*(?:和|、|还有|以及)?"
)
LOCATION_RE = re.compile(r"(?:地点|教室)\s*[:：]?\s*([^，,。;；]+)")
# 「在新体育馆」「在腾讯会议」这种没写「地点」但明显是地点的说法。
# 前缀可以是 0 字符（「在腾讯会议」本身就是完整地点），否则会漏掉这类说法。
_IN_PLACE = re.compile(
    r"在\s*([\u4e00-\u9fffA-Za-z0-9]{0,12}?"
    r"(?:教学楼|教室|实验室|机房|会议室|礼堂|报告厅|图书馆|宿舍|食堂|体育馆|操场|医院|"
    r"校区|中心|大楼|楼|馆|厅|场|室|站|机场|线上|腾讯会议|Zoom|zoom))"
)
# 「备注带实验报告」→ 带实验报告（跟着提醒一起念/显示）
NOTE_RE = re.compile(r"备注\s*[:：]?\s*([^，,。;；]+)")


def strip_leading(text: str, words: tuple[str, ...] = SCHEDULE_VERBS) -> str:
    """反复剥掉开头的口头语，直到没有可剥的为止。"""
    t = (text or "").strip(" \u3000，,。:：-—")
    # 「开组会 / 开例会」的「开」不是名字的一部分（但「开学典礼」不能碰）
    t = re.sub(r"^开(?=[会组例])", "", t)
    changed = True
    while changed and t:
        changed = False
        for w in words:
            if t.startswith(w):
                t = t[len(w) :].strip(" \u3000，,。:：-—")
                changed = True
                break
    return t


def clean_memo_content(text: str) -> str:
    """备忘是**纯文本**，时间词要留着：

    「记一下明天带伞」存成「带伞」就把最关键的信息丢了（备忘没有时间字段）。
    闹钟/日程那边有时间字段，才需要把时间词从内容里剥掉。
    """
    t = re.sub(r"^[，。、,.\s:：]+", "", text or "")
    t = _FILLER.sub("", t)
    t = _TAIL_FILLER.sub("", t)
    t = re.sub(r"[，。、,.\s:：]+", "", t)
    return t.strip()


def clean_content(text: str) -> str:
    """去掉时间词与口头语，只留事情本身。"""
    t = TIME_WORDS.sub("", text or "")
    t = re.sub(r"^[，。、,.\s:：]+", "", t)
    t = _FILLER.sub("", t)
    # ★先去掉标点、再削尾巴★：「有社团活动，到时候记得提醒我。」句末那个句号
    # 会让锚在 $ 的 _TAIL_FILLER 对不上，标题里就留下「到时候记得提醒我」。
    t = re.sub(r"[，。、,.\s:：]+", "", t)
    t = _TAIL_FILLER.sub("", t)
    return t.strip()


def clean_title(text: str) -> str:
    """标题清洗 = 去时间词 + 去提前量说法 + 去口头语。"""
    return clean_content(LEAD_WORDS.sub("", text or ""))


def is_topic_like(text: str) -> bool:
    """这句话到底有没有说「要干什么」——只有动作词就不算。"""
    t = (text or "").strip()
    if len(t) < 2 or t in _NOT_A_TOPIC:
        return False
    return not _ACTION_ONLY.match(t)


# --------------------------------------------------------------------------- #
# 标题归一化 / 「你说的是哪一条」
# --------------------------------------------------------------------------- #
_NORM_STRIP = re.compile(r"[\s_\-—·．.，,。:：、'\"“”‘’()（）\[\]【】]+")
# 在列表里找你指的那一条。只把这些真正泛的工具词当停用词，
# 「组会 / 例会」不算——它们通常就是条目的名字本身。
TITLE_STOPWORDS = {
    "课", "课程", "上课", "会议", "开会", "日程", "安排", "行程",
    "事情", "提醒", "活动", "一个", "一下",
}
# 双字匹配时容易撞车的泛词：光靠这些字对上不算「认出来了」
GENERIC_BIGRAMS = {
    "今天", "明天", "后天", "早上", "上午", "中午", "下午", "晚上", "时候", "时间",
    "什么", "怎么", "这个", "那个", "一下", "不是", "以后", "我要", "我们", "已经",
    "取消", "删除", "删掉", "去掉", "改到", "改成", "换个", "挪到", "提前", "推迟",
    "上课", "课程", "会议", "开会", "日程", "安排", "行程", "提醒", "取消", "的课",
}


def norm_title(text: str) -> str:
    """标题归一化：去掉空格标点、统一小写，用来判断「是不是同一条」。"""
    return _NORM_STRIP.sub("", (text or "").strip().lower())


def title_tokens(title: str) -> list[str]:
    if not title:
        return []
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}|[\u4e00-\u9fff]{2,}", title)
    return [t for t in toks if t not in TITLE_STOPWORDS]


def title_hit(title: str, text: str) -> int:
    """这句话说的是不是这条：2 = 名字里的词直接出现，1 = 只沾到两个字，0 = 不像。

    「开组会」对上「删掉组会」这种就得靠双字：名字本身很少被完整念一遍。
    """
    if not title:
        return 0
    if any(tok in text for tok in title_tokens(title)):
        return 2
    for i in range(len(title) - 1):
        bg = title[i : i + 2]
        if bg in GENERIC_BIGRAMS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", bg) and bg in text:
            return 1
    return 0


# --------------------------------------------------------------------------- #
# 拆地点 / 拆时长
# --------------------------------------------------------------------------- #
def split_title_location(text: str) -> tuple[str, str]:
    """从「每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302」里拆出事项和地点。"""
    loc = ""
    m = LOCATION_RE.search(text or "")
    if m:
        loc = m.group(1).strip()
        if loc in ("有", "是", "的"):
            loc = ""
    if not loc:                       # 没写「地点」就看「在…」
        m = _IN_PLACE.search(text or "")
        if m:
            loc = m.group(1).strip()
    head = re.split(r"[，,。;；]", text or "")[0]
    head = TIME_WORDS.sub("", head)
    head = REPEAT_WORDS.sub("", head)
    head = re.sub(r"\s+", " ", head)
    return _TAIL_FILLER.sub("", strip_leading(head)), loc


def parse_clock_range(text: str, now: datetime | None = None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """「下午3点到4点半」→ ((15, 0), (16, 30))；没有区间就返回 None。

    为什么要它：① 标题里不该残留「到4点半」；② 记下时长，下一项/提醒才不会把
    一个 90 分钟的活动当成一小时。时段词（下午）对**两端都生效**。
    """
    m = TIME_RANGE.search(text or "")
    if m is None:
        return None
    c1 = parse_clock(m.group(1))
    c2 = parse_clock(m.group(2))
    if c1 is None or c2 is None:
        return None
    h1 = _apply_period(c1[0], text, c1[1], now)
    h2 = _apply_period(c2[0], text, c2[1], now)
    return (h1, c1[1]), (h2, c2[1])


# 「三小时后提醒我」里的三小时是**延迟**，不是这件事的时长
_DELAY_SUFFIX = re.compile(r"(?:小时|钟头|分钟|分|天|周|个月)\s*(?:后|以后|之后|过后)")


# --------------------------------------------------------------------------- #
# 分类：闹钟还是日程（唯一的分界规则）
# --------------------------------------------------------------------------- #
def category_of(text: str) -> str:
    """按事件名词判断类别（course / meeting / task / activity），认不出来给空串。"""
    t = text or ""
    best = ""
    best_len = 0
    for cat, nouns in CATEGORY_NOUNS.items():
        for noun in nouns:
            if noun in t and len(noun) > best_len:
                best, best_len = cat, len(noun)
    return best


def looks_like_block(text: str) -> bool:
    """是不是「占一段时间的事」：有地点、有时长、有 A 点到 B 点。"""
    t = text or ""
    if LOCATION_RE.search(t) or _IN_PLACE.search(t):
        return True
    if TIME_RANGE.search(t):
        return True
    return bool(DURATION_WORDS.search(t) and re.search(r"持续|用|开|办|进行|一共|总共", t or ""))


def classify(text: str) -> tuple[str, str]:
    """→ (kind, category)。**这是全文唯一的分界规则。**

    旧代码这里是一条让位链（闹钟看不清就踢给日程），现在是「看这件事本身的性质」。
    category 认不出来就留空——**不要瞎猜成 task**（猜错会让「有哪些课」查不到）。
    """
    t = text or ""
    cat = category_of(t)
    if cat or looks_like_block(t):
        return "event", cat
    return "reminder", ""


def payload_clause(text: str) -> str:
    """剥掉「原因从句」：「我工作日九点有课，那就需要定工作日八点半的闹钟」→
    「需要定工作日八点半的闹钟」。

    为什么必须剥：不剥的话两个钟点都在句子里，「九点」很可能被当成闹钟时间。
    """
    return split_reason(text)[1]


def split_reason(text: str) -> tuple[str, str]:
    """按最后一个「那就/所以/因此…」切成 (原因从句, 要求从句)。没有标记就两段相同。"""
    t = text or ""
    last = None
    for m in _REASON_SPLIT.finditer(t):
        last = m
    if last is None:
        return t, t.strip()
    cause = t[: last.start()].strip(" \u3000，,。;；")
    return cause, t[last.end() :].strip()


# --------------------------------------------------------------------------- #
# 抽取：一句话 → 事件字段
# --------------------------------------------------------------------------- #
def extract(text: str, now: datetime, *, default_lead: int | None = None) -> dict:
    """把一句话变成 ``to_item()`` 能吃的字段。

    返回：kind / title / start(datetime) / repeat / weekday / time / duration_minutes /
    location / note / remind_before / until(date) / category / payload / cause / warned。

    ``title`` 可能是空串（用户没说清干什么），由调用方决定怎么追问；
    ``warned`` 是需要用户确认的疑点。
    """
    raw = (text or "").strip()
    cause, payload = split_reason(raw)
    kind, category = classify(payload)

    # ---- 时间 ---------------------------------------------------------
    clock = parse_clock(payload)
    day_hint = parse_date_hint(payload, now)
    start: datetime | None = None
    if clock is not None or day_hint is not None or _DELAY_SUFFIX.search(payload):
        start = parse_datetime(payload, now)

    # ---- 重复 ---------------------------------------------------------
    rep = parse_repeat(payload) or {}
    repeat = rep.get("repeat")
    weekday = rep.get("weekday")
    if weekday is None and repeat in ("weekly", "biweekly"):
        # 「每周交周报」没写星期几 → 就按说话这天算（不能默默变成一次性）
        weekday = (start or now).weekday()
    if repeat in ("weekly", "biweekly") and weekday is not None and start is None:
        # 只说了「每周三」没说日期 → 锚点放到下一个那个星期几
        hh, mm = parse_clock(payload) or (9, 0)
        anchor = datetime.combine(
            now.date() + timedelta(days=(weekday - now.weekday()) % 7), time(hh, mm)
        )
        start = anchor if anchor > now else anchor + timedelta(days=7)
    if start is None and repeat:
        # 循环事件没给时间 → 默认 09:00（★不能用「现在★」，否则每天的时刻会跟着说话时刻漂）
        day = now.date()
        if repeat == "weekdays":
            day += timedelta(days=max(0, (7 - day.weekday()) if day.weekday() >= 5 else 0))
        start = datetime.combine(day, time(9, 0))
        if start <= now and repeat in ("daily", "weekdays"):
            start += timedelta(days=1)
    if start is None:
        start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # ---- 时长 ---------------------------------------------------------
    # ★提前量不是时长★：「每周三九点上课，提前半小时」的半小时属于 remind_before，
    # 被当成 duration 会让条目凭空长出一个 30 分钟的尾巴。
    duration = 0
    body = LEAD_WORDS.sub("", payload)
    rng = parse_clock_range(body, now)
    if rng is not None:
        (h1, m1), (h2, m2) = rng
        minutes = (h2 * 60 + m2) - (h1 * 60 + m1)
        duration = minutes + 24 * 60 if minutes <= 0 else minutes
    elif (delta := parse_duration(body)) is not None and not _DELAY_SUFFIX.search(body):
        duration = int(delta.total_seconds() // 60)

    # ---- 标题 / 地点 / 备注 -------------------------------------------
    head, location = split_title_location(payload)
    title = clean_title(head)
    # 原因从句里往往才是「为什么」——**只有真的剥过原因从句时才拿它兜底**，
    # 否则「工作日八点半的闹钟」这种没主题的话会被兜成「工作日的闹钟」。
    hint = cause if cause and cause != payload else ""
    if not is_topic_like(title):
        cat_cause = category_of(hint)
        title = ACTION_BY_CATEGORY.get(cat_cause) or (clean_title(hint) if is_topic_like(hint) else "")
    note_m = NOTE_RE.search(raw)
    note = note_m.group(1).strip() if note_m else ""

    # ---- 提前量 / 截止 -------------------------------------------------
    leads = parse_reminds(raw)
    if leads is None:
        leads = [0] if kind == "reminder" else [default_lead if default_lead is not None else 10]
    until = parse_until(raw, now)

    return {
        "kind": kind,
        "title": title,
        "start": start,
        "repeat": repeat or "once",
        "weekday": weekday if repeat in ("weekly", "biweekly") else None,
        "month": rep.get("month"),
        "day": rep.get("day"),
        "every_days": rep.get("every_days"),
        "every_minutes": rep.get("every_minutes"),
        "time": "" if not repeat else f"{start.hour:02d}:{start.minute:02d}",
        "duration_minutes": duration,
        "location": location,
        "note": note,
        "remind_before": leads,
        "until": until,
        "category": category if kind == "event" else "",
        "payload": payload,
        "cause": cause,
        "warned": [],
    }


def to_item(fields: dict) -> dict:
    """``extract()`` 的字段 → :func:`events.new_event` 的条目（可直接 append）。"""
    start = fields.get("start")
    until = fields.get("until")
    return new_event(
        str(fields.get("title") or ""),
        kind=str(fields.get("kind") or "event"),
        start=start.strftime("%Y-%m-%d %H:%M:%S") if isinstance(start, datetime) else str(start or ""),
        repeat=str(fields.get("repeat") or ""),
        remind_before=list(fields.get("remind_before") or []),
        duration_minutes=int(fields.get("duration_minutes") or 0),
        location=str(fields.get("location") or ""),
        note=str(fields.get("note") or ""),
        category=str(fields.get("category") or ""),
        until=until.isoformat() if isinstance(until, date) else str(until or ""),
        weekday=fields.get("weekday"),
        month=fields.get("month"),
        day=fields.get("day"),
        every_days=fields.get("every_days"),
        every_minutes=fields.get("every_minutes"),
        time=str(fields.get("time") or ""),
    )


# --------------------------------------------------------------------------- #
# 说回人话
# --------------------------------------------------------------------------- #
def where_text(location: str) -> str:
    return f"，地点{location}" if location else ""


def until_text(until: date | None) -> str:
    return f"（到{until.month}月{until.day}日为止）" if until is not None else ""


def note_text(item: dict) -> str:
    return f"（{item['note']}）" if item.get("note") else ""


def span_text(item: dict) -> str:
    """间隔循环的「每 X 一次」那个 X。"""
    if item.get("every_days"):
        days = int(item["every_days"])
        return "天" if days == 1 else f"{cn_number(days)}天"
    if item.get("every_minutes"):
        mins = int(item["every_minutes"])
        if mins == 60:
            return "小时"
        if mins % 1440 == 0:
            return f"{cn_number(mins // 1440)}天"
        if mins % 60 == 0:
            return f"{cn_number(mins // 60)}小时"
        return f"{cn_number(mins)}分钟"
    return "天"


def leads_text(leads: list[int]) -> str:
    """「我会提前一天和半小时提醒你。」"""
    parts = []
    for lead in leads:
        if lead <= 0:
            parts.append("到点")
        elif lead % 1440 == 0:
            parts.append(f"提前{cn_number(lead // 1440)}天")
        elif lead % 60 == 0:
            parts.append(f"提前{cn_number(lead // 60)}小时")
        else:
            parts.append(f"提前{cn_number(lead)}分钟")
    if not parts:
        return "我会到点提醒你。"
    if len(parts) == 1:
        return f"我会{parts[0]}提醒你。"
    return "我会" + "、".join(parts[:-1]) + "和" + parts[-1] + "提醒你。"


def when_text(item: dict) -> str:
    """说清楚「这条什么时候发生」：重复的说周期+钟点（一次性返回空串，由调用方说日期）。"""
    from .events import repeat_text

    rep = repeat_of(item)
    if rep == "once":
        return ""
    if rep == "interval":
        tail = "（结束后再排下一次）" if int(item.get("duration_minutes", 0) or 0) else ""
        return f"每{span_text(item)}一次{tail}"
    hh, mm = parse_hhmm(item.get("time", "09:00"))
    return f"{repeat_text(item)} {hh:02d}:{mm:02d}"


def lead_head(start: datetime, now: datetime, lead: int) -> str:
    """提前提醒时那句「还有多久、也就是几点」。"""
    clock = start.strftime("%H:%M")
    if lead <= 0:
        return f"现在就是{clock}"
    minutes = max(0, int((start - now).total_seconds() // 60))
    if minutes <= 1:
        return f"马上就到{clock}了"
    if minutes <= 60:
        return f"{cn_number(minutes)}分钟后，也就是{clock}"
    if lead >= 1440:
        return humanize(start, now)
    return f"{humanize_delta((start - now).total_seconds())}后，也就是{clock}"


def render_fire(item: dict, start: datetime, now: datetime, lead: int = 0) -> str:
    """播报文案。闹钟和日程用**同一个函数**，差别只在口径：

    闹钟是「去做某件事」（吃药、起床），所以说「时间到了，吃药。」；
    日程是「有一件事」，所以说「提醒你：……，有跟导师见面，地点……。」
    """
    title = item.get("title") or "这件事"
    where = where_text(str(item.get("location") or ""))
    note = note_text(item)
    if kind_of(item) == "reminder":
        if lead <= 0:
            return f"时间到了，{title}。"
        head = lead_head(start, now, lead)
        head = f"{item['remind_text']}，{head}" if item.get("remind_text") else head
        return f"提醒你：{head}{note}，{title}{where}。"
    if lead <= 0:
        return f"提醒你：现在就是{start.strftime('%H:%M')}，有{title}{note}{where}。"
    head = lead_head(start, now, lead)
    head = f"{item['remind_text']}，{head}" if item.get("remind_text") else head
    return f"提醒你：{head}，有{title}{note}{where}。"


def render_added(item: dict, start: datetime, now: datetime, *, changed: bool = False) -> str:
    """新增/修改后的确认话术。"""
    title = item.get("title") or "这件事"
    where = where_text(str(item.get("location") or ""))
    until = until_text(until_of(item))
    leads = leads_of(item)
    when = when_text(item)
    head = f"{when}，{title}{where}" if when else f"{humanize(start, now)}，{title}{where}"
    if kind_of(item) == "reminder":
        verb = "已改到" if changed else "已记下"
    else:
        verb = "已改到" if changed else "已排入日程"
    tail = "" if kind_of(item) == "reminder" and leads == [0] else " " + leads_text(leads)
    return f"好，{verb}：{head}{until}。{tail}".strip()
