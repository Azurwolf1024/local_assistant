"""生活实用技能：时间问答、闹钟提醒、备忘、课程/会议日程。

设计原则
    1. **本地规则优先**：这类指令（几点了 / 十分钟后提醒我 / 记一下…）用正则识别，
       零延迟、零幻觉，比丢给大模型可靠得多。
    2. **数据放在 JSON 里**：`data/*.json` 可以直接用编辑器改，
       程序只在必要时写回，外部修改会自动重新加载。
    3. **没命中就返回 None**，交给 LLM 回答，技能不会抢话。

数据结构见 `data/` 目录下的三个 json 文件（首次运行自动生成）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .nlp_time import (
    clock_text,
    cn2num,
    cn_number,
    cn_quantity,
    humanize,
    humanize_delta,
    parse_clock,
    parse_date_hint,
    parse_datetime,
    parse_duration,
    parse_reminds,
    parse_repeat,
    parse_until,
)
from .settings import Settings
from .store import JsonStore
from .vision import Vision, VisionError, norm_name

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_FULL = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}
# 「每周三」「每个星期三」→ 固定课表，而不是一次性安排
_WEEKLY = re.compile(r"每(?:个)?(?:周|星期|礼拜)\s*([一二三四五六日天])")


def _weekly_weekday(text: str) -> int | None:
    """「每周三」返回 2（0=周一）；不是每周重复则返回 None。"""
    m = _WEEKLY.search(text or "")
    return _WEEKDAY_CN.get(m.group(1)) if m else None


# 在日程列表里找你指的那一条。只把这些真正泛的工具词当停用词，
# 「组会 / 例会」不算——它们通常就是条目的名字本身。
_TITLE_STOPWORDS = {
    "课", "课程", "上课", "会议", "开会", "日程", "安排", "行程",
    "事情", "提醒", "活动", "一个", "一下",
}

# 双字匹配时容易撞车的泛词：光靠这些字对上不算「认出来了」
_GENERIC_BIGRAMS = {
    "今天", "明天", "后天", "早上", "上午", "中午", "下午", "晚上", "时候", "时间",
    "什么", "怎么", "这个", "那个", "一下", "不是", "以后", "我要", "我们", "已经",
    "取消", "删除", "删掉", "去掉", "改到", "改成", "换个", "挪到", "提前", "推迟",
    "上课", "课程", "会议", "开会", "日程", "安排", "行程", "提醒", "取消", "的课",
}


def _title_tokens(title: str) -> list[str]:
    if not title:
        return []
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}|[\u4e00-\u9fff]{2,}", title)
    return [t for t in toks if t not in _TITLE_STOPWORDS]


def _title_hit(title: str, text: str) -> int:
    """这句说的是不是这条日程：2 = 名字里的词直接出现，1 = 只沾到两个字，0 = 不像。

    「开组会」对上「删掉组会」这种就得靠双字：名字本身很少被完整念一遍。
    """
    if not title:
        return 0
    if any(tok in text for tok in _title_tokens(title)):
        return 2
    for i in range(len(title) - 1):
        bg = title[i : i + 2]
        if bg in _GENERIC_BIGRAMS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", bg) and bg in text:
            return 1
    return 0


def _sched_key(item: dict) -> tuple:
    """改/删时用它把条目定位回去（比对象身份靠谱，重载缓存也不会错）。"""
    return (
        str(item.get("title", "")),
        str(item.get("repeat", "")),
        str(item.get("weekday", item.get("start", ""))),
        str(item.get("time", "")),
    )


def _days_in_month(year: int, month: int) -> int:
    """每月循环碰上「这个月没有 31 号」时要靠它回退到月末。"""
    if month == 2:
        return 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


# 新增日程时要把「帮我记录 / 安排 / 有」这类动词去掉，只留事情本身
_SCHEDULE_VERBS = (
    "帮我", "麻烦你", "麻烦", "请", "到时候", "记得", "提醒我", "帮我记", "记录一下", "记录",
    "记一下", "记下来", "记下", "添加", "新增", "创建", "新建", "排入", "安排", "加",
    "每天", "每", "有个", "有", "我要", "我", "你", "的", "去", "上", "在", "是",
)
_LOCATION = re.compile(r"(?:地点|教室)\s*[:：]?\s*([^，,。;；]+)")
# 标题里不该残留周期说法：「每月5号交房租」的标题应该是「交房租」
_REPEAT_WORDS = re.compile(
    r"(?:每(?:个)?年\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*月\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*[号日]?"
    r"|每(?:个)?月\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*[号日]?"
    r"|每\s*(?:\d{1,3}|[一二三四五六七八九十两]+|半)\s*(?:个)?\s*(?:天|日|小时|钟头|分钟|分)"
    r"|每(?:个)?(?:月|年|天))"
)


def _strip_leading(text: str, words: tuple[str, ...]) -> str:
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


def _split_title_location(text: str) -> tuple[str, str]:
    """从「每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302」里拆出事项和地点。"""
    loc = ""
    m = _LOCATION.search(text or "")
    if m:
        loc = m.group(1).strip()
        if loc in ("有", "是", "的"):
            loc = ""
    head = re.split(r"[，,。;；]", text or "")[0]
    head = _TIME_WORDS.sub("", head)
    head = _WEEKLY.sub("", head)
    head = _REPEAT_WORDS.sub("", head)
    head = re.sub(r"\s+", " ", head)
    return _strip_leading(head, _SCHEDULE_VERBS), loc


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class SkillResult:
    reply: str = ""                      # 要朗读/显示的回答
    action: str = ""                     # 命中的技能，用于日志与调试
    data: dict = field(default_factory=dict)
    speak: bool = True

    def __bool__(self) -> bool:
        return bool(self.reply)


# --------------------------------------------------------------------------- #
# 时间词清洗（从提醒内容里去掉「明天早上七点」这类表达）
# --------------------------------------------------------------------------- #
_NUM = r"(?:\d{1,2}|[一二三四五六七八九十两]+)"
_TIME_WORDS = re.compile(
    r"(?:大后天|后天|明天|明日|今天|今日|今早|明早|今晚|明晚|"
    r"凌晨|早上|早晨|清晨|上午|中午|正午|下午|傍晚|晚上|夜里|夜晚|半夜|"
    r"(?:下{1,2}个?|上个?|这|本)?(?:周|星期|礼拜)[一二三四五六日天末]|"
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
    r"请|你|我|要|去|来|有|一下|一个|个|的|把|给|向|从|和|跟)+"
)
_TAIL_FILLER = re.compile(
    r"(?:这件事|这个事情|一下|到时候|记得|提醒我|叫我|这个|那个|吧|哦|啊|呀|呢|了|的|吗)+" + r"$"
)
# 只剩这些词就说明用户没说具体要干什么
_NOT_A_TOPIC = {
    "", "我", "你", "他", "她", "它", "我们", "你们", "这个", "那个",
    "一下", "一个", "事情", "东西", "时候", "时间", "的", "了",
}

CREATE_ALARM_HINTS = ("提醒", "闹钟", "叫醒", "叫我", "喊我", "定时", "叫一下")
MEMO_HINTS = ("记一下", "记下", "记住", "记下来", "帮我记", "备忘", "记录一下", "提醒我记")
SCHEDULE_WORDS = ("课", "课程", "上课", "会议", "开会", "日程", "安排", "行程", "例会", "组会")

# 语音识别经常把关键词听错一个字，直接放宽字符集比写纠错表更好维护
#   「记一下」常被听成「记以下 / 记一哈 / 记一吓」
TRIGGER_ALARM = re.compile(r"(提醒|提行|提星|醒目|闹钟|闹中|叫醒|叫我|喊我|定时|喊一下)")
TRIGGER_MEMO = re.compile(r"(记\s*[一以衣]?\s*[下住录哈吓夏]|记住|^记得|记录|备忘|备忘记)")
TRIGGER_SCHEDULE = re.compile(r"(课|上课|会议|开会|例会|组会|日程|日成|行程|安排)")

# 「我的备忘有哪些 / 我现在有什么备忘录吗」这类是查询，不是添加。
# 光靠「备忘」两个字判断会把查询误当成新增（之前就出过这个 bug：
# 「我现在有什么备忘录吗」被存成了「录吗？」）。
MEMO_QUERY = re.compile(
    r"(?:备忘|备忘录|记录)[^，。？?!！]{0,6}?(?:有哪些|有什么|是什么|列表|内容|多少|几条|几句|吗|呢)"
    r"|(?:有哪些|有什么|看看|看一下|查一下|查查|列出|多少|几条|几句)[^，。？?!！]{0,4}?(?:备忘|备忘录)"
)
TRIGGER_MEMO_ADD = re.compile(
    r"(?:"
    r"记\s*[一以衣]?\s*[下住录哈吓夏]"          # 记下 / 记一下 / 记以下 / 记一哈
    r"|^记得|记住|记录(?:一下)?|备忘(?!录)(?:一下)?|mark"
    r")\s*[:：,，]?\s*(?P<content>.+)",
    re.S,
)

# 提醒内容里只剩这些词，说明用户没说具体要提醒什么。
# 注意：「起床」是真实会用的内容（「明天七点叫我起床」），不能放进来。
_ACTION_ONLY = re.compile(
    r"^(?:闹钟|闹中|提醒|定时|叫醒|叫我|喊我|喊一下|定在|设在|设成|定成|设为|设置|"
    r"定|设|来|要|有|我|你|一|下|个|把|给|时间|到|了|吧|啊|呀|一个|的|把它|"
    r"给我|提醒我)+" + r"$"
)
DEFAULT_REMINDER_WHAT = "时间到了"

# 判断句子里是否真的包含一个「时刻」表达。
# 不能只依赖 parse_datetime：它把「明天」这种光有日期的说法默认成 09:00，
# 于是「提醒我明天有什么课」会被误当成定时提醒。
CLOCK_EXPR = re.compile(
    r"(?:(?:\d{1,2}|[一二三四五六七八九十两]|半)\s*[点時])"
    r"|(?:[:：]\s*\d{1,2})"
    r"|(?:以后|之后|过后)"
    r"|(?:\d+|[一二三四五六七八九十两]+|半|几)\s*个?\s*(?:小时|钟头|分钟|分|秒)"
)


def has_clock_expr(text: str) -> bool:
    return bool(CLOCK_EXPR.search(text or ""))


def _extract_alarm_what(text: str) -> str:
    """从「提醒我xxx」「闹钟定在…」里抽取出真正要提醒的内容。

    中文既会说「提醒我喝水」（内容在后），也会说
    「我晚上七点有跆拳道课，到时候记得提醒我」（内容在前），
    所以两边都要试，并且把「我」这种纯代词当作无效内容。
    """
    trigger = ("提醒我", "提醒一下", "提醒", "叫醒我", "叫醒", "叫我", "喊我", "叫一下", "喊一下", "闹钟", "闹中", "定时")
    for kw in trigger:
        idx = text.rfind(kw)
        if idx < 0:
            continue
        tail = _clean_content(text[idx + len(kw) :])
        if _is_topic(tail):
            return tail
        head = _clean_content(text[:idx])
        if _is_topic(head):
            return head
    whole = _clean_content(text)
    return whole if _is_topic(whole) else ""


def _is_topic(text: str) -> bool:
    """判断抽出来的内容是不是真的有信息量（不是「我」「一下」这种）。"""
    t = (text or "").strip()
    if len(t) < 2 or t in _NOT_A_TOPIC:
        return False
    return not _ACTION_ONLY.match(t)


def _clean_content(text: str) -> str:
    t = _TIME_WORDS.sub("", text or "")
    t = re.sub(r"^[，。、,.\s:：]+", "", t)
    t = _FILLER.sub("", t)
    t = _TAIL_FILLER.sub("", t)
    t = re.sub(r"[，。、,.\s:：]+", "", t)
    return t.strip()


def _extract_after(text: str, keywords: tuple[str, ...]) -> str:
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            return text[idx + len(kw) :]
    return text


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class Skills:
    def __init__(self, settings: Settings, logger=None) -> None:
        import logging

        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop")
        cfg = settings.skills
        self.enabled = bool(cfg.enabled)
        self.data_dir = settings.resolve(cfg.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.alarms = JsonStore(settings.resolve(cfg.alarm_file), default=[])
        self.memos = JsonStore(settings.resolve(cfg.memo_file), default=[])
        self.schedule = JsonStore(
            settings.resolve(cfg.schedule_file),
            default=[
                {
                    "title": "AIAA3102 机器学习",
                    "kind": "course",
                    "repeat": "weekly",
                    "weekday": 3,
                    "time": "09:00",
                    "duration_minutes": 90,
                    "location": "教学楼 A302",
                    "remind_before": 15,
                    "note": "weekday: 0=周一 … 6=周日；这是示例条目，可直接删除",
                }
            ],
        )
        for store in (self.alarms, self.memos, self.schedule):
            store.ensure()

        # 最近的对话（你说 + 助手答），用来解析「它 / 那个 / 刚才那条」指谁。
        # 由 pipeline 每轮传进来；单独跑 skills 时就是空的。
        self.dialog: list[str] = []

        # 看图（摄像头 / 屏幕 / 剪贴板 / 文件）
        vcfg = settings.vision
        self.vision_cfg = vcfg
        self.vision = Vision(vcfg, settings.root, self.log) if vcfg.enabled else None
        self._vision_pending: dict | None = None   # 「是这个文件吗？」等着回答
        self._vision_shot: dict | None = None      # 最近一次「看了什么」

    # ======================================================================
    # 入口
    # ======================================================================
    def handle(self, text: str, dialog: list[str] | None = None) -> SkillResult | None:
        """尝试用技能回答；返回 None 表示应该交给 LLM。

        ``dialog``：最近几轮「你说 / 助手答」的原文，指代解析（它、那个、刚才那条）
        在上面找候选——技能本身不猜，找不到就问。
        """
        if dialog is not None:
            self.dialog = [str(x) for x in dialog if str(x).strip()][-10:]
        if not self.enabled:
            return None
        raw = (text or "").strip()
        if not raw:
            return None
        now = datetime.now()
        for fn in (
            self._handle_vision_reply,   # 先接「是 / 第一个」这种确认，别被别的技能抢走
            self._handle_help,
            self._handle_screen,
            self._handle_clock,
            self._handle_vision,         # 看图（带时间词的会让给日程）
            self._handle_alarm,
            self._handle_schedule,
            self._handle_memo,
        ):
            try:
                result = fn(raw, now)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"技能 {fn.__name__} 出错：{exc}")
                continue
            # 看图这类结果不带 reply（要交给视觉模型出话），所以 data 也算命中
            if result is not None and (result.reply or result.data):
                result.action = result.action or fn.__name__.replace("_handle_", "")
                self.log.info(f"[技能:{result.action}] {raw} -> {result.reply[:60]}")
                return result
        return None

    # ======================================================================
    # 时间 / 日期
    # ======================================================================
    _TIME_Q = re.compile(
        r"(现在|此刻|当前)?\s*(几点了?|几点钟|几电|几点拉|什么时间|什么时刻|时间是多少|报一下?时)"
    )
    _DATE_Q = re.compile(r"(今天|现在)\s*(是)?\s*(几号|多少号|什么日期|几月几号|日期)")
    _WEEK_Q = re.compile(r"(今天|现在|这周|本周)?\s*(是)?\s*(星期几|周几|礼拜几)")
    _DATETIME_Q = re.compile(r"(今天|现在)\s*(是)?\s*(什么日子|星期几|几号|日期|时间)")

    def _handle_clock(self, text: str, now: datetime) -> SkillResult | None:
        want_time = bool(self._TIME_Q.search(text))
        want_date = bool(self._DATE_Q.search(text))
        want_week = bool(self._WEEK_Q.search(text))
        if not (want_time or want_date or want_week):
            return None

        parts: list[str] = []
        if want_time or (not want_date and not want_week):
            parts.append(f"现在是{clock_text(now)}")
        if want_date or want_week:
            weekday = WEEKDAY_FULL[now.weekday()]
            if want_date and want_week:
                parts.append(f"今天是{now.month}月{now.day}日，{weekday}")
            elif want_date:
                parts.append(f"今天是{now.month}月{now.day}日")
            else:
                parts.append(f"今天是{weekday}")
        return SkillResult(reply="，".join(parts) + "。", action="clock")

    # ======================================================================
    # 闹钟 / 定时提醒
    # ======================================================================
    _ALARM_LIST_Q = re.compile(
        r"(有哪些|有什么|还有|看看|查一下|列出|列表|几个|多少)?\s*(提醒|闹钟|定时|定时器)\s*(有哪些|有什么|列表|是什么|吗|呢)*\s*[?？]?$"
    )
    _ALARM_CANCEL = re.compile(
        r"(?:取消|删除|去掉|关掉|清除|清空|不要|别再)\s*"
        r"(?:第\s*(?P<idx>\d+|[一二三四五六七八九十]+)\s*[个条])?\s*"
        r"(?P<all>所有|全部|这些|所有的)?\s*"
        r"(?:提醒|闹钟|定时)"
    )

    def _handle_alarm(self, text: str, now: datetime) -> SkillResult | None:
        # --- 取消 ---
        m = self._ALARM_CANCEL.search(text)
        if m:
            idx_raw, all_raw = m.group("idx"), m.group("all")
            if all_raw and not idx_raw:
                n = self.alarms.clear()
                return SkillResult(reply=f"已清空全部 {n} 条提醒。", action="alarm_clear")
            if all_raw and idx_raw:
                # 「取消所有第1个」这种说法，按“取消所有”处理
                n = self.alarms.clear()
                return SkillResult(reply=f"已清空全部 {n} 条提醒。", action="alarm_clear")
            idx = cn2num(idx_raw) if idx_raw else None
            if not idx:
                return None          # 「取消提醒」但没说是哪一条 -> 交给下游或让用户说清楚
            removed = self.alarms.remove_at(idx)
            if removed:
                when = humanize(datetime.fromisoformat(removed["when"]), now)
                return SkillResult(
                    reply=f"已撤销第{cn_number(idx)}条提醒：{when}{removed.get('what', '')}。",
                    action="alarm_cancel",
                )
            return SkillResult(reply=f"没有第{cn_number(idx)}条提醒。", action="alarm_cancel")

        # --- 查询 ---
        # 注意：「提醒我明天有什么课」里的「提醒」不是查提醒列表，
        # 所以要求查询词紧跟在提醒/闹钟之后，并且句子里没有日程关键词。
        has_hint = bool(TRIGGER_ALARM.search(text))
        is_list_q = bool(self._ALARM_LIST_Q.search(text)) or (
            has_hint
            and not TRIGGER_SCHEDULE.search(text)
            and bool(re.search(r"(提醒|闹钟|定时)\s*我?\s*(有哪些|有什么|还有|看看|列出|几个|多少)", text))
        )
        if is_list_q and not self._looks_like_create(text):
            return self._list_alarms(now)

        if not has_hint:
            return None

        # 「每周三…提前一天和半小时提醒我」「每2小时提醒我喝水」这种带重复周期、
        # 或者要好几次提前提醒的，属于日程（日程能存周期和多个提前量），
        # 闹钟只管一次性。
        if (parse_repeat(text) or {}).get("repeat", "once") != "once":
            return None
        if len(parse_reminds(text) or []) > 1:
            return None

        # --- 新建 ---
        if not has_clock_expr(text):
            # 「提醒我买牛奶」没说时间：不是定时提醒，交给后面的技能或 LLM
            return None
        when = parse_datetime(text, now)
        if when is None:
            return None

        # 「今天早上八点半」在已经十点的时候说，是倒不回过去的：
        # 顺延到明天同一时刻，并且明确告诉用户，免得他以为设错了。
        rolled = False
        if (when - now).total_seconds() < -60:
            when = when + timedelta(days=1)
            rolled = True

        what = _extract_alarm_what(text)
        delta = (when - now).total_seconds()
        when_text = humanize(when, now)
        prefix = "那个时间今天已经过了，" if rolled else ""

        if not what:
            # 只说「定个八点半的闹钟」没说干什么：别再鹦鹉学舌地把命令念回去
            what = DEFAULT_REMINDER_WHAT
            item = self.alarms.append(
                {
                    "when": when.strftime("%Y-%m-%d %H:%M:%S"),
                    "what": what,
                    "fired": False,
                    "kind": "alarm",
                }
            )
            return SkillResult(
                reply=f"{prefix}闹钟已定：{when_text}，{humanize_delta(delta)}后。",
                action="alarm_add",
                data=item,
            )

        item = self.alarms.append(
            {
                # 用秒级时间戳，「十分钟后」这种才不会被截断到整分钟
                "when": when.strftime("%Y-%m-%d %H:%M:%S"),
                "what": what,
                "fired": False,
                "kind": "alarm",
            }
        )
        return SkillResult(
            reply=f"{prefix}已记录。{when_text}，也就是{humanize_delta(delta)}后，我会提醒你{what}。",
            action="alarm_add",
            data=item,
        )

    def _looks_like_create(self, text: str) -> bool:
        return has_clock_expr(text)

    def _list_alarms(self, now: datetime) -> SkillResult:
        items = [it for it in self.alarms.load() if not it.get("fired")]
        items.sort(key=lambda x: x.get("when", ""))
        if not items:
            return SkillResult(reply="当前没有待处理的提醒。", action="alarm_list")
        lines = []
        for i, it in enumerate(items[:5], 1):
            try:
                when = datetime.fromisoformat(it["when"])
            except Exception:  # noqa: BLE001
                continue
            lines.append(f"第{cn_number(i)}条，{humanize(when, now)}，{it.get('what', '')}")
        tail = "" if len(items) <= 5 else f"，还有{len(items) - 5}条"
        return SkillResult(
            reply=f"待处理提醒{cn_quantity(len(items))}条。" + "；".join(lines) + tail + "。",
            action="alarm_list",
        )

    # ======================================================================
    # 备忘
    # ======================================================================
    _MEMO_ADD = TRIGGER_MEMO_ADD
    _MEMO_LIST = re.compile(
        r"(?:我的|看看|查一下|列出|有哪些|有什么)?\s*备忘(?:录)?\s*(?:有哪些|有什么|是什么|列表|记录|内容|吗|呢)?\s*[?？]?$"
    )
    _MEMO_CANCEL = re.compile(
        r"(?:删除|删掉|去掉|清除|清空|忘掉|不要|划掉)\s*(?:第\s*(\d+|[一二三四五六七八九十]+)\s*(?:个|条))?\s*(?:所有|全部)?\s*(?:备忘|记录)"
    )

    def _handle_memo(self, text: str, now: datetime) -> SkillResult | None:
        m = self._MEMO_CANCEL.search(text)
        if m:
            idx_raw = m.group(1)
            if idx_raw:
                idx = cn2num(idx_raw)
                removed = self.memos.remove_at(idx) if idx else None
                if removed:
                    return SkillResult(reply=f"已删除第{cn_number(idx)}条备忘。", action="memo_cancel")
                return SkillResult(reply=f"没有第{cn_number(idx)}条备忘。", action="memo_cancel")
            n = self.memos.clear()
            return SkillResult(reply=f"已清空全部 {n} 条备忘。", action="memo_clear")

        # 查询要在添加之前判定：“我现在有什么备忘录吗”不能被当成新增
        if MEMO_QUERY.search(text) or self._MEMO_LIST.search(text.strip()):
            items = self.memos.load()
            if not items:
                return SkillResult(reply="目前没有备忘。", action="memo_list")
            lines = [f"第{cn_number(i)}条，{it.get('content', '')}" for i, it in enumerate(items[:6], 1)]
            tail = "" if len(items) <= 6 else f"，还有{len(items) - 6}条"
            return SkillResult(
                reply=f"备忘{cn_quantity(len(items))}条。" + "；".join(lines) + tail + "。", action="memo_list"
            )

        m = self._MEMO_ADD.search(text)
        if m:
            content = _clean_content(m.group("content"))
            if not content:
                return None
            self.memos.append({"content": content, "done": False})
            # 「记一下明天要买牛奶」这类带时间的，顺带提醒一下
            when = parse_datetime(text, now)
            if when is not None and TRIGGER_ALARM.search(text):
                self.alarms.append(
                    {
                        "when": when.strftime("%Y-%m-%d %H:%M:%S"),
                        "what": content,
                        "fired": False,
                        "kind": "alarm",
                    }
                )
                return SkillResult(
                    reply=f"已归档：{content}。{humanize(when, now)}我会提醒你。", action="memo_add_alarm"
                )
            return SkillResult(reply=f"已归档：{content}。", action="memo_add")

        # 「提醒我买牛奶」这类没时间的 -> 存成备忘
        if TRIGGER_ALARM.search(text) and not has_clock_expr(text):
            content = _clean_content(_extract_after(text, ("提醒我", "提醒", "叫我")))
            if content:
                self.memos.append({"content": content, "done": False})
                return SkillResult(
                    reply=f"先归档为备忘：{content}。需要定时提醒的话，告诉我具体时间。",
                    action="memo_add",
                )
        return None

    # ======================================================================
    # 看图：摄像头 / 屏幕 / 剪贴板 / 文件
    # ======================================================================
    # 能看图的前提：句子里既要有「看」的动作，又要指向某个对象（这个/屏幕/文件…）。
    # 只有「看看」不够，否则「看看新闻」会被当成拍照。
    _VISION_ASK = re.compile(
        r"(?:看看|看一下|看下|看一眼|瞅瞅|瞧瞧|拍一张|拍个照|拍照|打开看看|看一看)"
    )
    # 「读 / 念」这类几乎总是对着文件说的（「念一下会议纪要」），
    # 所以不要求句子里出现「文件」两个字；但带时间词 + 日程词的会让给日程技能
    _VISION_READ = re.compile(r"(?:读一下|读读|读一读|念一下|念一?遍|念给我|给我读|给我念)")
    # 看得到的东西（屏幕 / 文件 / 这个 / 上面…）
    _VISION_OBJECT = re.compile(
        r"(?:这个|那个|这一?张|那一?张|这幅|这里|这儿|画面|镜头|摄像头|相机|"
        r"照片|图片|截图|屏幕|显示器|桌面|窗口|界面|剪贴板|"
        r"文件|文档|附件|报告|表格|日志|脚本|代码|内容|"
        r"上面|上头|里头|里面|手里|手上|桌上|窗外|镜头前)"
    )
    # 在问「是什么」——但必须同时用手指着（这/那/它）或提到看得到的东西，
    # 否则「明天是什么天气」也会被当成看图
    _VISION_WHAT = re.compile(r"(?:是什么|是啥|什么东西|写了什么|写的什么|有什么|讲了什么|什么颜色)")
    _VISION_POINT = re.compile(r"[这那此它他她]")
    _V_SRC_LAST = re.compile(r"(?:刚才(?:那|的)?(?:张|幅)?图|上一张|刚才拍的|刚刚拍的|刚才看到的)")
    _V_SRC_CLIP = re.compile(r"(?:剪贴板|复制的东西|复制的那|刚复制的|我复制的)")
    _V_SRC_FILE = re.compile(
        r"(?:\.(?:txt|md|py|json|csv|tsv|log|pdf|docx?|xlsx?|pptx?|ini|toml|ya?ml|xml|html|bat|ps1|sh|c|cpp|h|java|js|ts)\b"
        r"|文件|文档|附件|pdf|PDF|word|excel|表格|脚本|源代码)"
    )
    _V_SRC_SCREEN = re.compile(r"(?:屏幕|显示器|桌面|窗口|界面|屏上|画面上)")
    _V_SRC_CAMERA = re.compile(r"(?:摄像头|镜头|相机|摄像机|拍照|拍一张|拍个照|前面|手里|手上|桌上|窗外)")
    _V_YES = re.compile(r"^(?:嗯+|哦+|是|是的|对|对的|对呀|好|好的|好啊|行|可以|要|读吧|看吧|打开吧|"
                        r"ok|OK|Ok|okay|yes|yeah|yep)$")
    _V_NO = re.compile(r"^(?:不是|不对|不用|不要|算了|取消|别了|不看|不读了|不看了|换个|no|nope)$")
    _V_ORDINAL = re.compile(r"^(?:第\s*)?([一二三123])\s*(?:个|条|张|份|号)?$")
    _V_PUNCT = re.compile(r"[\s，,。.！!？?、；;~〜]+")

    def _handle_vision(self, text: str, now: datetime) -> SkillResult | None:
        """「看看这是什么」「看看我的屏幕」「读一下那个报告」。

        这里只负责**决定看什么**（拍图 / 截图 / 找到文件），真正的描述
        交给 pipeline 调视觉模型——技能不猜内容。
        """
        cfg = self.vision_cfg
        if not cfg.enabled or self.vision is None:
            return None
        look = bool(self._VISION_ASK.search(text))       # 看看 / 拍一张 / 打开看看
        read = bool(self._VISION_READ.search(text))      # 读一下 / 念给我
        last = bool(self._V_SRC_LAST.search(text))       # 刚才那张图
        obj = bool(self._VISION_OBJECT.search(text))     # 屏幕 / 文件 / 这个
        what = bool(self._VISION_WHAT.search(text))      # 是什么 / 写了什么
        point = bool(self._VISION_POINT.search(text))    # 这 / 那 / 它：在指着东西说
        wanted = (
            read                                          # 「读一下会议纪要」
            or last                                       # 「刚才那张图」
            or (look and (obj or what))                   # 「看看我的屏幕」「看看这是什么」
            or (what and (obj or point))                  # 「屏幕上写了什么」「它是什么颜色」
        )
        if not wanted:
            return None
        # 「看看今天的日程」「念一下明天的安排」是在问日程，不是在读文件：
        # 带时间词 + 日程词的一律让给日程技能（它在后面）
        if self._RANGE_WORD.search(text) and (
            TRIGGER_SCHEDULE.search(text) or _weekly_weekday(text) is not None
        ):
            return None
        source = self._vision_source(text, read=read)
        try:
            if source == "file":
                return self._vision_ask_file(text)
            if source == "last":
                shot = self.vision.last
                if shot is None:
                    return SkillResult(
                        reply="我还没看过什么东西。说「看看我的屏幕」或者「用摄像头看看」？",
                        action="vision_miss",
                    )
                return self._vision_look(shot.path, shot.what, text)
            if source == "screen":
                shot = self.vision.screen()
            elif source == "clipboard":
                shot = self.vision.clipboard()
            else:
                shot = self.vision.camera()
            return self._vision_look(shot.path, shot.what, text)
        except VisionError as exc:
            return SkillResult(reply=str(exc), action="vision_error")

    def _vision_source(self, text: str, read: bool = False) -> str:
        """这句话想在哪儿看：camera / screen / clipboard / file / last。"""
        if self._V_SRC_LAST.search(text):
            return "last"
        if self._V_SRC_CLIP.search(text):
            return "clipboard"
        if self._V_SRC_FILE.search(text):
            return "file"
        if self._V_SRC_SCREEN.search(text):
            return "screen"
        if self._V_SRC_CAMERA.search(text):
            return "camera"
        if read:
            return "file"                  # 「读一下会议纪要」= 去文件里找这个
        # 没说来源（「这是什么」）→ 按配置来；拿不准就去问清楚反而更膈应人
        default = str(self.vision_cfg.default_source or "camera").lower()
        return default if default in ("camera", "screen") else "camera"

    # ------------------------------------------------------------ 文件
    def _vision_ask_file(self, text: str) -> SkillResult:
        """文件先解析出**完整路径**再问一句，确认了才读。"""
        assert self.vision is not None
        hits = self.vision.find_file(text)
        name = self.vision.query_name(text)
        if not hits:
            roots = "、".join(p.name for p in self.vision.roots()) or "（未配置）"
            what = f"「{name}」" if name else "那个文件"
            return SkillResult(
                reply=f"没找到叫{what}的文件。我只在 {roots} 里找——说清楚文件名，或者把完整路径念给我？",
                action="vision_miss",
            )
        self._vision_pending = {"hits": hits, "at": time.monotonic()}
        best = hits[0]
        if len(hits) > 1 and hits[1].score >= best.score - 12:
            listing = "；".join(f"{i}) {self.vision.describe(h)}" for i, h in enumerate(hits[:3], 1))
            return SkillResult(
                reply=f"找到几个对得上的：{listing}。是哪个？说「第一个」或者直接把名字再说一遍。",
                action="vision_ask",
            )
        return SkillResult(
            reply=f"你是说 {self.vision.describe(best)}？说「是」我就打开看。",
            action="vision_ask",
        )

    def _handle_vision_reply(self, text: str, now: datetime) -> SkillResult | None:
        """接住「是这个文件吗？」的下一句：是 / 不是 / 第一个 / 再报个名字。"""
        pend = self._vision_pending
        if not pend:
            return None
        if time.monotonic() - float(pend.get("at", 0.0)) > float(self.vision_cfg.confirm_expire):
            self._vision_pending = None
            return None
        raw = self._V_PUNCT.sub("", (text or "").strip())
        if not raw or len(raw) > 16:
            # 说的是别的新指令，这件事就算过去了
            self._vision_pending = None
            return None
        hits = list(pend.get("hits") or [])
        if self._V_YES.match(raw):
            self._vision_pending = None
            if not hits:
                return None
            return self._vision_open(hits[0].path, text)
        if self._V_NO.match(raw):
            self._vision_pending = None
            return SkillResult(reply="好，那我不看了。", action="vision_cancel")
        ordinal = self._V_ORDINAL.match(raw)
        if ordinal:
            idx = "一二三".find(ordinal.group(1))
            if idx < 0:
                idx = int(ordinal.group(1)) - 1
            if 0 <= idx < len(hits):
                self._vision_pending = None
                return self._vision_open(hits[idx].path, text)
        # 「打开那个报告」——名字要在候选里对得上，否则不猜
        want = norm_name(raw)
        for h in hits:
            if want and want in norm_name(h.name):
                self._vision_pending = None
                return self._vision_open(h.path, text)
        self._vision_pending = None
        return None

    # ------------------------------------------------------------ 交给模型
    def _vision_open(self, path, user_text: str) -> SkillResult:
        """用户确认过了：读文件 / 看图，包装成 pipeline 能直接送给模型的形式。"""
        assert self.vision is not None
        p = path if isinstance(path, Path) else Path(str(path))
        try:
            text, kind = self.vision.read_file(p)
        except VisionError as exc:
            return SkillResult(reply=str(exc), action="vision_error")
        if kind == "image":
            return self._vision_look(p, f"图片《{p.name}》", user_text)
        if kind == "empty":
            return SkillResult(reply=f"{p.name} 是个空文件，没什么可看的。", action="vision_empty")
        return self._vision_text_result(p, text, user_text)

    def _vision_look(self, path, what: str, user_text: str) -> SkillResult:
        """看图：把图编码成 base64 交给视觉模型（pipeline 负责真的去调）。"""
        assert self.vision is not None
        suffix = str(path).lower().rsplit(".", 1)[-1]
        if suffix in ("png", "jpg", "jpeg", "bmp", "webp", "gif", "tif", "tiff"):
            try:
                images = [self.vision.encode(path)]
            except VisionError as exc:
                return SkillResult(reply=str(exc), action="vision_error")
            prompt = (
                f"用户让你看{what}。用户说：「{user_text}」\n"
                "请用中文口语回答，两到三句，不要用列表、不要 markdown。\n"
                "如果用户没问具体问题，就说你看到了什么：有什么东西、上面有没有文字、大概什么颜色。\n"
                "看不清楚就直说看不清，不要编。"
            )
            self._vision_shot = {"path": str(path), "what": what}
            return SkillResult(
                reply="",
                action="vision",
                data={"images": images, "prompt": prompt, "shot": str(path),
                      "what": what, "note": str(self.vision_cfg.say_first or "")},
            )
        # 不是图片（PDF/Word/文本）→ 抽文本即可，用不着视觉模型
        try:
            text, _kind = self.vision.read_file(path)
        except VisionError as exc:
            return SkillResult(reply=str(exc), action="vision_error")
        return self._vision_text_result(path, text, user_text)

    def _vision_text_result(self, path, text: str, user_text: str) -> SkillResult:
        """文本文件：把内容（截断）交给普通模型就行。"""
        limit = max(200, int(self.vision_cfg.file_max_chars))
        body = text[:limit]
        more = "（内容太长，这里只是前面一部分）" if len(text) > limit else ""
        lines = text.count("\n") + 1
        prompt = (
            f"用户让你看一个文件的全文：{getattr(path, 'name', path)}（约 {lines} 行）{more}\n"
            "----- 文件内容开始 -----\n"
            f"{body}\n"
            "----- 文件内容结束 -----\n"
            f"用户说：「{user_text}」\n"
            "请用中文口语回答，两到三句，不要用列表、不要 markdown。\n"
            "用户没问具体问题时，就说这个文件是做什么的。"
        )
        self._vision_shot = {"path": str(getattr(path, "name", path)), "what": "文件"}
        return SkillResult(
            reply="",
            action="vision",
            data={"images": [], "prompt": prompt, "shot": str(path), "what": "文件",
                  "num_ctx": int(self.vision_cfg.file_num_ctx),
                  "note": str(self.vision_cfg.say_first or "")},
        )

    # ======================================================================
    # 课程 / 会议 / 日程
    # ======================================================================
    # 「改 / 挪 / 删 / 取消」必须**先于**新增判断：以前「删掉每周三上午九点那节课」
    # 因为句子里同时有「每周三 + 时刻」被当成新增，反手往课表里塞了一条
    # 叫「删掉每…那节课」的垃圾条目。
    _SCHEDULE_EDIT = re.compile(
        r"(?:改到|改成|改为|改在|挪到|挪成|挪去|换到|调到|调成|提前到|推迟到|提前|推迟|往后推)"
    )
    _SCHEDULE_DROP = re.compile(
        r"(?:取消|删掉|删除|去掉|清掉|不上|不去|不参加|不开了|上不了)"
    )
    # 「以后不上这门课了」= 删掉；只说「这周三不上了」= 只跳过这一次
    _SCHEDULE_FOREVER = re.compile(r"(?:以后|往后都|再也不|不再|一直|永久|全部|所有|这门课|这一门)")
    # 批量说法里的「所有 / 全部」只是**范围**词，不能当成「以后都不上」。
    # （_SCHEDULE_FOREVER 里含「所有」，那是给单条删用的；批量必须用这个。）
    _FOREVER_WORDS = re.compile(r"(?:以后|往后|永远|再也不|不再|永久|彻底|从此)")
    # 带这些词的是问句，不要当成「取消」（「今天的课不上吗？」不能真去取消）
    _SCHEDULE_QUESTION = re.compile(r"(?:有什么|有哪些|有没有|列表|列出|查一下|查询|看看|看一下)|[吗呢？?]\s*$")
    _SCHEDULE_Q = re.compile(
        r"(?:今天|明天|后天|本周|这周|下周|下个星期|这个星期)?\s*(?:有什么|有哪些|有没有|安排|日程|行程|列表|看看|查一下)?\s*"
        r"(?:课|课程|上课|会议|开会|日程|日成|安排|行程|例会|组会)"
    )
    _SCHEDULE_NEXT = re.compile(r"(下一个|下一节|接下来|最近的?)\s*(?:课|课程|会议|开会|日程|安排|例会|组会)")
    # 指代词：出现这些词说明「哪一条」不在这一句里，要去看聊天记录
    _REFER_WORDS = re.compile(
        r"(?:它|他|她|这个|那个|这条|那条|这节|那节|这门|那门|刚才|刚刚|上面|前面|前面说|刚说|刚才说)"
    )
    # 批量说法：「所有课程」「全部会议」「所有日程」
    _ALL_WORDS = re.compile(r"(?:所有|全部|一切|每个|各个|所有的|全部的)")
    _SCOPE_COURSE = re.compile(r"(?:课程|课表|课)")
    _SCOPE_MEETING = re.compile(r"(?:会议|开会|例会|组会)")
    _SCOPE_TASK = re.compile(r"(?:任务|待办|要交的)")
    _SCOPE_ANY = re.compile(r"(?:日程|安排|行程|事情)")
    # 「有哪些课程」「一共有几门课」这种纯查询说法（没有「所有」也能问全体）
    _SCOPE_ASK = re.compile(r"(?:有(?:哪些|什么|多少)|都有|一共|总共|总共|几门|几节|几个)")
    # 句子里出现这些词就是在问某段时间，别按「全体」回答
    _RANGE_WORD = re.compile(
        r"(?:今天|明天|后天|大后天|这周|本周|下周|下下周|上周|周末|这个月|本月|下个月|上个月"
        r"|今年|明年|未来|这几天|这两天|最近)"
    )
    # 只说了时间段、没带「课/会议」这类词的问句：「下周呢」「这个月有什么」
    _RANGE_ASK = re.compile(
        r"(?:今天|今日|明天|后天|大后天|这周|本周|下周|下下周|这个月|本月|下个月|今年|明年"
        r"|未来|接下来|最近|这几天|这两天|这个?周末|下个?周末)"
        r"[^，,。;；?？]{0,8}(?:有什么|有哪些|有没有|有课|安排|日程|行程|吗|呢|咋样|怎么样)"
    )
    # 问句里也可能带「每周五下午两点」这种时刻，不能当成新增
    _SCHEDULE_ASK = re.compile(r"(有什么|有哪些|有没有|是什么|多少|查一下|查询|看看|看一下|列出|列表|吗|呢)")
    # 「帮我记录晚上7点半有跆拳道课」「明天下午三点安排组会」都应该当新增
    _SCHEDULE_ADD = re.compile(
        r"(?:添加|新增|创建|新建|安排|排入|记录|记一下|记下|记下来|加)\s*"
        r"(?:一个|一条|一门|一节|个)?\s*"
        r"(?:会议|日程|课程|课|安排|提醒|事情|待办)?\s*"
        r"[:：,，]?\s*(?P<content>.+)",
        re.S,
    )

    def _handle_schedule(self, text: str, now: datetime) -> SkillResult | None:
        # 改 / 删 / 跳过 先判，别让它们掉进新增分支
        change = self._handle_schedule_change(text, now)
        if change is not None:
            return change

        # 先判新增。「每周三上午九点有 AIAA3102」这种既没有「记录/安排」动词、
        # 也没有「课/会议」字眼，所以带「每周/每月/每年/每N天」+ 时刻的描述要单独放行，
        # 否则会被 _SCHEDULE_Q 之类的查询规则抢走。
        m = self._SCHEDULE_ADD.search(text)
        rule = parse_repeat(text)
        wd = int(rule["weekday"]) if rule and rule.get("weekday") is not None else _weekly_weekday(text)
        is_query = bool(self._SCHEDULE_ASK.search(text))
        looks_add = (m is not None and TRIGGER_SCHEDULE.search(text)) or rule is not None
        if not is_query and looks_add and (has_clock_expr(text) or rule is not None):
            first = parse_datetime(text, now)
            if first is None:
                # 只说了周期没说时刻（「每天提醒我吃药」）→ 默认上午九点
                first = datetime.combine(now.date(), datetime.min.time()).replace(hour=9, minute=0)
            title, location = _split_title_location(text)
            if not _is_topic(title):
                title = _clean_content(m.group("content")) if m else ""
            if not _is_topic(title):
                title = _clean_content(text)
            if not _is_topic(title):
                title = "日程"
            # 多个提前提醒：「提前一天和半小时提醒我」→ [1440, 30]
            leads = parse_reminds(text) or [int(self.settings.skills.default_remind_before)]
            until = parse_until(text, now)
            note = self._note_of(text)
            repeat = (rule or {}).get("repeat", "once")

            if wd is not None and repeat in ("weekly", "biweekly"):
                item: dict = {
                    "title": title,
                    "kind": "course",
                    "repeat": repeat,
                    "weekday": wd,
                    "time": f"{first.hour:02d}:{first.minute:02d}",
                    "duration_minutes": 90,
                    "remind_before": leads,
                }
                if repeat == "biweekly":
                    item["start"] = first.strftime("%Y-%m-%d %H:%M")   # 双周要有锚点才知道相位
                self._finish_item(item, location, until, note)
                self.schedule.append(item)
                return SkillResult(
                    reply=(
                        f"已排入课表：{self._repeat_text(item)}，{title}{self._where_text(location)}。"
                        f"最近一次是{humanize(first, now)}，{self._remind_text_of(leads)}"
                        f"{self._until_text(until)}"
                    ),
                    action="schedule_add_weekly",
                )

            kind = "meeting" if repeat == "once" else ("course" if repeat in ("weekly", "biweekly") else "task")
            item = {
                "title": title,
                "kind": kind,
                "repeat": repeat,
                "start": first.strftime("%Y-%m-%d %H:%M"),
                "time": f"{first.hour:02d}:{first.minute:02d}",
                "remind_before": leads,
            }
            if repeat == "interval":
                # 「每 3 天一次」不默认占时长，周期就是「结束后 N 天」里的 N
                item["duration_minutes"] = 0
            for key in ("weekday", "day", "month", "every_days", "every_minutes"):
                if rule and rule.get(key) is not None:
                    item[key] = rule[key]
            if rule and rule.get("weekday") is not None and "weekday" not in item:
                item["weekday"] = rule["weekday"]
            self._finish_item(item, location, until, note)
            # 说的是「每月5号」时 first 可能只是「今天九点」，真正第一次要按规则算
            real_first = next(iter(self._starts_from(item, now)), first)
            self.schedule.append(item)
            head = (
                f"已排入日程：{self._repeat_text(item)}"
                if repeat != "once"
                else f"已排入日程：{humanize(real_first, now)}"
            )
            return SkillResult(
                reply=(
                    f"{head}，{title}{self._where_text(location)}。"
                    f"第一次是{humanize(real_first, now)}，{self._remind_text_of(leads)}"
                    f"{self._until_text(until)}"
                ),
                action="schedule_add",
            )

        if self._SCHEDULE_NEXT.search(text):
            return self._next_item(now)

        listed = self._list_scope(text, now)
        if listed is not None:
            return listed

        if self._SCHEDULE_Q.search(text) or self._RANGE_ASK.search(text):
            return self._day_items(text, now)

        return None

    # ------------------------------------------------------------ 日程改 / 删 / 跳过
    def _handle_schedule_change(self, text: str, now: datetime) -> SkillResult | None:
        """处理「改到…」「取消…那节课」「这周三不上了」「把所有会议删掉」。

        返回 None = 这句话跟改/删无关，交给后面的新增与查询。
        """
        drop = self._SCHEDULE_DROP.search(text)
        edit = self._SCHEDULE_EDIT.search(text)
        if not drop and not edit:
            return None
        if self._SCHEDULE_QUESTION.search(text):
            return None                     # 「今天的课不上吗？」是问句，别真去取消
        items = self.schedule.load()
        if not items:
            return SkillResult(
                reply="日程里现在还是空的，没什么可以改的。", action="schedule_change"
            )

        # --- 批量：所有课程 / 所有会议 / 所有日程 ---
        pred, scope = self._scope_filter(text)
        if pred is not None:
            targets = [it for it in items if pred(it)]
            if not targets:
                return SkillResult(reply=f"日程里没有{scope}可以改。", action="schedule_change")
            if edit:
                return self._batch_edit(targets, scope, text, now)
            return self._batch_drop(targets, scope, text, now)

        hits = self._match_schedule_items(text, items, now)
        if not hits:
            # 这一句里没有名字：看聊天记录（「把它改到明天」「取消刚才那个」）
            refs = self._referenced_items(text, now)
            if len(refs) == 1:
                hits = [(4, refs[0])]
            elif len(refs) > 1:
                names = "、".join(f"「{it.get('title', '安排')}」" for it in refs[:4])
                return SkillResult(
                    reply=f"你说的「它」是指哪一条？（{names}）说个名字我就动手。",
                    action="schedule_change",
                )
            elif self._REFER_WORDS.search(text):
                return SkillResult(
                    reply="我这边没有可以指代的日程——先说一句「今天有什么课」，或者把名字说清楚？",
                    action="schedule_change_miss",
                )
        if not hits:
            # 听着像在说日程，但库里找不到对应条目：说清楚比乱改好
            if drop and (TRIGGER_SCHEDULE.search(text) or _weekly_weekday(text) is not None):
                return SkillResult(
                    reply="没找到你说的那条日程。先问一句「今天有什么课」，或者把名字说清楚一点？",
                    action="schedule_change_miss",
                )
            return None

        top = hits[0][0]
        if drop and top < 2:
            # 删除是不可逆的：名字没对上（只沾到两个字）就先问清楚，别猜
            names = "、".join(f"「{it.get('title', '安排')}」" for _s, it in hits[:4])
            return SkillResult(
                reply=f"我不太确定你说的是哪一条（可能是{names}）。说清楚名字、或者带上星期几试试？",
                action="schedule_change_unsure",
            )

        same = [it for s, it in hits if s == top]
        if len(same) > 1:
            names = "、".join(f"「{it.get('title', '安排')}」" for it in same[:4])
            return SkillResult(
                reply=f"有 {len(same)} 条都对得上（{names}），你说是哪一条？",
                action="schedule_change",
            )

        item = same[0]
        title = str(item.get("title") or "安排")
        key = _sched_key(item)
        rep = self._repeat_of(item)
        weekly = rep in ("weekly", "biweekly")

        # --- 删：以后都不上了 ---
        if drop and not weekly:
            self.schedule.remove_where(lambda it: _sched_key(it) == key)
            return SkillResult(reply=f"已删除日程：{title}。", action="schedule_delete")
        if drop and self._SCHEDULE_FOREVER.search(text):
            self.schedule.remove_where(lambda it: _sched_key(it) == key)
            return SkillResult(
                reply=f"已删除日程：{title}，以后不会再提醒了。", action="schedule_delete"
            )

        # --- 跳过：只取消最近这一次，课表本身留着 ---
        if drop and weekly:
            day = self._skip_day(text, item, now)
            if day < now.date():
                # 「这周三」在周五说已经是过去了：不猜，问清楚（取消是不可逆的）
                return SkillResult(
                    reply=(
                        f"{day.month}月{day.day}日（{WEEKDAY_NAMES[day.weekday()]}）已经过去了。"
                        f"你是想取消下一次吗？说「下次的{title}不上了」就行。"
                    ),
                    action="schedule_change_miss",
                )
            skips = [str(d) for d in (item.get("skip") or []) if d]
            if str(day) in skips:
                return SkillResult(
                    reply=f"{day.month}月{day.day}日的{title}本来就没安排。", action="schedule_skip"
                )
            skips.append(str(day))
            self._update_schedule_item(key, skip=skips)
            return SkillResult(
                reply=f"好，{humanize(self._at(item, day), now)}的{title}不提醒了，下周照常。",
                action="schedule_skip",
            )

        if drop:
            self.schedule.remove_where(lambda it: _sched_key(it) == key)
            return SkillResult(reply=f"已删除日程：{title}。", action="schedule_delete")

        # --- 改：时间 / 星期 / 地点 ---
        day = parse_date_hint(text, now)
        when = parse_datetime(text, now)
        new_rule = parse_repeat(text)
        new_leads = parse_reminds(text)
        fields: dict[str, Any] = {}

        if new_rule is not None:
            # 连周期一起改：「把组会改成每月5号」「改成每两周」
            fields["repeat"] = new_rule["repeat"]
            for stale in ("weekday", "day", "month", "every_days", "every_minutes"):
                if stale in item and new_rule.get(stale) is None:
                    fields[stale] = None          # 会被后面的清理删掉
            for keep in ("weekday", "day", "month", "every_days", "every_minutes"):
                if new_rule.get(keep) is not None:
                    fields[keep] = new_rule[keep]

        if weekly or fields.get("repeat") in ("weekly", "biweekly"):
            hh, mm = (when.hour, when.minute) if when is not None else self._parse_hhmm(item.get("time", "09:00"))
            wd = day.weekday() if day is not None else int(item.get("weekday", 0) or 0)
            fields.update(weekday=wd, time=f"{hh:02d}:{mm:02d}", skip=[])
        else:
            if when is None and fields.get("repeat") is None:
                return SkillResult(
                    reply=f"想把{title}改到什么时候？比如「挪到明天下午三点」。",
                    action="schedule_edit",
                )
            if when is not None:
                fields["start"] = when.strftime("%Y-%m-%d %H:%M")
                fields["time"] = f"{when.hour:02d}:{when.minute:02d}"
                if fields.get("repeat") in ("monthly", "yearly"):
                    fields.setdefault("day", when.day)
                    if fields["repeat"] == "yearly":
                        fields.setdefault("month", when.month)
        if new_leads is not None:
            fields["remind_before"] = new_leads
        note = self._note_of(text)
        if note:
            fields["note"] = note
        _, loc = _split_title_location(text)
        if loc:
            fields["location"] = loc

        clear = [k for k, v in fields.items() if v is None]
        for k in clear:
            fields.pop(k)
        if clear:
            # 有字段要消失（比如从「每周三」改成「每月5号」），删掉旧的再写新的
            items_now = self.schedule.load()
            for it in items_now:
                if _sched_key(it) == key:
                    for k in clear:
                        it.pop(k, None)
                    it.update(fields)
                    break
            self.schedule.save(items_now)
        else:
            self._update_schedule_item(key, **fields)

        where = f"，地点{loc}" if loc else ""
        cur = next((it for it in self.schedule.load() if it.get("title") == title), item)
        if self._repeat_of(cur) != "once" and (weekly or fields.get("repeat") is not None):
            reply = f"已改：{title} 现在是{self._repeat_text(cur)}{where}。"
        else:
            reply = f"已改：{title} 改到 {cur.get('start', '')}{where}。"
        if new_leads is not None:
            reply += self._remind_text_of(new_leads)
        return SkillResult(reply=reply, action="schedule_edit")

    # ------------------------------------------------------------ 日程字段工具
    @staticmethod
    def _note_of(text: str) -> str:
        """「备注带实验报告」→ 带实验报告（跟着提醒一起念/显示）。"""
        m = re.search(r"备注\s*[:：]?\s*([^，,。;；]+)", text or "")
        return m.group(1).strip() if m else ""

    def _finish_item(self, item: dict, location: str, until: date | None, note: str = "") -> None:
        if location:
            item["location"] = location
        if until is not None:
            item["until"] = until.isoformat()
        if note:
            item["note"] = note

    @staticmethod
    def _where_text(location: str) -> str:
        return f"，地点{location}" if location else ""

    @staticmethod
    def _until_text(until: date | None) -> str:
        return f"（到{until.month}月{until.day}日为止）" if until is not None else ""

    @staticmethod
    def _span_text(item: dict) -> str:
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

    def _repeat_text(self, item: dict) -> str:
        """把重复规则说成人话（用于播报）。"""
        rep = self._repeat_of(item)
        hh, mm = self._parse_hhmm(item.get("time", "09:00"))
        if rep == "weekly":
            return f"每{WEEKDAY_NAMES[int(item.get('weekday', 0))]} {hh:02d}:{mm:02d}"
        if rep == "biweekly":
            return f"每两周{WEEKDAY_NAMES[int(item.get('weekday', 0))]} {hh:02d}:{mm:02d}"
        if rep == "monthly":
            return f"每月{cn_number(int(item.get('day') or 1))}号 {hh:02d}:{mm:02d}"
        if rep == "yearly":
            return (
                f"每年{cn_number(int(item.get('month') or 1))}月"
                f"{cn_number(int(item.get('day') or 1))}日 {hh:02d}:{mm:02d}"
            )
        if rep == "interval":
            tail = "（结束后再排下一次）" if int(item.get("duration_minutes", 0) or 0) else ""
            return f"每{self._span_text(item)}一次{tail}"
        return "一次性"

    def _remind_text_of(self, leads: list[int]) -> str:
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

    # ------------------------------------------------------------ 批量 / 指代
    def _scope_filter(self, text: str, allow_partial: bool = False):
        """「所有课程 / 全部会议 / 所有日程」→ (过滤函数, 说法)。

        ``allow_partial``：只看「有哪些课程」这种**纯查询**说法（没有「所有」）。
        """
        t = text or ""
        if not (self._ALL_WORDS.search(t) or (allow_partial and self._SCOPE_ASK.search(t))):
            return None, ""
        if self._SCOPE_COURSE.search(t):
            return (
                lambda it: str(it.get("kind")) == "course"
                or self._repeat_of(it) in ("weekly", "biweekly"),
                "课程",
            )
        if self._SCOPE_MEETING.search(t):
            return (
                lambda it: str(it.get("kind")) == "meeting" or "会" in str(it.get("title", "")),
                "会议",
            )
        if self._SCOPE_TASK.search(t):
            return lambda it: str(it.get("kind")) == "task", "任务"
        if self._SCOPE_ANY.search(t):
            return lambda it: True, "日程"
        return None, ""

    def _batch_drop(self, targets: list[dict], scope: str, text: str, now: datetime) -> SkillResult:
        """批量取消：一次性的直接删；每周重复的要么「以后都不上」，要么逐条只跳过下一次。"""
        weekly = [it for it in targets if self._repeat_of(it) in ("weekly", "biweekly")]
        once = [it for it in targets if it not in weekly]
        forever = bool(self._FOREVER_WORDS.search(text))

        if weekly and not forever and not once and not self._RANGE_WORD.search(text):
            # 「取消所有课程」到底是「以后都不上」还是「这周不上」——不猜，问一句
            names = "、".join(str(it.get("title", "课")) for it in weekly[:4])
            return SkillResult(
                reply=(
                    f"一共有{cn_quantity(len(weekly))}门课（{names}）。"
                    f"是要以后都不上（彻底删掉），还是只取消这周这一次？"
                    f"说「以后都不上」或者「这周不上」就行。"
                ),
                action="schedule_change",
            )

        if forever or once and not weekly:
            keys = {_sched_key(it) for it in targets}
            removed = self.schedule.remove_where(lambda it: _sched_key(it) in keys)
            names = "、".join(str(it.get("title", "安排")) for it in removed[:6])
            tail = "等" if len(removed) > 6 else ""
            return SkillResult(
                reply=f"已删除{cn_quantity(len(removed))}条{scope}：{names}{tail}。",
                action="schedule_delete",
            )

        # 只跳过下一次
        if not weekly:
            return SkillResult(reply=f"{scope}里没有可以取消的。", action="schedule_change")
        day = parse_date_hint(text, now)
        done: list[str] = []
        for it in weekly:
            target_day = day if day is not None else self._skip_day("", it, now)
            if target_day < now.date():
                target_day = self._skip_day("", it, now)
            skips = [str(d) for d in (it.get("skip") or []) if d]
            if str(target_day) in skips:
                continue
            skips.append(str(target_day))
            self._update_schedule_item(_sched_key(it), skip=skips)
            done.append(str(it.get("title", "课")))
        if not done:
            return SkillResult(reply=f"{scope}这次本来就没安排。", action="schedule_skip")
        return SkillResult(
            reply=f"好，这次的{'、'.join(done)}都不提醒了，下次照常。", action="schedule_skip"
        )

    def _batch_edit(self, targets: list[dict], scope: str, text: str, now: datetime) -> SkillResult:
        """批量改：只改这一句里说得清楚的那几项（时刻 / 星期 / 提前量 / 地点 / 备注 / 周期）。"""
        when = parse_datetime(text, now)
        leads = parse_reminds(text)
        rule = parse_repeat(text)
        wd = _weekly_weekday(text)
        _, loc = _split_title_location(text)
        note = self._note_of(text)
        changed: list[str] = []
        for it in targets:
            fields: dict[str, Any] = {}
            weekly = self._repeat_of(it) in ("weekly", "biweekly")
            if wd is not None and weekly:
                fields["weekday"] = wd
                fields["skip"] = []          # 换了星期，之前跳过的那些天就不算数了
            if when is not None:
                if weekly:
                    fields["time"] = f"{when.hour:02d}:{when.minute:02d}"
                else:
                    # 一次性/间隔的只换时刻，日期各自保留
                    raw = it.get("start") or ""
                    head = str(raw)[:10] if len(str(raw)) >= 10 else now.strftime("%Y-%m-%d")
                    fields["start"] = f"{head} {when.hour:02d}:{when.minute:02d}"
                    fields["time"] = f"{when.hour:02d}:{when.minute:02d}"
            if rule is not None:
                fields["repeat"] = rule["repeat"]
                for key in ("weekday", "day", "month", "every_days", "every_minutes"):
                    if rule.get(key) is not None:
                        fields[key] = rule[key]
            if leads is not None:
                fields["remind_before"] = leads
            if loc:
                fields["location"] = loc
            if note:
                fields["note"] = note
            if fields:
                self._update_schedule_item(_sched_key(it), **fields)
                changed.append(str(it.get("title", "安排")))
        if not changed:
            return SkillResult(
                reply=f"要{scope}都改什么？可以说「都改到下午三点」或者「都提前半小时提醒」。",
                action="schedule_edit",
            )
        names = "、".join(changed[:6])
        return SkillResult(
            reply=f"已改{cn_quantity(len(changed))}条{scope}：{names}{'等' if len(changed) > 6 else ''}。",
            action="schedule_edit",
        )

    def _referenced_items(self, text: str, now: datetime) -> list[dict]:
        """「它 / 那个 / 刚才那条」指谁：去最近的聊天记录里找日程名字。

        技能不猜：记录里有几个候选就返回几个，由调用方决定是执行还是反问。
        """
        hay = "\n".join(self.dialog[-8:])
        if not hay.strip():
            return []
        items = self.schedule.load()
        hits = self._score_items(hay, items, now)
        return [it for s, it in hits if s >= 2]     # 至少要有名字或时间对得上

    def _list_scope(self, text: str, now: datetime) -> SkillResult | None:
        """「有哪些课程 / 所有会议」——把所有定义列出来，而不是只看某一天。"""
        pred, scope = self._scope_filter(text, allow_partial=True)
        if pred is None:
            return None
        # 「每周五有什么课」「9月20日有哪些课」是在问某一天，别按全体列表回答
        if (self._RANGE_WORD.search(text)
                or _weekly_weekday(text) is not None
                or parse_date_hint(text, now) is not None):
            return None
        targets = [it for it in self.schedule.load() if pred(it)]
        if not targets:
            return SkillResult(reply=f"日程里还没有{scope}。", action="schedule_list")
        parts = []
        for it in targets:
            nxt = self.next_occurrence(it, now)
            when = f"下一次{nxt.strftime('%m-%d %H:%M')}" if nxt else "已经过期"
            title = str(it.get("title") or "安排")
            parts.append(f"{title}（{when}）" if self._repeat_of(it) == "once"
                         else f"{self._repeat_text(it)} {title}，{when}")
        return SkillResult(
            reply=f"一共{cn_quantity(len(targets))}条{scope}：" + "；".join(parts) + "。",
            action="schedule_list",
        )

    def _match_schedule_items(self, text: str, items: list[dict], now: datetime) -> list[tuple[int, dict]]:
        """这句话指的是哪几条日程。"""
        return self._score_items(text, items, now)

    def _score_items(self, hay: str, items: list[dict], now: datetime) -> list[tuple[int, dict]]:
        """找出 ``hay`` 里提到了哪几条日程，返回 (得分, 条目) 按得分降序。

        名字最算数（4 分），其次是时间对得上（星期/几号/时刻）。
        ``hay`` 既可以是当前这一句，也可以是最近几轮聊天记录（指代解析）。
        """
        day = parse_date_hint(hay, now)
        clock = parse_clock(hay)
        hits: list[tuple[int, dict]] = []
        for it in items:
            score = 2 * _title_hit(str(it.get("title", "")), hay)
            rep = self._repeat_of(it)
            same_clock = bool(clock) and str(it.get("time", "")) == f"{clock[0]:02d}:{clock[1]:02d}"
            if rep in ("weekly", "biweekly"):
                if day is not None and int(it.get("weekday", -1)) == day.weekday():
                    score += 2
                if same_clock:
                    score += 1
            elif rep == "monthly":
                if day is not None and int(it.get("day") or 0) == day.day:
                    score += 2
                if same_clock:
                    score += 1
            elif rep == "yearly":
                if (
                    day is not None
                    and int(it.get("month") or 0) == day.month
                    and int(it.get("day") or 0) == day.day
                ):
                    score += 2
            elif rep == "interval":
                if same_clock:
                    score += 1
            elif day is not None and str(it.get("start") or "")[:10] == day.isoformat():
                score += 3
            if score:
                hits.append((score, it))
        hits.sort(key=lambda x: x[0], reverse=True)
        return hits

    def _skip_day(self, text: str, item: dict, now: datetime) -> date:
        """算出这次要跳过哪一天：句子里有日期就用它，否则用最近的那一次。"""
        day = parse_date_hint(text, now)
        if day is not None:
            return day
        nxt = self.next_occurrence(item, now - timedelta(seconds=1))
        return nxt.date() if nxt is not None else now.date()

    def _at(self, item: dict, day: date) -> datetime:
        hh, mm = self._parse_hhmm(item.get("time", "09:00"))
        return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)

    def _update_schedule_item(self, key: tuple, **fields: Any) -> None:
        for i, it in enumerate(self.schedule.load(), start=1):
            if _sched_key(it) == key:
                self.schedule.update(i, **fields)
                return

    # ---------------------------------------------------------------- 日程查询
    @staticmethod
    def _day_label(day: date, now: datetime, with_date: bool = False) -> str:
        delta = (day - now.date()).days
        if delta == 0:
            return "今天"
        if delta == 1:
            return "明天"
        if delta == 2:
            return "后天"
        if delta == 3:
            return "大后天"
        if not with_date:
            return WEEKDAY_NAMES[day.weekday()]
        return f"{day.month}月{day.day}日{WEEKDAY_NAMES[day.weekday()]}"

    def _range_of(self, text: str, now: datetime) -> tuple[date, date, str]:
        """解析「问的是哪一段时间」，返回 (起, 止, 说法)，闭区间。

        以前「下周」被算成「下周**一**那一天」，而「这个月 / 下个月 / 未来三天」
        全都落到「今天」——问一段时间却只报一天。这里把所有范围都摊开：
        日 / 周 / 周末 / 月 / 年 / 未来 N 天。
        """
        t = text or ""
        today = now.date()

        def week_of(offset: int) -> tuple[date, date]:
            monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            return monday, monday + timedelta(days=6)

        def month_of(offset: int) -> tuple[date, date]:
            y = today.year + (today.month - 1 + offset) // 12
            m = (today.month - 1 + offset) % 12 + 1
            return date(y, m, 1), date(y, m, _days_in_month(y, m))

        if "大后天" in t:
            d = today + timedelta(days=3)
            return d, d, "大后天"
        if "后天" in t:
            d = today + timedelta(days=2)
            return d, d, "后天"
        if "明天" in t or "明日" in t:
            d = today + timedelta(days=1)
            return d, d, "明天"
        # 周末（周六+周日）
        if re.search(r"(下下个?周末|下下周末)", t):
            s = week_of(2)[0] + timedelta(days=5)
            return s, s + timedelta(days=1), "下下个周末"
        if re.search(r"(下个?周末|下周末)", t):
            s = week_of(1)[0] + timedelta(days=5)
            return s, s + timedelta(days=1), "下周末"
        if re.search(r"(这个?周末|本周末)", t):
            s = week_of(0)[0] + timedelta(days=5)
            if s < today:               # 已经过了就顺延到下一个周末
                s += timedelta(days=7)
            return s, s + timedelta(days=1), "这个周末"
        # 整周（周一起算）
        if re.search(r"(下下个?周|下下星期|下下个星期)", t):
            s, e = week_of(2)
            return s, e, "下下周"
        if re.search(r"(下周|下个星期|下星期|下个礼拜)", t):
            s, e = week_of(1)
            return s, e, "下周"
        if re.search(r"(这周|本周|这个星期|这星期|本星期|这一周|整周|全周)", t):
            s, e = week_of(0)
            return s, e, "这周"
        # 整月
        if re.search(r"(下个月|下月)", t):
            s, e = month_of(1)
            return s, e, "下个月"
        if re.search(r"(这个月|本月|当月)", t):
            s, e = month_of(0)
            return s, e, "这个月"
        # 整年
        if re.search(r"(明年|下一年)", t):
            y = today.year + 1
            return date(y, 1, 1), date(y, 12, 31), "明年"
        if re.search(r"(今年|本年度)", t):
            return date(today.year, 1, 1), date(today.year, 12, 31), "今年"
        # 未来 N 天 / 未来一周 / 未来一个月
        m = re.search(r"(?:未来|接下来|后面|最近)\s*(\d{1,2}|[一二三四五六七八九十两]+)\s*天", t)
        if m:
            n = max(1, int(cn2num(m.group(1)) or 1))
            return today, today + timedelta(days=n - 1), f"未来{cn_number(n)}天"
        if re.search(r"(?:未来|接下来|后面)\s*(?:一)?(?:周|星期|礼拜)", t):
            return today, today + timedelta(days=6), "未来一周"
        if re.search(r"(?:未来|接下来)\s*(?:一)?个?月", t):
            return today, today + timedelta(days=29), "未来一个月"
        if "这两天" in t:
            return today, today + timedelta(days=1), "这两天"
        if re.search(r"(这几天|最近几天)", t):
            return today, today + timedelta(days=3), "这几天"
        if re.search(r"(最近|接下来)", t):
            return today, today + timedelta(days=6), "最近一周"
        return today, today, "今天"

    def _day_items(self, text: str, now: datetime) -> SkillResult | None:
        start, end, label = self._range_of(text, now)
        return self._range_items(start, end, label, now)

    def _range_items(self, start: date, end: date, label: str, now: datetime) -> SkillResult:
        """把一段时间里的安排按天列出来（重复规则由 _occurrences_on 展开）。"""
        # 问「这周 / 这个月」时只报还没过去的：周一说「这周安排」不想再听上周三的课
        if start != end:
            start = max(start, now.date())
        found: list[tuple[date, str]] = []
        total = 0
        day = start
        while day <= end:
            items = self._occurrences_on(day, ignore_reminder=True)
            if items:
                total += len(items)
                detail = "、".join(
                    f"{clock_text(w)}{i.get('title', '安排')}"
                    + (f"（地点{i['location']}）" if i.get("location") else "")
                    for w, i in items
                )
                found.append((day, detail))
            day += timedelta(days=1)

        if not found:
            return SkillResult(reply=f"{label}没有课程或会议安排。", action="schedule_query")
        if start == end:
            return SkillResult(
                reply=f"{label}有{cn_quantity(total)}项安排：" + "；".join(d for _day, d in found) + "。",
                action="schedule_query",
            )
        span = f"{label}（{start.month}月{start.day}日到{end.month}月{end.day}日）"
        # 跨度大了就得把日期写出来，不然「周三」分不清是哪一周
        with_date = (end - start).days > 31
        detail = "；".join(f"{self._day_label(d, now, with_date)}{txt}" for d, txt in found)
        return SkillResult(
            reply=f"{span}有{cn_quantity(total)}项安排：{detail}。",
            action="schedule_query",
        )

    def _next_item(self, now: datetime) -> SkillResult:
        best: tuple[datetime, dict] | None = None
        for offset in range(0, 15):
            day = now.date() + timedelta(days=offset)
            for when, item in self._occurrences_on(day, ignore_reminder=True):
                end = when + timedelta(minutes=int(item.get("duration_minutes", 60) or 60))
                if end <= now:
                    continue
                if best is None or when < best[0]:
                    best = (when, item)
        if best is None:
            return SkillResult(reply="未来两周内没有安排。", action="schedule_next")
        when, item = best
        delta = (when - now).total_seconds()
        where = f"，地点{item['location']}" if item.get("location") else ""
        if delta <= 0:
            return SkillResult(reply=f"你正在进行：{item.get('title', '')}{where}。", action="schedule_next")
        return SkillResult(
            reply=f"下一项是{humanize(when, now)}的{item.get('title', '安排')}{where}，还有{humanize_delta(delta)}。",
            action="schedule_next",
        )
    # ---------------------------------------------------------------- 日程展开
    @staticmethod
    def _repeat_of(item: dict) -> str:
        """把各种写法的 repeat 归一成 once / weekly / biweekly / monthly / yearly / interval。"""
        raw = str(item.get("repeat", "once")).strip().lower()
        if raw in ("once", "weekly", "biweekly", "monthly", "yearly", "interval"):
            return raw
        return {
            "每周": "weekly", "周": "weekly", "星期": "weekly", "每周重复": "weekly",
            "每两周": "biweekly", "双周": "biweekly", "两周一": "biweekly",
            "每月": "monthly", "每个月": "monthly", "月": "monthly",
            "每年": "yearly", "年": "yearly", "每年重复": "yearly",
            "间隔": "interval", "after_end": "interval", "每天": "interval",
            "一次": "once", "一次性": "once", "none": "once", "": "once",
        }.get(raw, "once")

    def _anchor_of(self, item: dict) -> datetime | None:
        raw = item.get("start") or item.get("when")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("/", "-"))
        except ValueError:
            return None

    def _until_of(self, item: dict) -> date | None:
        raw = item.get("until")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("/", "-")).date()
        except ValueError:
            return None

    def _interval_delta(self, item: dict) -> timedelta:
        """间隔循环的周期 = **这次结束** + 间隔（「结束后 N 天再排一次」）。"""
        dur = max(0, int(item.get("duration_minutes", 0) or 0))
        if item.get("every_minutes"):
            gap = int(item["every_minutes"])
        elif item.get("every_hours"):
            gap = int(item["every_hours"]) * 60
        elif item.get("every_days"):
            gap = int(item["every_days"]) * 24 * 60
        else:
            gap = 24 * 60
        return timedelta(minutes=max(1, dur + gap))

    def _leads_of(self, item: dict) -> list[int]:
        """提前提醒的分钟数组（0 = 到点）；兼容旧数据的整数写法。"""
        raw = item.get("remind_before")
        if raw is None:
            raw = self.settings.skills.default_remind_before
        if isinstance(raw, (int, float, str)):
            try:
                return [max(0, int(raw))]
            except (TypeError, ValueError):
                return [10]
        out = []
        for v in raw:
            try:
                out.append(max(0, int(v)))
            except (TypeError, ValueError):
                continue
        return sorted(set(out), reverse=True) or [10]

    def _starts_from(self, item: dict, since: datetime, limit: int = 16) -> list[datetime]:
        """since（含）往后最多 limit 个开始时间；跳过 skip 里的日子、到 until 为止。"""
        rep = self._repeat_of(item)
        skip = {str(d) for d in (item.get("skip") or [])}
        until = self._until_of(item)
        anchor = self._anchor_of(item)
        hh, mm = self._parse_hhmm(item.get("time", "09:00"))
        out: list[datetime] = []

        def usable(cand: datetime) -> bool:
            if until is not None and cand.date() > until:
                return False
            return str(cand.date()) not in skip

        if rep in ("weekly", "biweekly"):
            weekday = int(item.get("weekday", -1))
            if not 0 <= weekday <= 6:
                return []
            period = timedelta(days=7 if rep == "weekly" else 14)
            base = anchor or datetime.combine(
                since.date() + timedelta(days=(weekday - since.weekday()) % 7), datetime.min.time()
            ).replace(hour=hh, minute=mm)
            if base < since:
                steps = (since - base) // period
                base = base + period * max(0, int(steps))
            k = 0
            while len(out) < limit and k <= limit + 6:
                cand = base + period * k
                k += 1
                if until is not None and cand.date() > until:
                    break
                if cand < since or not usable(cand):
                    continue
                out.append(cand)
        elif rep == "monthly":
            day_ = int(item.get("day") or (anchor.day if anchor else since.day))
            first = (anchor or since).replace(day=1, hour=hh, minute=mm)
            if first < since.replace(day=1, hour=hh, minute=mm):
                first = since.replace(day=1, hour=hh, minute=mm)
            k = 0
            while len(out) < limit and k <= limit + 4:
                y = first.year + (first.month - 1 + k) // 12
                mo = (first.month - 1 + k) % 12 + 1
                # 这个月没有这一天就挪到当月最后一天（比如每月 31 号 → 2 月 28/29 号）
                d = min(day_, _days_in_month(y, mo))
                cand = datetime(y, mo, d, hh, mm)
                k += 1
                if cand < since:
                    continue
                if until is not None and cand.date() > until:
                    break
                if not usable(cand):
                    continue
                out.append(cand)
        elif rep == "yearly":
            month = int(item.get("month") or (anchor.month if anchor else since.month))
            day_ = int(item.get("day") or (anchor.day if anchor else since.day))
            year = max((anchor or since).year, since.year)
            k = 0
            while len(out) < limit and k <= limit + 2:
                y = year + k
                d = min(day_, _days_in_month(y, month))   # 2/29 在平年落到 2/28
                cand = datetime(y, month, d, hh, mm)
                k += 1
                if cand < since:
                    continue
                if until is not None and cand.date() > until:
                    break
                if not usable(cand):
                    continue
                out.append(cand)
        else:                                   # once / interval
            base = anchor or since
            if rep != "interval":
                if base >= since and usable(base):
                    out.append(base)
            else:
                period = self._interval_delta(item)
                steps = 0 if base >= since else max(0, int((since - base) // period))
                k = steps
                while len(out) < limit and k <= steps + limit + 6:
                    cand = base + period * k
                    k += 1
                    if cand < since:
                        continue
                    if until is not None and cand.date() > until:
                        break
                    if not usable(cand):
                        continue
                    out.append(cand)
        return out

    def _occurrences_on(self, day: date, ignore_reminder: bool = False) -> list[tuple[datetime, dict]]:
        """返回某天所有日程的 (开始时间, 条目) 列表。"""
        day_start = datetime.combine(day, datetime.min.time())
        now = datetime.now()
        out: list[tuple[datetime, dict]] = []
        for item in self.schedule.load():
            for cand in self._starts_from(item, day_start, limit=4):
                if cand.date() != day:
                    continue
                if not ignore_reminder and cand < now:
                    continue
                out.append((cand, item))
                break
        out.sort(key=lambda x: x[0])
        return out

    def next_occurrence(self, item: dict, now: datetime) -> datetime | None:
        """给调度器用：算出这个日程的下一次开始时间（跳过被单独取消的那几天）。"""
        for cand in self._starts_from(item, now - timedelta(seconds=1)):
            if cand > now:
                return cand
        return None

    @staticmethod
    def _parse_hhmm(value: Any) -> tuple[int, int]:
        try:
            h, m = str(value).split(":")
            return int(h), int(m)
        except Exception:  # noqa: BLE001
            return 9, 0

    # ======================================================================
    # 显示器开关
    # ======================================================================
    _SCREEN_OFF_WORDS = (
        "关屏幕", "关闭屏幕", "关掉屏幕", "关一下屏幕", "把屏幕关", "屏幕关掉", "屏幕关了",
        "关显示器", "关闭显示器", "关掉显示器", "显示器关", "屏幕熄灭", "屏幕黑",
        "息屏", "黑屏", "关显示", "屏幕灭",
    )
    _SCREEN_ON_WORDS = (
        "开屏幕", "打开屏幕", "点亮屏幕", "亮屏", "把屏幕打开", "屏幕亮",
        "开显示器", "打开显示器", "点亮显示器", "显示器亮",
    )

    def _handle_screen(self, text: str, now: datetime) -> SkillResult | None:
        compact = re.sub(r"[\s，。、,.!！?？]", "", text)
        if any(w in compact for w in self._SCREEN_OFF_WORDS):
            if not self.settings.skills.allow_system_commands:
                return SkillResult(reply="系统级操作已被禁用（config.toml 的 allow_system_commands）。", action="screen_off")
            from .system_ops import monitor_off

            ok, info = monitor_off()
            if not ok:
                return SkillResult(reply=info, action="screen_off")
            return SkillResult(
                reply="已关闭显示。系统仍在运行，只是屏幕灭了；敲一下键盘或叫我都能回来。",
                action="screen_off",
            )
        if any(w in compact for w in self._SCREEN_ON_WORDS):
            if not self.settings.skills.allow_system_commands:
                return SkillResult(reply="系统级操作已被禁用（config.toml 的 allow_system_commands）。", action="screen_on")
            from .system_ops import monitor_on

            ok, info = monitor_on()
            return SkillResult(reply="屏幕已点亮。" if ok else info, action="screen_on")
        return None

    # ======================================================================
    # 能力咨询
    # ======================================================================
    _HELP_Q = re.compile(r"(你能做什么|你会什么|有什么功能|帮助|怎么用|能干什么|使用说明|help)")

    def _handle_help(self, text: str, now: datetime) -> SkillResult | None:
        if not self._HELP_Q.search(text):
            return None
        return SkillResult(
            reply=(
                "我在。我能做这些：「现在几点」报时间；"
                "「十分钟后提醒我喝水」「明天早上七点叫我起床」定提醒；"
                "「记一下买牛奶」「我的备忘有哪些」管备忘；"
                "「今天有什么课」「这周有什么安排」「下周有什么安排」「这个月有什么安排」"
                "「下一个会议是什么」查日程；"
                "「每周三上午九点有 AIAA3102」排课、「每两周周三开组会」「每月5号交房租」"
                "「每3天浇一次花」这种重复日程，说「提前一天和半小时提醒我」就能多提醒几次；"
                "「把组会挪到周五上午十点」改日程、「下周三的课不上了」只取消那一次、"
                "「以后不上这门课了」彻底删掉；"
                "「取消它」「把它改到下午三点」也行，指哪个我会从刚才的对话里找，"
                "拿不准就问你；"
                "「有哪些课程」「所有会议」看全部的安排，「取消所有会议」"
                "「所有课程提前半小时提醒」一次改一片；"
                "「看看这是什么」「看看我的屏幕上是什么」「读一下那个报告」我还能看图看文件；"
                "「关屏幕」把显示器关掉（只是关屏，不睡眠）。"
                "其余的，直接问我就好。"
            ),
            action="help",
        )

    # ======================================================================
    # 供调度器使用
    # ======================================================================
    def due_alarms(self, now: datetime) -> list[dict]:
        """返回到点且尚未播报的闹钟，并标记为已播报。"""
        due = []
        for it in self.alarms.load():
            if it.get("fired"):
                continue
            try:
                when = datetime.fromisoformat(it["when"])
            except Exception:  # noqa: BLE001
                continue
            if when <= now:
                due.append(it)
        for it in due:
            it["fired"] = True
            it["fired_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        if due:
            self.alarms.save(self.alarms.load())
        return due

    def due_schedule(self, now: datetime) -> list[tuple[dict, str]]:
        """返回该提醒的日程：(条目, 提醒文案)。

        每条日程可以配多个提前量（``remind_before: [60, 10, 0]``），
        每个「第几次提醒」只播一次，记在 ``_fired`` 里。
        """
        out: list[tuple[dict, str]] = []
        changed = False
        for item in self.schedule.load():
            start = self.next_occurrence(item, now - timedelta(seconds=1))
            if start is None:
                continue
            fired = {str(k) for k in (item.get("_fired") or [])}
            for lead in self._leads_of(item):
                fire_at = start - timedelta(minutes=lead)
                # 提前量大的先响；到点那一次（lead=0）允许「刚过开始时刻」还算数
                late = timedelta(minutes=5) if lead == 0 else timedelta(0)
                if not (fire_at <= now < start + late):
                    continue
                key = f"{start.isoformat()}|{lead}"
                if key in fired:
                    continue
                fired.add(key)
                item["_fired"] = sorted(fired)[-40:]
                changed = True
                out.append((item, self._reminder_text(item, start, now, lead)))
        if changed:
            self.schedule.save(self.schedule.load())
        return out

    def _reminder_text(self, item: dict, start: datetime, now: datetime, lead: int) -> str:
        """拼提醒文案：多个提前量各自说清楚「还有多久」。"""
        where = f"，地点{item['location']}" if item.get("location") else ""
        clock = start.strftime("%H:%M")
        if lead <= 0:
            head = f"现在就是{clock}"
        else:
            minutes = max(0, int((start - now).total_seconds() // 60))
            if minutes <= 1:
                head = f"马上就到{clock}了"
            elif minutes <= 60:
                head = f"{cn_number(minutes)}分钟后，也就是{clock}"
            elif lead >= 1440:
                head = f"{humanize(start, now)}"
            else:
                head = f"{humanize_delta((start - now).total_seconds())}后，也就是{clock}"
        note = f"（{item['note']}）" if item.get("note") else ""
        head = f"{item['remind_text']}，" + head if item.get("remind_text") else head
        return f"提醒你：{head}，有{item.get('title', '安排')}{note}{where}。"

    def stats(self) -> str:
        alarms = [a for a in self.alarms.load() if not a.get("fired")]
        return (
            f"提醒 {len(alarms)} 条 / 备忘 {len(self.memos.load())} 条 / "
            f"日程 {len(self.schedule.load())} 条  ({self.data_dir})"
        )
