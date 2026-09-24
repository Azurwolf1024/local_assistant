"""一句话 ↔ 一个事件：**文本侧唯一的家**。

为什么要有这个模块
------------------
旧代码把「闹钟」和「日程」当成两种东西，配了两个 handler，中间靠三条让位规则
互相踢球：说了重复 / 说了多个提前量 / 说了日期+钟点还带事件名词 → 从闹钟踢给日程。
后果是「每个工作日八点半叫我起床」这种再普通不过的话被踢到日程那边，而日程只认
「每周X / 每N天」，最后只能存成一条**不会重复**的闹钟。

现在没有「闹钟」和「日程」这两个类型了，只有**一张可填可不填的表**：

    ★说了就加上，没说就缺省★

| 字段 | 缺省 | 说了什么才加 |
|---|---|---|
| `title` | 空（**纯闹钟**：只负责准时响） | 「提醒我吃药」→ 吃药 |
| `remind_before` | `[0]`（准时） | 「提前半小时」→ `[30, 0]` 等 |
| `repeat` + `weekday`/`day`/… | 无（一次） | 「每周三／工作日／每天」 |
| `duration_minutes` | `0` | 「三点到四点半」→ 90 |
| `location` / `note` / `until` | 空 | 「地点…」「备注…」「到…为止」 |

所以「闹钟的关键在于准时响起」这件事不需要一个类型来保证——它就是缺省值。
``category``（course/meeting/task/activity）只是个**标签**，用来筛选和选词，不是类型。

这里的另一个职责是把**文本**搞清楚，别的模块不要再自己写正则。
分工：
    :mod:`voice_loop.nlp_time`   时间词（什么时候、多久、重复、提前量、截止）
    :mod:`voice_loop.events`     事件模型 + 发生时间引擎 + 到期引擎
    :mod:`voice_loop.event_text` ← 本模块：一句话 ←→ 事件字段/播报文案
    :mod:`voice_loop.skills`     意图路由（新增/查/改/删/完成）与播报时机
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .events import (
    leads_of,
    new_event,
    parse_hhmm,
    repeat_of,
    title_of,
    until_of,
)
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
# 标签：course / meeting / task / activity（**不是**类型，只用于筛选与选词）
# --------------------------------------------------------------------------- #
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
# ★命令残留★：清洗后还带着「定/设/提醒/闹钟」这类字样的，不是内容，是没洗干净的
# 命令（「需要定所有工作日的闹钟」）。遇到它宁可回头用原因从句/类别名。
_COMMAND_RESIDUE = re.compile(r"提醒|闹钟|闹中|定时|设置|设定|定个|定一个|订个|安排|记录")
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
# 新增事件时要把「帮我记录 / 安排 / 有」这类动词去掉，只留事情本身。
# ★「上 / 去 / 在 / 是」不在表里★：它们会把「上课」咬成「课」（1 个字就不算内容了）。
# 真正需要剥这类的场合由 :func:`clean_title_loose` 兜底。
SCHEDULE_VERBS = (
    "帮我", "麻烦你", "麻烦", "请", "到时候", "记得", "提醒我", "叫醒我", "叫我", "喊我",
    "帮我记", "记录一下", "记录",
    "记一下", "记下来", "记下", "添加", "新增", "创建", "新建", "排入", "安排", "加",
    "每天", "每", "有个", "有", "我要", "我", "你", "的",
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
# ★提前量的**列表**写法★：「提前一天和半小时提醒我」「提前30分钟、10分钟和到点提醒我」
# 单个 LEAD_WORDS 只吃得下第一段（后面那几段没有「提前」二字），剩下那段会被当成
# 标题（「提前一天和」）和时长（凭空多出 30 分钟）。所以列表形式要一次吃完。
_LEAD_UNIT = r"(?:\d+|半|[一二三四五六七八九十两]+)?\s*(?:天|日|小时|钟头|分钟|分|刻|点)"
LEAD_LIST = re.compile(
    rf"(?:提前|预先|事先)\s*{_LEAD_UNIT}"
    rf"(?:\s*(?:和|、|还有|以及|跟|,|，)\s*{_LEAD_UNIT})+"
)


def strip_leads(text: str) -> str:
    """把提前量说法整段剥掉（含列表形式、含「到点/准时」）。"""
    t = LEAD_LIST.sub("", text or "")
    t = LEAD_WORDS.sub("", t)
    return re.sub(r"(?:到点|准时|正点|提前|预先|事先)", "", t)
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
    return clean_content(strip_leads(text))


def clean_title_loose(text: str) -> str:
    """更狠一点的标题清洗：去时间、去提前量、去口头语，**再去开头动词**。

    用在「分句标题不可用」的兜底上。★顺序很重要★：先清时间再剥动词——
    反过来的话「每周三上午九点提醒我上课」剥完「每」就停在「周三」上，
    时间还在、动词也剥不掉，最后得到「提醒我上课」（看着像内容，其实是命令残留）。
    """
    return strip_leading(clean_content(strip_leads(text)))


def is_topic_like(text: str) -> bool:
    """这句话到底有没有说「要干什么」——只有动作词就不算。"""
    t = (text or "").strip()
    if len(t) < 2 or t in _NOT_A_TOPIC:
        return False
    if _ACTION_ONLY.match(t):
        return False
    # ★只剩周期 / 提前量 / 钟点的说法不是内容★：
    #   「每个工作日」「提前一天和」「到点」以前会被当成标题存进去。
    rest = re.sub(r"[\s，。、,.：:]+", "", strip_leads(REPEAT_WORDS.sub("", t)))
    # 只削**尾巴**上的连接词（列表写法会留下一个「和」）；
    # ★不能削开头★——「和面」这种词的开头也是「和」。
    rest = re.sub(r"(?:和|跟|与|及|、|,|，)+$", "", rest)
    return len(rest) >= 2


def is_command_residue(text: str) -> bool:
    """清洗后还残留命令词（「需要定所有工作日的闹钟」）→ 它说的不是内容。"""
    return bool(_COMMAND_RESIDUE.search(text or ""))


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
    """按事件名词判断**标签**（course / meeting / task / activity），认不出来给空串。

    ★这只影响筛选和选词，不影响事件怎么发生★——没有「这是日程还是闹钟」的判断了。
    认不出来就留空，**不要瞎猜成 task**（猜错会让「有哪些课」查不到）。
    """
    t = text or ""
    best = ""
    best_len = 0
    for cat, nouns in CATEGORY_NOUNS.items():
        for noun in nouns:
            if noun in t and len(noun) > best_len:
                best, best_len = cat, len(noun)
    return best


def classify(text: str) -> str:
    """一句话 → **标签**（course/meeting/task/activity，或空串）。

    以前这里要回答「是闹钟还是日程」，而且答错了就要靠让位规则补救；
    现在没有那个问题了——标签只用于筛选与选词。
    """
    return category_of(text)


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
def extract(text: str, now: datetime) -> dict:
    """把一句话变成 ``to_item()`` 能吃的字段——**说了就填上，没说就缺省**。

    返回：title / start(datetime) / repeat / weekday / time / duration_minutes /
    location / note / remind_before / until(date) / category / payload / cause。

    ★缺省提前量永远是 ``[0]``（准时）★——不提供「默认提前几分钟」的旋钮，
    因为那是“猜”出来的行为；要提前就得说「提前…」。
    """
    raw = (text or "").strip()
    cause, payload = split_reason(raw)
    category = category_of(payload)

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
    body = strip_leads(payload)
    rng = parse_clock_range(body, now)
    if rng is not None:
        (h1, m1), (h2, m2) = rng
        minutes = (h2 * 60 + m2) - (h1 * 60 + m1)
        duration = minutes + 24 * 60 if minutes <= 0 else minutes
    elif (delta := parse_duration(body)) is not None and not _DELAY_SUFFIX.search(body):
        duration = int(delta.total_seconds() // 60)

    # ---- 标题 / 地点 / 备注 -------------------------------------------
    head, location = split_title_location(payload)
    # ★三条路依次试，因为「剥动词」和「剥口头语」的副作用不同★：
    #   ① 分句（strip_leading）：会剥「每/上…」这类开头动词，但不碰 _FILLER 里的「跟/和」
    #      ——「跟导师见面」要保持完整；
    #   ② clean_title（_FILLER）：会剥「跟/和」，但不碰「上」——「上课」要保持完整；
    #   ③ 两个都剥：只剩下「定一个的闹钟」这种命令残留时才走到这里。
    title = re.sub(r"[，。、,.\s:：]+", "", head)     # 内部空格/标点也去掉（「AIAA3102 机器学习」）
    if not is_topic_like(title) or is_command_residue(title):
        title = clean_title(payload)
    if not is_topic_like(title) or is_command_residue(title):
        title = clean_title_loose(payload)
    if not is_topic_like(title) or is_command_residue(title):
        title = ""                          # 只剩命令词 → 当作没说内容
    # 原因从句里往往才是「为什么」，而且比命令残留更像内容：
    # 「我工作日九点有课，那就需要定工作日八点半的闹钟」→ 标题取「上课」而不是
    #「需要定所有工作日的闹钟」。
    if not title and cause and cause != payload:
        cat_cause = category_of(cause)
        title = ACTION_BY_CATEGORY.get(cat_cause) or ""
        if not title:
            loose = clean_title_loose(cause)
            title = loose if (is_topic_like(loose) and not is_command_residue(loose)) else ""
        if cat_cause and not category:
            category = cat_cause          # 标题取自原因从句，标签也跟着它
    note_m = NOTE_RE.search(raw)
    note = note_m.group(1).strip() if note_m else ""

    # ---- 提前量 / 截止 -------------------------------------------------
    # ★缺省是 [0]（准时）★：说了「提前…」才加，没说什么都不加。
    leads = parse_reminds(raw) or [0]
    until = parse_until(raw, now)

    return {
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
        "category": category,
        "payload": payload,
        "cause": cause,
    }


def to_item(fields: dict) -> dict:
    """``extract()`` 的字段 → :func:`events.new_event` 的条目（可直接 append）。"""
    start = fields.get("start")
    until = fields.get("until")
    return new_event(
        str(fields.get("title") or ""),
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
def note_of(text: str) -> str:
    """「备注带实验报告」→ 带实验报告（跟着提醒一起念/显示）。"""
    m = NOTE_RE.search(text or "")
    return m.group(1).strip() if m else ""


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
    """播报文案。**看提前量，不看类型**（没有类型了）：

    * 准时（``lead <= 0``）→ 闹钟口径「时间到了，吃药。」——这是缺省的那次；
    * 提前 → 「提醒你：十分钟后，也就是10:00，开组会，地点…。」

    标题可以是空的（纯闹钟）：「时间到了。」
    """
    title = title_of(item)
    where = where_text(str(item.get("location") or ""))
    note = note_text(item)
    tail = f"，{title}{note}{where}" if title or note or where else ""
    if lead <= 0:
        return f"时间到了{tail}。"
    head = lead_head(start, now, lead)
    if item.get("remind_text"):
        head = f"{item['remind_text']}，{head}"
    return f"提醒你：{head}{tail}。"


def render_added(item: dict, start: datetime, now: datetime, *, changed: bool = False) -> str:
    """新增/修改后的确认话术。

    ``leads == [0]`` 时不说「我会准时提醒你」——那是缺省值，说了反而啰嗦；
    只有用户特意要了提前量才回报一遍。
    """
    title = title_of(item) or "闹钟"
    where = where_text(str(item.get("location") or ""))
    until = until_text(until_of(item))
    leads = leads_of(item)
    when = when_text(item)
    head = f"{when}，{title}{where}" if when else f"{humanize(start, now)}，{title}{where}"
    verb = "已改到" if changed else "已记下"
    tail = "" if leads in ([0], []) else " " + leads_text(leads)
    return f"好，{verb}：{head}{until}。{tail}".strip()
