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
)
from .settings import Settings
from .store import JsonStore

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


# 新增日程时要把「帮我记录 / 安排 / 有」这类动词去掉，只留事情本身
_SCHEDULE_VERBS = (
    "帮我", "麻烦你", "麻烦", "请", "到时候", "记得", "提醒我", "帮我记", "记录一下", "记录",
    "记一下", "记下来", "记下", "添加", "新增", "创建", "新建", "排入", "安排", "加",
    "每天", "每", "有个", "有", "我要", "我", "你", "的", "去", "上", "在", "是",
)
_LOCATION = re.compile(r"(?:地点|教室)\s*[:：]?\s*([^，,。;；]+)")


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

    # ======================================================================
    # 入口
    # ======================================================================
    def handle(self, text: str) -> SkillResult | None:
        """尝试用技能回答；返回 None 表示应该交给 LLM。"""
        if not self.enabled:
            return None
        raw = (text or "").strip()
        if not raw:
            return None
        now = datetime.now()
        for fn in (
            self._handle_help,
            self._handle_screen,
            self._handle_clock,
            self._handle_alarm,
            self._handle_schedule,
            self._handle_memo,
        ):
            try:
                result = fn(raw, now)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"技能 {fn.__name__} 出错：{exc}")
                continue
            if result:
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
    # 带这些词的是问句，不要当成「取消」（「今天的课不上吗？」不能真去取消）
    _SCHEDULE_QUESTION = re.compile(r"(?:有什么|有哪些|有没有|列表|列出|查一下|查询|看看|看一下)|[吗呢？?]\s*$")
    _SCHEDULE_Q = re.compile(
        r"(?:今天|明天|后天|本周|这周|下周|下个星期|这个星期)?\s*(?:有什么|有哪些|有没有|安排|日程|行程|列表|看看|查一下)?\s*"
        r"(?:课|课程|上课|会议|开会|日程|日成|安排|行程|例会|组会)"
    )
    _SCHEDULE_NEXT = re.compile(r"(下一个|下一节|接下来|最近的?)\s*(?:课|课程|会议|开会|日程|安排|例会|组会)")
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
        # 也没有「课/会议」字眼，所以带「每周」+ 时刻的课表描述要单独放行，
        # 否则会被 _SCHEDULE_Q 之类的查询规则抢走。
        m = self._SCHEDULE_ADD.search(text)
        wd = _weekly_weekday(text)
        is_query = bool(self._SCHEDULE_ASK.search(text))
        looks_add = (m is not None and TRIGGER_SCHEDULE.search(text)) or wd is not None
        if not is_query and looks_add and has_clock_expr(text):
            week_start = parse_datetime(text, now) if wd is not None else None
            title, location = _split_title_location(text)
            if not _is_topic(title):
                title = _clean_content(m.group("content")) if m else ""
            if not _is_topic(title):
                title = _clean_content(text)
            if not _is_topic(title):
                title = "日程"
            lead = int(self.settings.skills.default_remind_before)

            if wd is not None and week_start is not None:
                # 用 parse_datetime 的小时分钟，才能把「下午两点」正确当成 14 点
                hh, mm = week_start.hour, week_start.minute
                item = {
                    "title": title,
                    "kind": "course",
                    "repeat": "weekly",
                    "weekday": wd,
                    "time": f"{hh:02d}:{mm:02d}",
                    "duration_minutes": 90,
                    "remind_before": lead,
                }
                if location:
                    item["location"] = location
                self.schedule.append(item)
                where = f"，地点{location}" if location else ""
                return SkillResult(
                    reply=(
                        f"已排入课表：每{WEEKDAY_NAMES[wd]} {hh:02d}:{mm:02d}，{title}{where}。"
                        f"最近一次是{humanize(week_start, now)}，我会提前{cn_number(lead)}分钟提醒你。"
                    ),
                    action="schedule_add_weekly",
                )

            when = parse_datetime(text, now)
            if when is None:
                return SkillResult(
                    reply="请给我具体时间，比如「每周三上午九点有 AIAA3102」或「明天下午三点安排组会」。",
                    action="schedule_add",
                )
            item = {
                "title": title,
                "kind": "meeting",
                "repeat": "once",
                "start": when.strftime("%Y-%m-%d %H:%M"),
                "remind_before": lead,
            }
            if location:
                item["location"] = location
            self.schedule.append(item)
            where = f"，地点{location}" if location else ""
            return SkillResult(
                reply=f"已排入日程：{humanize(when, now)}，{title}{where}。我会提前{cn_number(lead)}分钟提醒你。",
                action="schedule_add",
            )

        if self._SCHEDULE_NEXT.search(text):
            return self._next_item(now)

        if self._SCHEDULE_Q.search(text):
            return self._day_items(text, now)

        return None

    # ------------------------------------------------------------ 日程改 / 删 / 跳过
    def _handle_schedule_change(self, text: str, now: datetime) -> SkillResult | None:
        """处理「改到…」「取消…那节课」「这周三不上了」。

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
        hits = self._match_schedule_items(text, items, now)
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
        weekly = str(item.get("repeat", "once")).lower() in ("weekly", "每周", "周")

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
        fields: dict[str, Any] = {}
        if weekly:
            when = parse_datetime(text, now)
            hh, mm = (when.hour, when.minute) if when is not None else self._parse_hhmm(item.get("time", "09:00"))
            wd = day.weekday() if day is not None else int(item.get("weekday", 0) or 0)
            fields.update(weekday=wd, time=f"{hh:02d}:{mm:02d}", skip=[])
        else:
            when = parse_datetime(text, now)
            if when is None:
                return SkillResult(
                    reply=f"想把{title}改到什么时候？比如「挪到明天下午三点」。",
                    action="schedule_edit",
                )
            fields["start"] = when.strftime("%Y-%m-%d %H:%M")
        _, loc = _split_title_location(text)
        if loc:
            fields["location"] = loc
        self._update_schedule_item(key, **fields)

        where = f"，地点{loc}" if loc else ""
        if weekly:
            reply = (
                f"已改：{title} 现在是每{WEEKDAY_NAMES[int(fields['weekday'])]} {fields['time']}{where}。"
            )
        else:
            reply = f"已改：{title} 改到 {fields['start']}{where}。"
        return SkillResult(reply=reply, action="schedule_edit")

    def _match_schedule_items(self, text: str, items: list[dict], now: datetime) -> list[tuple[int, dict]]:
        """找出这句话指的是哪几条日程，返回 (得分, 条目) 按得分降序。

        名字最算数（4 分），其次是一周里的星期（2 分），最后是时刻（1 分）。
        """
        day = parse_date_hint(text, now)
        clock = parse_clock(text)
        hits: list[tuple[int, dict]] = []
        for it in items:
            score = 2 * _title_hit(str(it.get("title", "")), text)
            weekly = str(it.get("repeat", "once")).lower() in ("weekly", "每周", "周")
            if weekly:
                if day is not None and int(it.get("weekday", -1)) == day.weekday():
                    score += 2
                if clock and str(it.get("time", "")) == f"{clock[0]:02d}:{clock[1]:02d}":
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
    def _day_items(self, text: str, now: datetime) -> SkillResult | None:
        target = now.date()
        if "后天" in text:
            target = now.date() + timedelta(days=2)
        elif "明天" in text:
            target = now.date() + timedelta(days=1)
        elif "下周" in text or "下个星期" in text:
            target = now.date() + timedelta(days=7 - now.weekday())
        elif re.search(r"(这周|本周|一星期|整周)", text):
            return self._week_items(now)

        items = self._occurrences_on(target, ignore_reminder=True)
        label = "今天" if target == now.date() else "明天" if target == now.date() + timedelta(days=1) else f"{target.month}月{target.day}日"
        if not items:
            return SkillResult(reply=f"{label}没有课程或会议安排。", action="schedule_query")
        parts = []
        for when, item in items:
            where = f"，地点{item['location']}" if item.get("location") else ""
            parts.append(f"{clock_text(when)}，{item.get('title', '安排')}{where}")
        return SkillResult(reply=f"{label}有{cn_quantity(len(items))}项安排：" + "；".join(parts) + "。", action="schedule_query")

    def _week_items(self, now: datetime) -> SkillResult:
        lines = []
        for offset in range(7):
            day = now.date() + timedelta(days=offset)
            items = self._occurrences_on(day, ignore_reminder=True)
            if not items:
                continue
            label = "今天" if offset == 0 else "明天" if offset == 1 else WEEKDAY_NAMES[day.weekday()]
            detail = "、".join(f"{clock_text(w)}{i.get('title', '')}" for w, i in items)
            lines.append(f"{label}{detail}")
        if not lines:
            return SkillResult(reply="这周没有课程或会议。", action="schedule_query")
        return SkillResult(reply="这周安排：" + "；".join(lines) + "。", action="schedule_query")

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
    def _occurrences_on(self, day: date, ignore_reminder: bool = False) -> list[tuple[datetime, dict]]:
        """返回某天所有日程的 (开始时间, 条目) 列表。"""
        out: list[tuple[datetime, dict]] = []
        for item in self.schedule.load():
            if str(day) in [str(d) for d in (item.get("skip") or [])]:
                continue                      # 这一天被单独取消了
            repeat = str(item.get("repeat", "once")).lower()
            if repeat in ("weekly", "每周", "周"):
                if int(item.get("weekday", -1)) != day.weekday():
                    continue
                hh, mm = self._parse_hhmm(item.get("time", "09:00"))
            else:
                raw = item.get("start") or item.get("when") or ""
                try:
                    start = datetime.fromisoformat(str(raw).replace("/", "-"))
                except ValueError:
                    continue
                if start.date() != day:
                    continue
                hh, mm = start.hour, start.minute
            when = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
            if not ignore_reminder and when < datetime.now():
                continue
            out.append((when, item))
        out.sort(key=lambda x: x[0])
        return out

    def next_occurrence(self, item: dict, now: datetime) -> datetime | None:
        """给调度器用：算出这个日程的下一次开始时间（跳过被单独取消的那几天）。"""
        repeat = str(item.get("repeat", "once")).lower()
        skipped = {str(d) for d in (item.get("skip") or [])}
        if repeat in ("weekly", "每周", "周"):
            weekday = int(item.get("weekday", -1))
            if not 0 <= weekday <= 6:
                return None
            hh, mm = self._parse_hhmm(item.get("time", "09:00"))
            delta = (weekday - now.weekday()) % 7
            candidate = datetime.combine(now.date() + timedelta(days=delta), datetime.min.time()).replace(
                hour=hh, minute=mm
            )
            if candidate <= now:
                candidate += timedelta(days=7)
            # 连续几次都可能被跳过（比如连着取消两周）
            for _ in range(8):
                if str(candidate.date()) not in skipped:
                    break
                candidate += timedelta(days=7)
            return candidate
        raw = item.get("start") or item.get("when") or ""
        try:
            start = datetime.fromisoformat(str(raw).replace("/", "-"))
        except ValueError:
            return None
        return start if start > now else None

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
                "「今天有什么课」「下一个会议是什么」查日程；"
                "「每周三上午九点有 AIAA3102」排课、"
                "「把组会挪到周五上午十点」改日程、"
                "「下周三的课不上了」只取消那一次、"
                "「以后不上这门课了」彻底删掉；"
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
        """返回该提醒的日程：(条目, 提醒文案)。每条日程每天只提醒一次。"""
        out: list[tuple[dict, str]] = []
        today = now.strftime("%Y-%m-%d")
        changed = False
        for item in self.schedule.load():
            start = self.next_occurrence(item, now - timedelta(seconds=1))
            if start is None:
                continue
            lead = int(item.get("remind_before", self.settings.skills.default_remind_before) or 0)
            fire_at = start - timedelta(minutes=lead)
            key = f"{start.isoformat()}"
            if fire_at <= now < start and item.get("_reminded_for") != key:
                item["_reminded_for"] = key
                changed = True
                where = f"，地点{item['location']}" if item.get("location") else ""
                minutes = int((start - now).total_seconds() // 60)
                head = f"{cn_number(minutes)}分钟后" if 0 < minutes <= 60 else humanize(start, now)
                clock = start.strftime("%H:%M")
                out.append(
                    (
                        item,
                        f"提醒你：{head}，也就是{clock}，有{item.get('title', '安排')}{where}。",
                    )
                )
        if changed:
            self.schedule.save(self.schedule.load())
        return out

    def stats(self) -> str:
        alarms = [a for a in self.alarms.load() if not a.get("fired")]
        return (
            f"提醒 {len(alarms)} 条 / 备忘 {len(self.memos.load())} 条 / "
            f"日程 {len(self.schedule.load())} 条  ({self.data_dir})"
        )
