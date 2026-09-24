"""生活实用技能：时间问答、提醒/日程（统一为「事件」）、备忘、看图。

设计原则
    1. **本地规则优先**：这类指令（几点了 / 十分钟后提醒我 / 记一下…）用规则识别，
       零延迟、零幻觉，比丢给大模型可靠得多。
    2. **数据放在 JSON 里**：`data/*.json` 可以直接用编辑器改，
       程序只在必要时写回，外部修改会自动重新加载。
    3. **没命中就返回 None**，交给 LLM 回答，技能不会抢话。

★这里只管「意图路由」与「播报时机」★
    怎么把一句话变成事件字段 → `voice_loop/event_text.py`
    事件怎么存、什么时候该响  → `voice_loop/events.py`
    什么时候算「几点」        → `voice_loop/nlp_time.py`
不再区分「闹钟」与「日程」：两者是同一种事件的不同可选字段（见 events.py 的模块说明）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import event_text as et
from .events import (
    EventStore,
    _days_in_month,
    anchor_of,
    mark_skipped,
    skipped_of,
    display_title,
    has_repeat,
    is_fired,
    leads_of,
    needs_confirm,
    parse_hhmm,
    repeat_of,
    repeat_text,
    title_of,
    until_of,
)
from .nlp_time import (
    _apply_period,
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
    parse_weekday,
)
from .settings import Settings
from .store import JsonStore
from .vision import Vision, VisionError, norm_name

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_FULL = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}
# 「周五」这种没写「每」的星期（用来判断说的时间是不是已经过去了）
_WEEKDAY_WORD = re.compile(r"(?:周|星期|礼拜)[一二三四五六日天末]")



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
# 提示词与路由用的触发词
# ★文本清洗（时间词、填充词、标题归一化）已经全在 voice_loop/event_text.py★
# --------------------------------------------------------------------------- #
CREATE_ALARM_HINTS = ("提醒", "闹钟", "叫醒", "叫我", "喊我", "定时", "叫一下")





CREATE_ALARM_HINTS = ("提醒", "闹钟", "叫醒", "叫我", "喊我", "定时", "叫一下")
MEMO_HINTS = ("记一下", "记下", "记住", "记下来", "帮我记", "备忘", "记录一下", "提醒我记")
SCHEDULE_WORDS = ("课", "课程", "上课", "会议", "开会", "日程", "安排", "行程", "例会", "组会")

# 语音识别经常把关键词听错一个字，直接放宽字符集比写纠错表更好维护
#   「记一下」常被听成「记以下 / 记一哈 / 记一吓」
TRIGGER_ALARM = re.compile(r"(提醒|提行|提星|醒目|闹钟|闹中|叫醒|叫我|喊我|定时|喊一下)")
TRIGGER_MEMO = re.compile(r"(记\s*[一以衣]?\s*[下住录哈吓夏]|记住|^记得|记录|备忘|备忘记)")
TRIGGER_SCHEDULE = re.compile(r"(课|上课|会议|开会|例会|组会|日程|日成|行程|安排)")
# ★明确日程名词★：只有这些才把「取消…」让给日程分支。
# 「安排」不在这里 —— 它同时是用户说闹钟时的常用词。
# 实测「取消明天早上的安排」就是因为句中有「安排」被整个绕过闹钟分支，
# 转去删掉了四天后的「跟导师见面」（不可逆），而 08:00 那条闹钟还在。
_SCHEDULE_NOUN = re.compile(
    r"(?:课程|课|上课|会议|开会|例会|组会|日程|行程|讲座|答辩|面试|考试|出差|行程)"
)
# 约会类：说「下周三下午三点半跟导师见面」时，句子里既没有「课/会议」，
# 也没有「安排/记录」这种动词，以前会直接掉给大模型 ——
# 大模型会回一句「已记录此安排」，实际上什么都没存（用户就是这么被坑的）。
# 只要带明确时刻，这类说法也算新增日程。
TRIGGER_APPOINT = re.compile(
    r"(见面|碰面|碰头|约见|面谈|面试|答辩|汇报|演讲|聚餐|吃饭|喝咖啡|茶话会|"
    r"拜访|走访|体检|复诊|看病|出差|讲座|研讨会|年会|沙龙|分享会|宣讲会|"
    r"运动会|开学典礼|考试|测验|团建|接待|值班|报到|注册)"
)
# 「我周三下午三点半要去见导师」「明天下午三点交材料」：带这种「去干什么」的动词，
# 而且句子里有日期 + 时刻，就当成一条安排（只加了动词不行，还得有明确时间）
PLAN_VERB = re.compile(
    r"(?:要去|得去|去见|去找|去拿|去交|去办|去开|去听|去参加|"
    r"去见|见|找|约|参加|出席|值班|接|送|交|提交|办理|面签)"
)

# ★活动类★：句子里既没有「安排/记录」动词，也没有「课/会议/见面」这种词，
# 但「有…活动/比赛/演出/社团活动」说到底也是一条日程。
# 实测踩到（2026-09-20）：用户连说四遍
# 「9月23号下午3点到4点半，有一场造物社的活动」「下周三下午3点社团活动」
# 一条都没排上，只建了个闹钟——因为技能层根本不认这类说法。
ACTIVITY_NOUN = re.compile(
    r"(活动|比赛|大赛|赛事|演出|表演|汇报演出|讲座|培训|排练|聚会|团建|展会|运动会"
    r"|志愿|义工|社团|学生会|班会|例会|升旗|实验|实训|军训|婚礼|生日会|宣讲|分享会"
    r"|联谊|见面会|茶话会|路演|答辩会)"
)

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


# 判断句子里是否真的包含一个「时刻」表达。
# 不能只依赖 parse_datetime：它把「明天」这种光有日期的说法默认成 09:00，
# 于是「提醒我明天有什么课」会被误当成定时提醒。


# 只有时段没有钟点：「明天中午和导师吃饭」「晚上开会」
_DAY_PERIOD = re.compile(
    r"(半夜|凌晨|清晨|早晨|早上|上午|中午|正午|下午|傍晚|晚上|夜里|今早|明早|今晚|明晚)"
)
_PERIOD_HOUR = {
    "半夜": 0, "凌晨": 5, "清晨": 7, "早晨": 8, "早上": 8, "今早": 8, "明早": 8,
    "上午": 10, "中午": 12, "正午": 12, "下午": 15, "傍晚": 18,
    "晚上": 20, "今晚": 20, "明晚": 20, "夜里": 21,
}
# 只说了时段（「明天早上」）时用来收窄候选：(起始小时, 结束小时)，左闭右开
_PERIOD_RANGE = {
    "半夜": (0, 5), "凌晨": (0, 6), "清晨": (4, 9), "早晨": (4, 11), "早上": (4, 11),
    "今早": (4, 11), "明早": (4, 11), "上午": (7, 12), "中午": (11, 14),
    "正午": (11, 14), "下午": (12, 18), "傍晚": (16, 20), "晚上": (17, 24),
    "今晚": (17, 24), "明晚": (17, 24), "夜里": (20, 24),
}


def _clock_with_period(text: str, now: datetime) -> tuple[int, int] | None:
    """句中的钟点，并且把上午/下午/晚上算进去（「下午三点半」→ (15, 30)）。

    光用 parse_clock 拿到的是 (3, 30)，跟 15:30 比就对不上：
    删除守卫曾因此把「9月23日下午三点半的跟导师见面不去了」误拦成「时间对不上」。
    """
    clock = parse_clock(text)
    if clock is None:
        return None
    dt = parse_datetime(text, now)
    return (dt.hour, dt.minute) if dt is not None else clock


def _period_hour(text: str) -> int | None:
    """「明天中午吃饭」这种只有时段的说法，给一个合理的钟点（中午->12 点）。"""
    m = _DAY_PERIOD.search(text or "")
    return _PERIOD_HOUR.get(m.group(1)) if m else None


def _key_of(item: dict) -> Any:
    """改/删时用它把条目定位回去：**用 id**。

    旧代码用的是「标题 + 周期 + 星期 + 钟点」四元组，两个同名的例子（比如两条
    「喝水」）就会一起被改掉；id 是唯一的，也不会被重载缓存搞错。
    """
    return item.get("id")


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

        # ★一个文件装全部事件★（提醒 + 日程 + 事件链），不再分两个文件两套字段
        self.store = EventStore(
            settings.resolve(cfg.event_file),
            default_lead=int(cfg.default_remind_before),
        )
        self.memos = JsonStore(settings.resolve(cfg.memo_file), default=[])
        for store in (self.store, self.memos):
            store.ensure()

        # 最近的对话（你说 + 助手答），用来解析「它 / 那个 / 刚才那条」指谁。
        # 由 pipeline 每轮传进来；单独跑 skills 时就是空的。
        self.dialog: list[str] = []
        # 刚记下的那一条（事件），用来处理「我说今天晚上8点45」这种**随即修正**。
        # 实测：用户说完一句时间说错了，下一句「我说是今晚8点45」以前会被当成新请求
        # 交给大模型，而大模型只会嘴上说「已更正」——**其实什么也没改**。
        self._last_add: dict | None = None
        # 等着用户确认的那次删改（带重复的事件才需要确认）
        self._pending_change: dict | None = None

        # 看图（摄像头 / 屏幕 / 剪贴板 / 文件）
        vcfg = settings.vision
        self.vision_cfg = vcfg
        self.vision = Vision(vcfg, settings.root, self.log) if vcfg.enabled else None
        self._vision_pending: dict | None = None   # 「是这个文件吗？」等着回答
        self._vision_shot: dict | None = None      # 最近一次「看了什么」

    # ======================================================================
    # 入口
    # ======================================================================
    def handle(
        self, text: str, dialog: list[str] | None = None, now: datetime | None = None
    ) -> SkillResult | None:
        """尝试用技能回答；返回 None 表示应该交给 LLM。

        ``dialog``：最近几轮「你说 / 助手答」的原文，指代解析（它、那个、刚才那条）
        在上面找候选——技能本身不猜，找不到就问。
        ``now``：默认取当前时间；测试里传固定时间就能写死期望值（不然跨零点会飘）。
        """
        if dialog is not None:
            self.dialog = [str(x) for x in dialog if str(x).strip()][-10:]
        if not self.enabled:
            return None
        raw = (text or "").strip()
        if not raw:
            return None
        now = now or datetime.now()
        for fn in (
            self._handle_vision_reply,   # 先接「是 / 第一个」这种确认，别被别的技能抢走
            self._handle_event_confirm,  # 「确定」——上一轮问过「要取消吗」
            self._handle_help,
            self._handle_screen,
            self._handle_clock,
            self._handle_vision,         # 看图（带时间词的会让给事件）
            self._handle_correction,     # 「我说是今晚8点45」——改刚才那一条，别新建
            self._handle_event,          # 提醒/日程统一入口（没有两个处理器了）
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
    # 事件（提醒 / 日程统一入口）
    # ======================================================================
    # ★没有「闹钟分支」和「日程分支」了★
    # 旧代码有两个处理器，中间靠三条让位规则互相踢球（说了重复 / 说了多个提前量 /
    # 说了日期+钟点还带事件名词 → 从闹钟踢给日程），于是「每个工作日八点半叫我起床」
    # 被踢到只认「每周X / 每N天」的日程那边，最后只能存成一条不会重复的闹钟。
    # 现在只有一个入口，顺序固定：**按时间的取消 → 改/删/跳过 → 列出 → 新增 → 查询**。
    # 顺序不能乱：改/删要先判，否则「取消周三的组会」会掉进新增分支。
    _LIST_Q = re.compile(
        r"(有哪些|有什么|还有|看看|查一下|列出|列表|几个|多少)?\s*"
        r"(提醒|闹钟|定时|定时器|事件)\s*"
        r"(有哪些|有什么|列表|是什么|吗|呢)*\s*[?？]?$"
    )
    _EVENT_CANCEL = re.compile(
        r"(?:取消|删除|去掉|关掉|清除|清空|不要|别再)\s*"
        r"(?:第\s*(?P<idx>\d+|[一二三四五六七八九十]+)\s*[个条])?\s*"
        r"(?P<all>所有|全部|这些|所有的)?\s*"
        r"(?:提醒|闹钟|定时|事件)"
    )
    _CANCEL_WORD = re.compile(r"取消|删掉|删除|去掉|不要了|别提醒|不用提醒|关掉|清掉")

    def _handle_event(self, text: str, now: datetime) -> SkillResult | None:
        """提醒/日程的唯一入口。"""
        by_index = self._cancel_by_index(text)
        if by_index is not None:
            return by_index
        by_time = self._cancel_by_time(text, now)
        if by_time is not None:
            return by_time
        changed = self._handle_schedule_change(text, now)
        if changed is not None:
            return changed
        # ★说了「取消…提醒/闹钟」却没对上任何一条：绝不能往下掉进「新增」★
        # 实测踩到：说「取消今晚十点的闹钟」时库里没有那条，旧代码一路走到新建，
        # 结果多出一条 what=「取消」的闹钟——用户以为删了，反而多了一条。
        if self._CANCEL_WORD.search(text) and TRIGGER_ALARM.search(text):
            return SkillResult(
                reply="我没找到要取消的那条——说个时间或者第几个，比如「取消明天早上的闹钟」。",
                action="event_cancel_miss",
            )
        for fn in (self._event_list, self._event_add, self._handle_schedule_query):
            got = fn(text, now)
            if got is not None:
                return got
        return None

    def _cancel_by_index(self, text: str) -> SkillResult | None:
        """「取消第2个」「清空所有提醒」——按序号 / 全部。"""
        m = self._EVENT_CANCEL.search(text)
        if not m:
            return None
        idx_raw, all_raw = m.group("idx"), m.group("all")
        items = self.store.load()
        if all_raw:
            n = self.store.clear()
            return SkillResult(reply=f"已清空全部 {n} 条。", action="event_clear")
        idx = cn2num(idx_raw) if idx_raw else None
        if not idx:
            return None          # 「取消提醒」但没说是哪一条 → 交给后面的分支
        if not (1 <= int(idx) <= len(items)):
            return SkillResult(reply=f"没有第{cn_number(int(idx))}条。", action="event_cancel")
        removed = self.store.remove_at(int(idx))
        if removed is None:
            return None
        return SkillResult(
            reply=f"已撤销第{cn_number(int(idx))}条：{display_title(removed)}。",
            action="event_cancel",
        )

    def _cancel_by_time(self, text: str, now: datetime) -> SkillResult | None:
        """按句中的时间取消一条事件（不要求说序号）。

        ★为什么不能拿 TRIGGER_SCHEDULE 当闸门★：「安排」这个词既是日程词，
        也是用户平时说提醒的说法。实测用户说「取消明天早上的安排」时，
        就因为句子里有「安排」被整个绕过，转去删掉了四天后的「跟导师见面」（不可逆），
        而那条 08:00 的提醒还在。现在只认「课/会议/组会…」这类**明确名词**，
        且句子里没提提醒/闹钟。
        """
        if not self._CANCEL_WORD.search(text):
            return None
        if _SCHEDULE_NOUN.search(text) and not TRIGGER_ALARM.search(text):
            return None                      # 「取消明天早上的课」→ 交给按名字那条路
        day = parse_date_hint(text, now)
        clock = _clock_with_period(text, now)
        if day is None and clock is None:
            return None
        hits: list[tuple[dict, datetime]] = []
        for item in self.store.load():
            when = self._next_of(item, now)
            if when is None:
                continue
            if day is not None and when.date() != day:
                continue
            if clock is not None and (when.hour, when.minute) != clock:
                continue
            hits.append((item, when))
        if len(hits) > 1:
            # 只说了时段（「明天早上」）而当天有多条时，用时段再收一次
            period = _DAY_PERIOD.search(text)
            window = _PERIOD_RANGE.get(period.group(1)) if period else None
            if window:
                narrowed = [h for h in hits if window[0] <= h[1].hour < window[1]]
                if narrowed:
                    hits = narrowed
        if not hits:
            return None
        if len(hits) > 1:
            names = "、".join(f"{humanize(when, now)}{display_title(it)}" for it, when in hits[:4])
            return SkillResult(
                reply=f"{cn_quantity(len(hits))}条都对得上（{names}），你说是哪一条？",
                action="event_cancel_unsure",
            )
        item, when = hits[0]
        if not self._confirm_repeat(item, when, "取消"):
            return self._ask_confirm(item, when, "取消")
        self.store.remove_where(lambda it: it.get("id") == item.get("id"))
        return SkillResult(
            reply=f"已取消{humanize(when, now)}的{display_title(item)}。",
            action="event_cancel",
        )

    def _event_list(self, text: str, now: datetime) -> SkillResult | None:
        """「有哪些提醒 / 我的闹钟」——一个列表，不再分两个。"""
        has_hint = bool(TRIGGER_ALARM.search(text))
        is_list_q = bool(self._LIST_Q.search(text)) or (
            has_hint
            and not TRIGGER_SCHEDULE.search(text)
            and bool(re.search(r"(提醒|闹钟|定时)\s*我?\s*(有哪些|有什么|还有|看看|列出|几个|多少)", text))
        )
        if not is_list_q or has_clock_expr(text):
            return None                      # 「八点半定个闹钟」是新增，不是查询
        upcoming: list[tuple[datetime, dict]] = []
        for item in self.store.load():
            when = self._next_of(item, now)
            if when is None and not has_repeat(item):
                # 一次性、时间已经过了、但一次都还没响过 → 还是待办（补响前不能被藏起来）
                start = anchor_of(item)
                if start is not None and not any(
                    is_fired(item, start, lead) for lead in leads_of(item)
                ):
                    when = start
            if when is not None:
                upcoming.append((when, item))
        upcoming.sort(key=lambda p: p[0])
        if not upcoming:
            return SkillResult(reply="当前没有待办的事件。", action="event_list")
        lines = [
            f"第{cn_number(i)}条，{humanize(when, now)}，{display_title(it)}"
            for i, (when, it) in enumerate(upcoming[:5], 1)
        ]
        tail = "" if len(upcoming) <= 5 else f"，还有{len(upcoming) - 5}条"
        return SkillResult(
            reply=f"待办事件{cn_quantity(len(upcoming))}条。" + "；".join(lines) + tail + "。",
            action="event_list",
        )

    def _next_of(self, item: dict, since: datetime) -> datetime | None:
        """这条从 ``since`` 起（含）下一次什么时候发生。"""
        from .events import occurrences

        got = occurrences(item, since, limit=1)
        return got[0] if got else None

    def _ask_confirm(self, item: dict, when: datetime, verb: str) -> SkillResult:
        """带重复的事件：删/改之前先问一句（影响的是往后所有次）。"""
        self._pending_change = {"id": item.get("id"), "verb": verb}
        return SkillResult(
            reply=(
                f"「{display_title(item)}」是{repeat_text(item)}的，"
                f"以后每次都会跟着{verb}。确定要{verb}吗？说「确定」我就动手。"
            ),
            action="event_confirm",
        )

    def _confirm_repeat(self, item: dict, when: datetime, verb: str) -> bool:
        """带重复的事件需要一句确认；一次性的直接做。"""
        if not needs_confirm(item):
            return True
        pending = self._pending_change
        return bool(
            pending
            and pending.get("id") == item.get("id")
            and pending.get("verb") == verb
        )

    def _handle_event_confirm(self, text: str, now: datetime) -> SkillResult | None:
        """接「确定 / 好 / 对」+ 上一轮问过的那个确认。"""
        pending = self._pending_change
        if not pending:
            return None
        if not re.search(r"^(确定|确认|好的?|嗯|对|是|可以|继续|就这么)|确定|确认", text or ""):
            return None
        self._pending_change = None
        item = self.store.by_id(pending.get("id"))
        if item is None:
            return SkillResult(reply="那条已经不在了。", action="event_confirm")
        if str(pending.get("verb") or "") in ("取消", "删除"):
            self.store.remove_where(lambda it: it.get("id") == item.get("id"))
            return SkillResult(
                reply=f"好，{display_title(item)}已经删除，以后不会再提醒了。",
                action="event_delete",
            )
        return SkillResult(reply="好，那就按刚才说的改。", action="event_confirm")

    def _looks_like_add(self, text: str, now: datetime) -> bool:
        """这句话像不像「新增一条」。

        ★这个判据必须「新增」和「改/删」共用★：以前改/删那侧只看
        `_SCHEDULE_ADD / ACTIVITY_NOUN / TRIGGER_APPOINT`，于是
        「每周三九点提醒我上课，提前半小时」（里面有「提前」会撞 `_SCHEDULE_EDIT`）
        被回一句「日程里现在还是空的，没什么可以改的」——明明是要新增。
        """
        m = self._SCHEDULE_ADD.search(text)
        rule = parse_repeat(text)
        if m is not None and TRIGGER_SCHEDULE.search(text):
            return True
        if rule is not None:
            return True
        if bool(TRIGGER_APPOINT.search(text)) and (
            has_clock_expr(text) or _DAY_PERIOD.search(text) is not None
        ):
            return True
        if bool(PLAN_VERB.search(text)) and has_clock_expr(text) and parse_date_hint(text, now):
            return True
        if bool(ACTIVITY_NOUN.search(text)) and has_clock_expr(text) and parse_date_hint(text, now):
            return True
        # 带「提醒/闹钟」+ 明确时刻的也算（以前这种会掉到闹钟处理器，现在同一条路）
        return bool(TRIGGER_ALARM.search(text)) and has_clock_expr(text)

    def _event_add(self, text: str, now: datetime) -> SkillResult | None:
        """新增一条事件：**说什么就填什么**（时长/重复/地点/提前量/截止）。

        旧代码在这里分了两条路（「每周三…」手拼一个课表条目，其余手拼一个日程条目），
        两边各自猜默认值。现在只有 :func:`event_text.extract` 一条路：
        没说时长就是 0、没说提前量就是准时、没说重复就是一次。
        """
        if self._SCHEDULE_ASK.search(text) or not self._looks_like_add(text, now):
            return None                      # 问句不是新增；不像是新增就交给别人

        fields = et.extract(text, now)
        start: datetime = fields["start"]
        rolled = ""
        if start < now - timedelta(seconds=60):
            wd_hint = parse_weekday(text)
            if has_repeat(fields):
                # 重复的：引擎会自己往后滚（时间字段里的钟点才是基准）
                rolled = "这周那个时间已经过了。"
            elif wd_hint is not None:
                # 说了星期几：这周那个点已经过了 → 指的一定是下一个
                days = (wd_hint - now.weekday()) % 7 or 7
                start = datetime.combine(now.date() + timedelta(days=days), start.time())
                fields["start"] = start
                rolled = "这周那个时间已经过了，我按下一个算。"
            else:
                start += timedelta(days=1)
                fields["start"] = start
                rolled = "那个时间今天已经过了，我按明天算。"
        if has_repeat(fields):
            fields["time"] = f"{start.hour:02d}:{start.minute:02d}"
        item = et.to_item(fields)
        if not title_of(item) and not has_clock_expr(text):
            return None                      # 连时间都没说清，别硬存
        dup = self._find_duplicate(item, now, start)
        if dup is not None:
            return SkillResult(
                reply=(
                    f"这条已经有了：{self._when_of(dup, now)}，{display_title(dup)}。"
                    f"要改就说「把{display_title(dup)}挪到…」。"
                ),
                action="event_exists",
            )
        item = self.store.append(item)
        self._remember_add(item.get("id"))
        return SkillResult(
            reply=et.render_added(item, start, now) + (" " + rolled if rolled else ""),
            action="event_add",
            data=item,
        )

    def _when_of(self, item: dict, now: datetime) -> str:
        """说清楚一条事件「什么时候」：重复的说周期+钟点，一次性的说日期时刻。"""
        text = et.when_text(item)
        if text:
            return text
        anchor = anchor_of(item)
        return humanize(anchor, now) if anchor else "时间未定"

    def _handle_schedule_query(self, text: str, now: datetime) -> SkillResult | None:
        """「下一项是什么」「这周有什么安排」「周三有什么课」。"""
        if self._SCHEDULE_NEXT.search(text):
            return self._next_item(now)
        listed = self._list_scope(text, now)
        if listed is not None:
            return listed
        if self._SCHEDULE_Q.search(text) or self._RANGE_ASK.search(text):
            if not self._NOT_SCHEDULE_Q.search(text):
                return self._day_items(text, now)
            return None          # 「今天天气怎么样」交给模型去答
        return None

    # ======================================================================
    # 随即修正：「我说是今晚8点45」「不对，是明天上午」——改刚才那一条
    # ======================================================================
    _CORRECT = re.compile(
        r"我说|我是说|说的是|说错了|改了|不对|不是|应该是|指的是|搞错|错了|改成|换到|挪到"
    )
    # 允许出现在「纯时间」一句话里的字。除此之外任何一个字（提醒/开会/交房租/课…）
    # 都会让它失去资格——这样「每月5号下午三点交房租」就不会把上一条改掉了
    # （曾经用「长度 <= 14 且带钟点」当条件，结果这类短句被误当成修正在改）。
    _TIME_ONLY_CHARS = set(
        "0123456789"                    # 数字
        "零一二三四五六七八九十百两"      # 汉字数字
        "点时時刻分秒号日天"              # 时间单位
        "早上下晚中夜晨凌半午"            # 时段
        "今明后大周星期礼拜末这那个"      # 日期/星期
        "的我是了不改成到应该对说"          # 修正措辞（「我说」「改成」「不对」）
        "吧啊呢呀就才差过整"              # 语气/零碎
        ":："                            # 「8:45」这种写法
    )

    @classmethod
    def _looks_time_only(cls, text: str, now: datetime) -> bool:
        """整句只在说时间（可带「我说/不对/改成」这类词），没有别的内容。

        这是「随即修正」的准入条件：修正只该给出一个新时间，不该顺便带来别的事。
        """
        t = re.sub(r"[\s，。、,.!！?？~]", "", text or "")
        if not (2 <= len(t) <= 20):
            return False
        if any(ch not in cls._TIME_ONLY_CHARS for ch in t):
            return False
        if has_clock_expr(t):
            return True
        # 只说日期不说钟点的（「不是今天，是后天」），必须有修正措辞才认
        return bool(cls._CORRECT.search(t)) and parse_date_hint(t, now) is not None

    def _handle_correction(self, text: str, now: datetime) -> SkillResult | None:
        """把「刚说的那条」改成新时间，而不是新建一条。

        为什么需要它：实测用户说完「8点45提醒我练琴」发现被理解成明早八点，
        紧接着说「我说今天晚上8点45」——这句话以前落到大模型手里，
        大模型只会回一句「抱歉，刚才的时间换算有误，今晚八点四十五分……」，
        **而库里什么也没改**（那个闹钟仍然是明天 08:00）。不可逆的错就这么留下了。

        触发条件（两个都满足才动手，宁少改不多改）：
            1. 整句只在说时间（见 _looks_time_only）——带别的内容就照常走新增/查询；
            2. 上一轮确实刚记下一条（默认 3 分钟内）。
        """
        if self._last_add is None:
            return None
        age = time.monotonic() - float(self._last_add.get("at") or 0.0)
        if age > 180:
            return None
        if not self._looks_time_only(text, now):
            return None

        when = parse_datetime(text, now)
        if when is None:
            return None
        if when <= now:
            when = when + timedelta(days=1)

        item = self.store.by_id(self._last_add.get("id"))
        if item is None:
            return None
        eid = item.get("id")
        title = title_of(item) or "闹钟"
        if has_repeat(item):
            # 重复事件只改「那一个钟点」——课表还在
            fields: dict[str, Any] = {"time": when.strftime("%H:%M")}
        else:
            fields = {"start": when.strftime("%Y-%m-%d %H:%M:%S"), "state": None}
        self._update_event(eid, **fields)
        self._last_add = {"id": eid, "at": time.monotonic()}
        return SkillResult(
            reply=f"改好了：{title}改到{humanize(when, now)}。",
            action="event_reschedule",
            data={"start": when.isoformat(), "title": title},
        )

    def _remember_add(self, event_id: Any) -> None:
        """记下「刚刚新增的那一条」，供随即修正使用。"""
        self._last_add = {"id": event_id, "at": time.monotonic()}

    # ======================================================================
    # 「这句话要不要先让确定性层看一眼」
    # ======================================================================
    # routes = model（默认）时，模型先选工具；它在某些说话方式上漏调工具时，
    # 用这些词判断「这一句看起来要动手」——是的话就把模型的回答先攒住不念，
    # 问过技能层再决定说哪句（见 pipeline.respond）。所以它是**兜底的触发条件**，
    # 不是路由本身：多包含几个词只会多等一会儿，不会答错。
    _ROUTE_WORDS = re.compile(
        r"日程|安排|课程|课|会议|开会|例会|组会|行程|讲座|答辩|面试|体检"
        r"|提醒|闹钟|叫我|喊我|备忘|记一下|记住|记下"
        r"|几点|几号|星期几|周几|报时|什么日子"
        r"|看看|看一下|瞧一眼|拍一张|拍个照|摄像头|屏幕上|屏幕里|剪贴板|桌面上的|文件"
    )

    def recent_add(self, seconds: float = 180.0) -> bool:
        """刚记下一条吗？（「随即修正」类判断用）"""
        if self._last_add is None:
            return False
        return time.monotonic() - float(self._last_add.get("at") or 0.0) <= seconds

    def needs_attention(self, text: str) -> bool:
        """这句话要不要「攒着等模型决定完、再让技能层兜一下」。

        三种情况要：
            1. 有等着你回答的事（「是这个文件吗？」）；
            2. 刚记下一条（3 分钟内）——下一句很可能是「我说是今晚8点45」这种即时纠正；
            3. 句子里有 :data:`_ROUTE_WORDS` 里的词。
        """
        if self._vision_pending:
            return True
        if self.recent_add():
            return True
        return bool(self._ROUTE_WORDS.search(text or ""))

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
            raw = m.group("content")
            content = et.clean_content(raw)              # 顺带存闹钟时当 what 用
            memo_text = et.clean_memo_content(raw)       # 备忘本身要留住时间词
            if not memo_text and not content:
                return None
            self.memos.append({"content": memo_text or content, "done": False})
            # 「记一下明天要买牛奶」这类带时间的，顺带提醒一下——存成一条普通事件
            when = parse_datetime(text, now)
            if when is not None and TRIGGER_ALARM.search(text):
                fields = et.extract(content or memo_text, now)
                fields["title"] = content or memo_text
                fields["start"] = when
                fields["repeat"] = "once"
                item = self.store.append(et.to_item(fields))
                self._remember_add(item.get("id"))
                return SkillResult(
                    reply=f"已归档：{memo_text or content}。{humanize(when, now)}我会提醒你。",
                    action="memo_add_event",
                )
            return SkillResult(reply=f"已归档：{memo_text or content}。", action="memo_add")

        # 「提醒我买牛奶」这类没时间的 -> 存成备忘
        if TRIGGER_ALARM.search(text) and not has_clock_expr(text):
            content = et.clean_content(_extract_after(text, ("提醒我", "提醒", "叫我")))
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
        r"文件|文档|附件|报告|表格|日志|脚本|代码|内容|颜色|"
        r"上面|上头|里头|里面|手里|手上|桌上|窗外|镜头前)"
    )
    # 在问「是什么」——必须同时指着看得到的东西，或就是【这是什么】这种短问句，
    # 否则「导师见面那件事是什么时候」也会被当成看图（这个坑踩过）
    # 注意：不包含裸的「有什么」——「这个月有什么」「下周有什么」是查询
    _VISION_WHAT = re.compile(
        r"(?:是什么|是啥|什么东西|写了什么|写的什么|讲了什么|什么颜色|什么字)"
    )
    # 「屏幕上有什么」「里面有什么」：这种「有什么」前面跟着看得见的东西才算看图
    _VISION_HAVE = re.compile(
        r"(?:屏幕|显示器|桌面|窗口|界面|画面|镜头|摄像头|相机|照片|图片|截图|剪贴板|"
        r"文件|文档|表格|图上|图里|照片里|上面|上头|里头|里面|手里|手上|桌上|窗外)"
        r"[^，,。;；?？]{0,4}有(?:什么|哪些)"
    )
    _VISION_TINY = re.compile(
        r"^[这那](?:个|张|幅|些|是|块|只|台|本)?\s*(?:是什么|是啥|什么东西|写的什么)"
    )
    # 「看看下周都有什么事」「看一下这周有什么安排」是**查询**，不是看图：
    # 这些「什么 + 抽象名词」根本不是看得到的东西（这个坑评估里踩到了）
    _VISION_NOT = re.compile(
        r"(?:什么事|有什么安排|有什么日程|有什么活动|有什么计划|有什么任务|都有什么|"
        r"有哪些安排|有哪些事|什么时候)"
    )
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
        tiny = bool(self._VISION_TINY.match(text))       # 「这是什么」这种短问句
        have = bool(self._VISION_HAVE.search(text))      # 「屏幕上有什么」
        if not (read or last) and self._VISION_NOT.search(text):
            return None                     # 「看看这周有什么安排」是查询
        wanted = (
            read                                          # 「读一下会议纪要」
            or last                                       # 「刚才那张图」
            or (look and (obj or what))                   # 「看看我的屏幕」「看看这是什么」
            or (what and (obj or tiny))                   # 「屏幕上写了什么」「它是什么颜色」
            or have                                       # 「屏幕上有什么」「里面有什么」
        )
        if not wanted:
            return None
        # 「看看今天的日程」「念一下明天的安排」是在问日程，不是在读文件：
        # 带时间词 + 日程词的一律让给日程技能（它在后面）
        if self._RANGE_WORD.search(text) and (
            TRIGGER_SCHEDULE.search(text) or parse_weekday(text) is not None
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
    # 「提了时间但可能解析不出来」的补充词（只用于「如实说没听懂」的判断，
    # 不影响任何路由，所以宁宽勿窄）
    _MENTION_TIME_EXTRA = re.compile(
        r"这两天|两周|几个?星期|几天|几年|未来|接下来|以后|之后|下下|月份?"
    )
    # 这些是问别的事，不是问日程（「今天天气怎么样」的「怎么样」会撞上 _RANGE_ASK）
    _NOT_SCHEDULE_Q = re.compile(r"(天气|气温|温度|下雨|下雪|雨|雪|新闻|股票|汇率|价格|几点开会?)")
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

    # ------------------------------------------------------------ 日程改 / 删 / 跳过
    def _find_duplicate(self, item: dict, now: datetime, first: datetime) -> dict | None:
        """同名字 + 同一次时间就当成重复：语音里同一句话说两遍太常见了，
        以前会存成两条，第二遍提醒又响一次。"""
        want = et.norm_title(str(item.get("title") or ""))
        if not want:
            return None
        rep = repeat_of(item)
        for it in self.store.load():
            if et.norm_title(str(it.get("title") or "")) != want:
                continue
            if repeat_of(it) != rep:
                continue
            if rep == "once":
                if str(it.get("start") or "")[:16] == first.strftime("%Y-%m-%d %H:%M"):
                    return it
            elif rep in ("weekly", "biweekly"):
                if (int(it.get("weekday", -1)) == int(item.get("weekday", -2))
                        and it.get("time") == item.get("time")):
                    return it
            elif rep == "monthly":
                if it.get("day") == item.get("day") and it.get("time") == item.get("time"):
                    return it
            elif rep == "yearly":
                if (it.get("month") == item.get("month") and it.get("day") == item.get("day")
                        and it.get("time") == item.get("time")):
                    return it
            elif rep == "interval":
                if (it.get("time") == item.get("time")
                        and it.get("every_days") == item.get("every_days")
                        and it.get("every_minutes") == item.get("every_minutes")):
                    return it
        return None

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
        # 批量说法（「所有课程…」）永远不是「新增一条」
        pred, scope = self._scope_filter(text)
        # ★没说要删、又像新增的，让给新增分支★
        # 「明天下午三点安排项目评审会，提前30分钟提醒我」里的「提前」撞上
        # _SCHEDULE_EDIT，结果被当成「改哪一条」去反问用户（实测）。
        if not drop and pred is None and self._looks_like_add(text, now):
            return None
        items = self.store.load()
        if not items:
            # ★店空的时候，别把「要新增」的句子拦下来★
            # 「明天下午三点安排项目评审会，提前30分钟提醒我」里的「提前」会撞上
            # _SCHEDULE_EDIT，于是空库时回一句「没什么可以改的」，明明是要新增却排不上
            # （实测 2026-09-20，全新数据目录时必现）。判据与新增分支共用。
            if self._looks_like_add(text, now):
                return None
            return SkillResult(
                reply="现在还是空的，没什么可以改的。", action="event_change"
            )

        # --- 批量：所有课程 / 所有会议 / 所有日程 ---
        if pred is not None:
            targets = [it for it in items if pred(it)]
            if not targets:
                return SkillResult(reply=f"日程里没有{scope}可以改。", action="event_change")
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
                    action="event_change",
                )
            elif self._REFER_WORDS.search(text):
                return SkillResult(
                    reply="我这边没有可以指代的日程——先说一句「今天有什么课」，或者把名字说清楚？",
                    action="event_change_miss",
                )
        if not hits:
            # 听着像在说日程，但库里找不到对应条目：说清楚比乱改好
            if drop and (TRIGGER_SCHEDULE.search(text) or parse_weekday(text) is not None):
                return SkillResult(
                    reply="没找到你说的那条日程。先问一句「今天有什么课」，或者把名字说清楚一点？",
                    action="event_change_miss",
                )
            return None

        top = hits[0][0]
        if drop and top < 2:
            # 删除是不可逆的：名字没对上（只沾到两个字）就先问清楚，别猜
            names = "、".join(f"「{it.get('title', '安排')}」" for _s, it in hits[:4])
            return SkillResult(
                reply=f"我不太确定你说的是哪一条（可能是{names}）。说清楚名字、或者带上星期几试试？",
                action="event_change_unsure",
            )

        same = [it for s, it in hits if s == top]
        if len(same) > 1:
            names = "、".join(f"「{it.get('title', '安排')}」" for it in same[:4])
            return SkillResult(
                reply=f"有 {len(same)} 条都对得上（{names}），你说是哪一条？",
                action="event_change",
            )

        item = same[0]
        title = display_title(item)
        eid = _key_of(item)
        rep = repeat_of(item)
        recurring = has_repeat(item)

        # ★删之前必须核对时间★（这一步是拿真数据换来的）：
        # 实测「取消明天早上的安排」被接成「删日程」，而且是从上一轮助手自己念过的
        # 话里「指代」到了 9月23日的「跟导师见面」——把一条跟时间完全对不上的日程删了。
        # 删除不可逆，句子里既然带了时间，对不上就一定要拦下来问清楚。
        if drop:
            mismatch = self._drop_time_mismatch(text, item, rep, now)
            if mismatch:
                return SkillResult(reply=mismatch, action="event_change_unsure")

        # --- 删：整条不要了 ---
        # 一次性的直接删；带重复的**要先问一句**（影响的是往后所有次）。
        # 但「取消X」默认**只跳过最近这一次**——带重复的不轻易删（删除不可逆），
        # 只有说了「以后都不上」这类永久说法才删，删之前还要确认一次。
        if drop and not recurring:
            self.store.remove_where(lambda it: _key_of(it) == eid)
            return SkillResult(reply=f"已删除：{title}。", action="event_delete")
        if drop and recurring and self._SCHEDULE_FOREVER.search(text):
            when = self._next_of(item, now) or now
            if not self._confirm_repeat(item, when, "删除"):
                return self._ask_confirm(item, when, "删除")
            self.store.remove_where(lambda it: _key_of(it) == eid)
            return SkillResult(
                reply=f"已删除：{title}，以后不会再提醒了。", action="event_delete"
            )

        # --- 跳过：只取消最近这一次，课表本身留着 ---
        if drop and recurring:
            day = self._skip_day(text, item, now)
            if day < now.date():
                # 「这周三」在周五说已经是过去了：不猜，问清楚（取消是不可逆的）
                return SkillResult(
                    reply=(
                        f"{day.month}月{day.day}日（{WEEKDAY_NAMES[day.weekday()]}）已经过去了。"
                        f"你是想取消下一次吗？说「下次的{title}不上了」就行。"
                    ),
                    action="event_change_miss",
                )
            skips = skipped_of(item)
            if str(day) in skips:
                return SkillResult(
                    reply=f"{day.month}月{day.day}日的{title}本来就没安排。", action="event_skip"
                )
            skips.append(str(day))
            self._mark_skipped(item, day)
            return SkillResult(
                reply=f"好，{humanize(self._at(item, day), now)}的{title}不提醒了，下周照常。",
                action="event_skip",
            )

        if drop:
            self.store.remove_where(lambda it: _key_of(it) == eid)
            return SkillResult(reply=f"已删除日程：{title}。", action="event_delete")

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

        if rep in ("weekly", "biweekly") or fields.get("repeat") in ("weekly", "biweekly"):
            hh, mm = (when.hour, when.minute) if when is not None else parse_hhmm(item.get("time", "09:00"))
            wd = day.weekday() if day is not None else int(item.get("weekday", 0) or 0)
            fields.update(weekday=wd, time=f"{hh:02d}:{mm:02d}", state=None)
        else:
            if when is None and fields.get("repeat") is None:
                return SkillResult(
                    reply=f"想把{title}改到什么时候？比如「挪到明天下午三点」。",
                    action="event_change",
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
        note = et.note_of(text)
        if note:
            fields["note"] = note
        _, loc = et.split_title_location(text)
        if loc:
            fields["location"] = loc

        clear = [k for k, v in fields.items() if v is None]
        for k in clear:
            fields.pop(k)
        if clear:
            # 有字段要消失（比如从「每周三」改成「每月5号」），删掉旧的再写新的
            items_now = self.store.load()
            for it in items_now:
                if _key_of(it) == eid:
                    for k in clear:
                        it.pop(k, None)
                    it.update(fields)
                    break
            self.store.save(items_now)
        else:
            self._update_event(eid, **fields)

        where = f"，地点{loc}" if loc else ""
        cur = next((it for it in self.store.load() if it.get("title") == title), item)
        if rep != "once" and (rep in ("weekly", "biweekly") or fields.get("repeat") is not None):
            reply = f"已改：{title} 现在是{self._when_of(cur, now)}{where}。"
        else:
            reply = f"已改：{title} 改到 {cur.get('start', '')}{where}。"
        if new_leads is not None:
            reply += et.leads_text(new_leads)
        return SkillResult(reply=reply, action="event_change")

    def _scope_filter(self, text: str, allow_partial: bool = False):
        """「所有课程 / 全部会议 / 所有日程」→ (过滤函数, 说法)。

        ``allow_partial``：只看「有哪些课程」这种**纯查询**说法（没有「所有」）。
        """
        t = text or ""
        if not (self._ALL_WORDS.search(t) or (allow_partial and self._SCOPE_ASK.search(t))):
            return None, ""
        if self._SCOPE_COURSE.search(t):
            return (
                lambda it: it.get("category") == "course"
                or repeat_of(it) in ("weekly", "biweekly"),
                "课程",
            )
        if self._SCOPE_MEETING.search(t):
            return (
                lambda it: it.get("category") == "meeting" or "会" in str(it.get("title", "")),
                "会议",
            )
        if self._SCOPE_TASK.search(t):
            return lambda it: it.get("category") == "task", "任务"
        if self._SCOPE_ANY.search(t):
            return lambda it: True, "日程"
        return None, ""

    def _batch_drop(self, targets: list[dict], scope: str, text: str, now: datetime) -> SkillResult:
        """批量取消：一次性的直接删；每周重复的要么「以后都不上」，要么逐条只跳过下一次。"""
        weekly = [it for it in targets if repeat_of(it) in ("weekly", "biweekly")]
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
                action="event_change",
            )

        if forever or once and not weekly:
            ids = {it.get("id") for it in targets}
            removed = self.store.remove_where(lambda it: it.get("id") in ids)
            names = "、".join(str(it.get("title", "安排")) for it in removed[:6])
            tail = "等" if len(removed) > 6 else ""
            return SkillResult(
                reply=f"已删除{cn_quantity(len(removed))}条{scope}：{names}{tail}。",
                action="event_delete",
            )

        # 只跳过下一次
        if not weekly:
            return SkillResult(reply=f"{scope}里没有可以取消的。", action="event_change")
        day = parse_date_hint(text, now)
        done: list[str] = []
        for it in weekly:
            target_day = day if day is not None else self._skip_day("", it, now)
            if target_day < now.date():
                target_day = self._skip_day("", it, now)
            if str(target_day) in skipped_of(it):
                continue
            mark_skipped(it, target_day)
            done.append(f"{target_day.month}月{target_day.day}日的{display_title(it)}")
        if not done:
            return SkillResult(reply=f"{scope}这次本来就没安排。", action="event_skip")
        return SkillResult(
            reply=f"好，这次{'、'.join(done)}都不提醒了，下次照常。", action="event_skip"
        )

    def _batch_edit(self, targets: list[dict], scope: str, text: str, now: datetime) -> SkillResult:
        """批量改：只改这一句里说得清楚的那几项（时刻 / 星期 / 提前量 / 地点 / 备注 / 周期）。"""
        when = parse_datetime(text, now)
        leads = parse_reminds(text)
        rule = parse_repeat(text)
        wd = parse_weekday(text)
        _, loc = et.split_title_location(text)
        note = et.note_of(text)
        changed: list[str] = []
        for it in targets:
            fields: dict[str, Any] = {}
            weekly = repeat_of(it) in ("weekly", "biweekly")
            if wd is not None and weekly:
                fields["weekday"] = wd
                fields["state"] = None       # 换了星期，之前跳过的那些天就不算数了
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
                self._update_event(it.get("id"), **fields)
                changed.append(str(it.get("title", "安排")))
        if not changed:
            return SkillResult(
                reply=f"要{scope}都改什么？可以说「都改到下午三点」或者「都提前半小时提醒」。",
                action="event_change",
            )
        names = "、".join(changed[:6])
        return SkillResult(
            reply=f"已改{cn_quantity(len(changed))}条{scope}：{names}{'等' if len(changed) > 6 else ''}。",
            action="event_change",
        )

    def _referenced_items(self, text: str, now: datetime) -> list[dict]:
        """「它 / 那个 / 刚才那条」指谁：去最近的聊天记录里找日程名字。

        技能不猜：记录里有几个候选就返回几个，由调用方决定是执行还是反问。
        """
        hay = "\n".join(self.dialog[-8:])
        if not hay.strip():
            return []
        items = self.store.load()
        hits = self._score_items(hay, items, now)
        return [it for s, it in hits if s >= 2]     # 至少要有名字或时间对得上

    def _list_scope(self, text: str, now: datetime) -> SkillResult | None:
        """「有哪些课程 / 所有会议」——把所有定义列出来，而不是只看某一天。"""
        pred, scope = self._scope_filter(text, allow_partial=True)
        if pred is None:
            return None
        # 「每周五有什么课」「9月20日有哪些课」是在问某一天，别按全体列表回答
        if (self._RANGE_WORD.search(text)
                or parse_weekday(text) is not None
                or parse_date_hint(text, now) is not None):
            return None
        targets = [it for it in self.store.load() if pred(it)]
        if not targets:
            return SkillResult(reply=f"日程里还没有{scope}。", action="event_list")
        parts = []
        for it in targets:
            nxt = self._next_of(it, now)
            when = f"下一次{nxt.strftime('%m-%d %H:%M')}" if nxt else "已经过期"
            title = str(it.get("title") or "安排")
            parts.append(f"{title}（{when}）" if repeat_of(it) == "once"
                         else f"{et.when_text(it)} {title}，{when}")
        return SkillResult(
            reply=f"一共{cn_quantity(len(targets))}条{scope}：" + "；".join(parts) + "。",
            action="event_list",
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
            score = 2 * et.title_hit(str(it.get("title", "")), hay)
            rep = repeat_of(it)
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

    def _drop_time_mismatch(self, text: str, item: dict, rep: str, now: datetime) -> str | None:
        """要删的这条跟句子里说的时间对得上吗？对不上就返回「一句拒绝的话」。

        只拦「句子里明确带了时间」的情况；没提时间的（「删掉组会」）照旧放行。
        周期课按**星期几 + 时刻**比（“取消这周三的课”本来就允许跨到下一次），
        一次性/间隔类按**日期 + 时刻**比。
        """
        day = parse_date_hint(text, now)
        clock = _clock_with_period(text, now)
        if day is None and clock is None:
            return None
        title = str(item.get("title") or "安排")
        if rep in ("weekly", "biweekly"):
            if day is not None and int(item.get("weekday", -1)) != day.weekday():
                return (
                    f"「{title}」是{self._when_of(item, now)}"
                    f"，不是{self._day_label(day, now, True)}——要删它就说「删掉{title}」。"
                )
            if clock is not None and str(item.get("time", "")) != f"{clock[0]:02d}:{clock[1]:02d}":
                return (
                    f"「{title}」是{self._when_of(item, now)}开始的"
                    f"，跟你说的时间对不上——要删它就说「删掉{title}」。"
                )
            return None
        nxt = self._next_of(item, now - timedelta(seconds=1))
        if nxt is None:
            return f"「{title}」算不出下一次时间，我不删——你说清楚名字再试。"
        when_text = humanize(nxt, now)
        if day is not None and nxt.date() != day:
            return (
                f"「{title}」是{nxt.month}月{nxt.day}日{clock_text(nxt)}，"
                f"不是{self._day_label(day, now, True)}"
                f"——要删它就说「删掉{title}」。"
            )
        if clock is not None and (nxt.hour, nxt.minute) != clock:
            return (
                f"「{title}」是{when_text}，跟你说的时间对不上"
                f"——要删它就说「删掉{title}」。"
            )
        return None

    def _skip_day(self, text: str, item: dict, now: datetime) -> date:
        """算出这次要跳过哪一天：句子里有日期就用它，否则用最近的那一次。"""
        day = parse_date_hint(text, now)
        if day is not None:
            return day
        nxt = self._next_of(item, now - timedelta(seconds=1))
        return nxt.date() if nxt is not None else now.date()

    def _at(self, item: dict, day: date) -> datetime:
        hh, mm = parse_hhmm(item.get("time", "09:00"))
        return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)

    def _mark_skipped(self, item: dict, day: date) -> None:
        """跳过一次（记进 state.skipped）并落盘：课表本身留着。"""
        mark_skipped(item, day)
        self.store.save(self.store.load())

    def _update_event(self, event_id: Any, **fields: Any) -> None:
        """按 id 改一条（字段就地在库里改，不动别条）。"""
        for i, it in enumerate(self.store.load(), start=1):
            if _key_of(it) == event_id:
                self.store.update(i, **fields)
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

    def _range_of(self, text: str, now: datetime) -> tuple[date, date, str, bool]:
        """解析「问的是哪一段时间」，返回 (起, 止, 说法, 听懂了没)，闭区间。

        以前「下周」被算成「下周**一**那一天」，而「这个月 / 下个月 / 未来三天」
        全都落到「今天」——问一段时间却只报一天。这里把所有范围都摊开：
        日 / 周 / 周末 / 月 / 年 / 未来 N 天。

        末尾那个 bool 是为「没听懂」准备的：以前解析不出来就**静静地**退到「今天」，
        工具层拿这个答案去回答「下周三下午」就成了谎话（模型直接拿它下结论）。
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
            return d, d, "大后天", True
        if "后天" in t:
            d = today + timedelta(days=2)
            return d, d, "后天", True
        if "明天" in t or "明日" in t:
            d = today + timedelta(days=1)
            return d, d, "明天", True
        # ★「今天」要显式认出来★：以前它靠「都没命中 → 默认今天」兜着，
        # 而默认那条现在标成「没听懂」，会把「今天有什么课」当成听不懂的时间段。
        if re.search(r"(今天|今日)", t):
            return today, today, "今天", True
        # 周末（周六+周日）
        if re.search(r"(下下个?周末|下下周末)", t):
            s = week_of(2)[0] + timedelta(days=5)
            return s, s + timedelta(days=1), "下下个周末", True
        if re.search(r"(下个?周末|下周末)", t):
            s = week_of(1)[0] + timedelta(days=5)
            return s, s + timedelta(days=1), "下周末", True
        if re.search(r"(这个?周末|本周末)", t):
            s = week_of(0)[0] + timedelta(days=5)
            if s < today:               # 已经过了就顺延到下一个周末
                s += timedelta(days=7)
            return s, s + timedelta(days=1), "这个周末", True
        # ★裸「周末」★：以前落到「今天」（问周末却报今天）
        if "周末" in t:
            s = week_of(0)[0] + timedelta(days=5)
            if s < today:
                s += timedelta(days=7)
            return s, s + timedelta(days=1), "这个周末", True
        # ★单个星期几（「周三 / 下周三 / 这周三」）→ 那一天，不是整周★
        # 必须放在「整周」前面：「下周三」里也含「下周」，先匹整周就会
        # 把「下周三下午有没有空」答成整周。没写限定时往后找最近的那一天。
        m = re.search(r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", t)
        if m:
            idx = _WEEKDAY_CN.get(m.group(2))
            if idx is not None:
                word = m.group(1) or ""
                monday = today - timedelta(days=today.weekday())
                if word == "下下":
                    d = monday + timedelta(weeks=2, days=idx)
                elif word == "下":
                    d = monday + timedelta(weeks=1, days=idx)
                else:
                    d = monday + timedelta(days=idx)
                    if d < today:           # 本周（或今天）已经过了 → 顺延到下一次
                        d += timedelta(days=7)
                # ★说法按解析出来的那天重新生成★：「这周三」已经过了会被顺延到
                # 下周，叫「这周三（9月23日）」就自相矛盾了。
                weeks_ahead = (d - monday).days // 7
                word = {0: "这", 1: "下"}.get(weeks_ahead, "下下")
                return d, d, f"{word}周{m.group(2)}（{d.month}月{d.day}日）", True
        # 整周（周一起算）
        if re.search(r"(下下个?周|下下星期|下下个星期)", t):
            s, e = week_of(2)
            return s, e, "下下周", True
        if re.search(r"(下周|下个星期|下星期|下个礼拜)", t):
            s, e = week_of(1)
            return s, e, "下周", True
        if re.search(r"(这周|本周|这个星期|这星期|本星期|这一周|整周|全周)", t):
            s, e = week_of(0)
            return s, e, "这周", True
        # 整月
        if re.search(r"(下个月|下月)", t):
            s, e = month_of(1)
            return s, e, "下个月", True
        if re.search(r"(这个月|本月|当月)", t):
            s, e = month_of(0)
            return s, e, "这个月", True
        # 整年
        if re.search(r"(明年|下一年)", t):
            y = today.year + 1
            return date(y, 1, 1), date(y, 12, 31), "明年", True
        if re.search(r"(今年|本年度)", t):
            return date(today.year, 1, 1), date(today.year, 12, 31), "今年", True
        # 未来 N 天 / 未来一周 / 未来一个月
        m = re.search(r"(?:未来|接下来|后面|最近)\s*(\d{1,2}|[一二三四五六七八九十两]+)\s*天", t)
        if m:
            n = max(1, int(cn2num(m.group(1)) or 1))
            return today, today + timedelta(days=n - 1), f"未来{cn_number(n)}天", True
        if re.search(r"(?:未来|接下来|后面)\s*(?:一)?(?:周|星期|礼拜)", t):
            return today, today + timedelta(days=6), "未来一周", True
        if re.search(r"(?:未来|接下来)\s*(?:一)?个?月", t):
            return today, today + timedelta(days=29), "未来一个月", True
        if "这两天" in t:
            return today, today + timedelta(days=1), "这两天", True
        if re.search(r"(这几天|最近几天)", t):
            return today, today + timedelta(days=3), "这几天", True
        if re.search(r"(最近|接下来)", t):
            return today, today + timedelta(days=6), "最近一周", True
        return today, today, "今天", False          # 没听懂：只说「今天」但标清楚

    def range_understood(self, text: str, now: datetime) -> bool:
        """这句话里的时间段真的解析出来了吗（工具层用来决定「如实说没听懂」）。"""
        return self._range_of(text, now)[3]

    def _day_items(self, text: str, now: datetime) -> SkillResult | None:
        start, end, label, ok = self._range_of(text, now)
        # ★没听懂时间段就别拿「今天」顶替★：问「这两周有安排吗」答「今天没有安排」
        # 是假话，而工具层会把这句话当成事实去下结论（实测：模型据此说「下周三下午有空」）。
        # 提了时间又没解析出来 → 交白卷，让工具层/上层去反问。
        if not ok and self.mentions_time(text):
            return None
        # 只问一天时，句中的时段也要算进去：「下周三下午有没有空」
        # 不能把上午的课也算成「有安排」。
        period = None
        if start == end:
            hit = _DAY_PERIOD.search(text or "")
            window = _PERIOD_RANGE.get(hit.group(1)) if hit else None
            if window:
                period = window
                base, _sep, tail = label.partition("（")
                label = f"{base}{hit.group(1)}（{tail}" if tail else f"{base}{hit.group(1)}"
        return self._range_items(start, end, label, now, period=period)

    def mentions_time(self, text: str) -> bool:
        """这句里有没有「时间说法」。

        用来区分「换个说法再问」和「他确实问了某段时间」：
        工具层解析不出时间段时，提了时间的要如实说没听懂，
        没提时间的（「我的日程」）才能退到「今天有什么」。
        """
        t = text or ""
        return bool(
            self._RANGE_WORD.search(t)
            or _DAY_PERIOD.search(t)
            or _WEEKDAY_WORD.search(t)
            or self._MENTION_TIME_EXTRA.search(t)
            or parse_datetime(t) is not None
        )

    def _on_day(self, day: date, now: datetime) -> list[tuple[datetime, dict]]:
        """某一天里所有会发生的事件（重复规则由 :func:`events.occurrences` 展开）。

        ★不再按「闹钟/日程」筛★：没有那个区分了——今天要吃药和今天要上课一样都是
        「今天的事」。真要看某一类，用 category 筛（「有哪些课」走 _scope_filter）。
        """
        from .events import occurrences

        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        out: list[tuple[datetime, dict]] = []
        for item in self.store.load():
            for when in occurrences(item, start, limit=4):
                if when >= end:
                    break
                out.append((when, item))
        out.sort(key=lambda pair: pair[0])
        return out

    def _range_items(
        self, start: date, end: date, label: str, now: datetime,
        period: tuple[int, int] | None = None,
    ) -> SkillResult:
        """把一段时间里的安排按天列出来（重复规则由 events.occurrences 展开）。

        ``period``：只看这个时段（左闭右开小时区间），用于「下周三下午」这类问法。
        """
        # 问「这周 / 这个月」时只报还没过去的：周一说「这周安排」不想再听上周三的课
        if start != end:
            start = max(start, now.date())
        found: list[tuple[date, str]] = []
        total = 0
        day = start
        while day <= end:
            items = self._on_day(day, now)
            if period is not None:
                items = [(w, i) for w, i in items if period[0] <= w.hour < period[1]]
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
            return SkillResult(reply=f"{label}没有课程或会议安排。", action="event_query")
        if start == end:
            return SkillResult(
                reply=f"{label}有{cn_quantity(total)}项安排：" + "；".join(d for _day, d in found) + "。",
                action="event_query",
            )
        span = f"{label}（{start.month}月{start.day}日到{end.month}月{end.day}日）"
        # 跨度大了就得把日期写出来，不然「周三」分不清是哪一周
        with_date = (end - start).days > 31
        detail = "；".join(f"{self._day_label(d, now, with_date)}{txt}" for d, txt in found)
        return SkillResult(
            reply=f"{span}有{cn_quantity(total)}项安排：{detail}。",
            action="event_query",
        )

    def _next_item(self, now: datetime) -> SkillResult:
        best: tuple[datetime, dict] | None = None
        for offset in range(0, 15):
            day = now.date() + timedelta(days=offset)
            for when, item in self._on_day(day, now):
                end = when + timedelta(minutes=int(item.get("duration_minutes", 60) or 60))
                if end <= now:
                    continue
                if best is None or when < best[0]:
                    best = (when, item)
        if best is None:
            return SkillResult(reply="未来两周内没有安排。", action="event_next")
        when, item = best
        delta = (when - now).total_seconds()
        where = f"，地点{item['location']}" if item.get("location") else ""
        if delta <= 0:
            return SkillResult(reply=f"你正在进行：{item.get('title', '')}{where}。", action="event_next")
        return SkillResult(
            reply=f"下一项是{humanize(when, now)}的{item.get('title', '安排')}{where}，还有{humanize_delta(delta)}。",
            action="event_next",
        )
    # ======================================================================
    # 显示开关
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
    def due_events(self, now: datetime) -> list[tuple[dict, datetime, int]]:
        """到点的事件（条目, 发生时间, 提前量）；会就地写回 state.fired。"""
        return self.store.due_now(now)

    def stats(self) -> str:
        items = self.store.load()
        repeat = sum(1 for it in items if has_repeat(it))
        return (f"事件 {len(items)} 条（其中重复 {repeat} 条）/ "
                f"备忘 {len(self.memos.load())} 条  ({self.data_dir})")
