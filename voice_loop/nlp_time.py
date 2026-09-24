"""中文时间表达解析（定闹钟 / 记日程要用）。

覆盖的常见说法：
    时间点： 三点、下午三点半、晚上7点20、19:30、明天早上八点、后天下午两点
    日期：   今天 / 明天 / 后天 / 大后天 / 下周三 / 本周五 / 9月20号
    时段：   早上 / 上午 / 中午 / 下午 / 傍晚 / 晚上 / 夜里 / 凌晨
    时长：   十分钟后 / 半小时后 / 一个半小时 / 2小时15分
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}

# 「这/本 = 本周」「下 = 下一周」「上 = 上一周」——相对**本周**（周一起算）偏移几周
_WEEK_OFFSET = {
    "这": 0, "这个": 0, "本": 0,
    "下": 1, "下个": 1, "下下": 2,
    "上": -1, "上个": -1, "上上": -2,
}

# 时段 -> 需要补的小时数（只在 hour < 12 时生效；0 表示不补）
#   上午七点 -> 7      下午三点 -> 15     晚上八点 -> 20     凌晨两点 -> 2
PERIOD_OFFSET = {
    "凌晨": 0,
    "早上": 0,
    "早晨": 0,
    "清晨": 0,
    "上午": 0,
    "中午": 0,
    "正午": 0,
    "下午": 12,
    "傍晚": 12,
    "晚上": 12,
    "夜里": 12,
    "夜晚": 12,
    "半夜": 0,
}

# 「今晚」这类词同时含日期与时段信息，统一展开成好识别的写法
_PERIOD_FIX = {
    "今晚": "今天晚上",
    "今早": "今天早上",
    "今早上": "今天早上",
    "明晚": "明天晚上",
    "明早": "明天早上",
    "明早上": "明天早上",
    "今日晚": "今天晚上",
}


def cn2num(text: str) -> int | None:
    """把「十五 / 23 / 两」这类写法转成整数；失败返回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    s = s.replace("两", "二")
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = CN_DIGITS.get(head, 1) if head else 1
        ones = CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in CN_DIGITS:
            return None
        if tail and tail not in CN_DIGITS:
            return None
        return tens * 10 + ones
    if len(s) == 1:
        return CN_DIGITS.get(s)
    if all(c in CN_DIGITS for c in s):  # 「二三」→ 23
        return int("".join(str(CN_DIGITS[c]) for c in s))
    return None


def parse_duration(text: str) -> timedelta | None:
    """解析「十分钟后 / 一个半小时 / 2小时15分」这类时长。"""
    t = (text or "").strip()
    if not t:
        return None
    total = timedelta()

    m = re.search(r"(?:一|1|\d+)?个?半\s*(?:个)?\s*(?:小时|钟头)", t)
    if m:
        total += timedelta(minutes=30)

    m = re.search(r"(\d+|[一二三四五六七八九十两]+)\s*个\s*半\s*(?:小时|钟头)", t)
    if m:
        n = cn2num(m.group(1))
        if n:
            total += timedelta(hours=n)

    for pattern, unit in (
        (r"(\d+|[一二三四五六七八九十两]+)\s*(?:个)?\s*(?:小时|钟头)", "hours"),
        (r"(\d+|[一二三四五六七八九十两]+)\s*分钟?", "minutes"),
        (r"(\d+|[一二三四五六七八九十两]+)\s*秒", "seconds"),
    ):
        for m in re.finditer(pattern, t):
            n = cn2num(m.group(1))
            if n is None:
                continue
            total += timedelta(**{unit: n})

    return total if total.total_seconds() > 0 else None


def parse_clock(text: str) -> tuple[int, int] | None:
    """从文本里解析出「几点几分」，返回 (hour, minute)。"""
    t = (text or "").strip()

    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi

    m = re.search(r"(\d{1,2}|[一二三四五六七八九十两]+)\s*[点時时]", t)
    if not m:
        return None
    hour = cn2num(m.group(1))
    if hour is None:
        return None

    minute = 0
    tail = t[m.end() :]
    if tail.startswith("半"):
        minute = 30
    else:
        # ★「分」是可选的★：以前要求必须带「分」，「8点45」被当成 8:00
        # （整个分钟被吞了，实测用户说「8点45提醒我练琴」被排到了 08:00）。
        # 但也不能看到任何数字就当分钟：「8点2个闹钟」里的 2 不是分钟，
        # 所以只接受「两位以上」或者「前面有零」的写法（45 / 05 / 四十五）。
        m2 = re.match(r"\s*(\d{1,2}|[一二三四五六七八九十两]+)\s*分?", tail)
        if m2:
            raw_min = m2.group(1)
            v = cn2num(raw_min)
            two_digit = len(raw_min) >= 2 or raw_min.startswith("0")
            has_fen = "分" in tail[: len(raw_min) + 2]
            if v is not None and 0 <= v <= 59 and (two_digit or has_fen):
                minute = v
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        return None
    return hour % 24 if hour == 24 else hour, minute


def _apply_period(hour: int, text: str, minute: int = 0, now: datetime | None = None) -> int:
    """根据「下午 / 晚上」等词把 12 小时制补成 24 小时制。

    没有时段词时，再看**现在几点**（以前一律当上午，实测踩坑）：
    """
    for word, offset in PERIOD_OFFSET.items():
        if word not in text:
            continue
        if word == "凌晨" and hour == 12:
            return 0            # 凌晨十二点 -> 0 点
        if word in ("中午", "正午") and hour <= 2:
            return 12 + hour    # 中午一点 -> 13 点
        if offset and hour < 12:
            return hour + offset
        return hour

    # --- 没有时段词：按「现在」推断 ---
    # 晚上 20:36 说「8点45」= 今晚 20:45（不是明天早上 8:45，已经过了还等到明天就说不过去）；
    # 早上 8:30 说「8点」保持上午（不该跳成今晚 20:00）；
    # 晚上说「7点」而 19:00 已过 → 保持上午 → 明天 07:00。
    if now is None or hour >= 12 or now.hour < 12:
        return hour
    if (hour + 12, minute) > (now.hour, now.minute):
        return hour + 12
    return hour


def parse_date_hint(text: str, now: datetime) -> date | None:
    """解析 今天 / 明天 / 下周三 / 9月20号 这类日期。"""
    t = text or ""
    if "大后天" in t:
        return now.date() + timedelta(days=3)
    if "后天" in t:
        return now.date() + timedelta(days=2)
    if "明天" in t or "明日" in t or "明早" in t or "明晚" in t:
        return now.date() + timedelta(days=1)
    if "今天" in t or "今日" in t or "今晚" in t:
        return now.date()

    m = re.search(
        r"(下下|下个|下|上个|上上|上|这个|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天末])", t
    )
    if m:
        prefix, day_char = m.group(1) or "", m.group(2)
        target = _WEEKDAY_CN.get(day_char)
        if target is not None:
            if prefix in _WEEK_OFFSET:
                # 「这周三 / 本周三 / 上周三」都是相对**本周**（周一起算）算的，
                # 所以可能落在过去（周五说「这周三」就是前天），这是有意的：
                # 「取消这周三的课」必须指向真的那一天。
                delta = target - now.weekday() + 7 * _WEEK_OFFSET[prefix]
            else:
                # 只说「周三」= 最近的将来那个周三。注意这里 **不能** 再 +7：
                # (target - today) % 7 已经滚到下一周了，
                # 以前再 +7 就把「下周三」多算了一周（周五说 → 9/30 而不是 9/23）。
                delta = (target - now.weekday()) % 7
            return now.date() + timedelta(days=delta)

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(now.year, month, day)
        except ValueError:
            return None
        if candidate < now.date():
            try:
                candidate = date(now.year + 1, month, day)
            except ValueError:
                return None
        return candidate

    m = re.search(r"(\d{1,2})\s*[号日]", t)
    if m:
        day = int(m.group(1))
        try:
            candidate = date(now.year, now.month, day)
        except ValueError:
            return None
        if candidate < now.date():
            month = now.month % 12 + 1
            year = now.year + (1 if month == 1 else 0)
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
        return candidate
    return None


def parse_datetime(text: str, now: datetime | None = None, roll_forward: bool = True) -> datetime | None:
    """从一句话里解析出目标时间点。

    ``roll_forward=True`` 时，如果解析出的时刻已经过去（例如晚上说「七点起床」），
    会自动顺延到明天。
    """
    now = now or datetime.now()
    t = (text or "").strip()
    if not t:
        return None
    for src, dst in _PERIOD_FIX.items():
        t = t.replace(src, dst)

    # 相对时长：「十分钟后提醒我」「一个半小时后」
    # 注意：不能用「时」判断，因为「小时」里也有「时」
    if re.search(r"(后|以后|之后|过后)", t) and not re.search(r"点|[:：]\s*\d", t):
        delta = parse_duration(t)
        if delta:
            return now + delta

    clock = parse_clock(t)
    if clock is None:
        # 只有日期没有时刻 -> 当天 09:00
        d = parse_date_hint(t, now)
        if d is None:
            return None
        return datetime.combine(d, time(9, 0))

    hour, minute = clock
    hour = _apply_period(hour, t, minute, now)

    day = parse_date_hint(t, now)
    explicit_day = day is not None
    if day is None:
        day = now.date()

    target = datetime.combine(day, time(hour, minute))
    if roll_forward and not explicit_day and target <= now:
        target += timedelta(days=1)
    return target


# --------------------------------------------------------------------------- #
# 重复周期 / 提前量
# --------------------------------------------------------------------------- #
_MONTHLY = re.compile(r"每(?:个)?月")
_YEARLY = re.compile(r"每(?:个)?年")
_EVERY_MINUTES = re.compile(r"每\s*(\d{1,3}|[一二三四五六七八九十两]+)\s*(?:个)?\s*(?:分钟|分)(?!钟)")
_EVERY_HOURS = re.compile(r"每\s*(\d{1,3}|[一二三四五六七八九十两]+|半)\s*(?:个)?\s*(?:小时|钟头)")
_EVERY_DAYS = re.compile(r"每\s*(\d{1,3}|[一二三四五六七八九十两]+)\s*天")
_EVERY_WEEKS = re.compile(r"每\s*(\d{1,2}|[一二三四五六七八九十两]+)\s*(?:个)?\s*(?:周|星期|礼拜)")
_DAILY = re.compile(r"每天|每晚|每早|每日")
# ★工作日★：用户的真实需求是「工作日早上 8 点半给我定闹钟」——
# 它不能用「每 7 天」或「每天」硬拼（周六周日要跳掉），所以做成独立的一种周期。
_WORKDAYS = re.compile(r"工作日|周一到周五|周一至周五|星期一到星期五|礼拜一到礼拜五")
_HOURLY = re.compile(r"每小时|每个小时")
_WEEKDAY_IN_TEXT = re.compile(r"(?:周|星期|礼拜)\s*([一二三四五六日天])")
_MONTH_IN_TEXT = re.compile(r"(\d{1,2}|[一二三四五六七八九十]+)\s*月\s*(\d{1,2}|[一二三四五六七八九十]+)\s*[号日]")
_DAY_IN_TEXT = re.compile(r"(\d{1,2}|[一二三四五六七八九十]+)\s*[号日]")


def parse_weekday(text: str) -> int | None:
    """「周三 / 星期三 / 礼拜天」→ 0（周一）… 6（周日）；没说返回 None。

    和 :func:`parse_date_hint` 的区别：它只说**星期几**，不管这是本周还是下周。
    「每周交周报」这种没写星期几的情况需要它来判断要不要兜底。
    """
    m = _WEEKDAY_IN_TEXT.search(text or "")
    return _WEEKDAY_CN.get(m.group(1)) if m else None


def parse_repeat(text: str) -> dict | None:
    """解析重复周期，返回可以直接并进事件条目的字段。

    支持：每周三 / 每两周周三 / 每月5号 / 每年3月1日 / 每3天 / 每2小时 /
    **每天** / **工作日（周一到周五）**。

    ``weekly`` / ``daily`` / ``weekdays`` 是**固定日历周期**（点说一个钟点，就总是在那时刻）；
    ``interval`` 是「这次结束后再过 N 天/分钟」（会漂移），两者别混。
    """
    t = text or ""
    m = _YEARLY.search(t)
    if m:
        mm = _MONTH_IN_TEXT.search(t)
        out = {"repeat": "yearly"}
        if mm:
            out["month"] = int(cn2num(mm.group(1)) or 0) or None
            out["day"] = int(cn2num(mm.group(2)) or 0) or None
            out = {k: v for k, v in out.items() if v}
        return out
    m = _MONTHLY.search(t)
    if m:
        dm = _DAY_IN_TEXT.search(t)
        out = {"repeat": "monthly"}
        if dm:
            day = cn2num(dm.group(1))
            if day:
                out["day"] = int(day)
        return out
    # ★工作日要在「每N天/每天」之前判★（「工作日」里有个「天」字，但前面没有数字，
    # 其实不会误配——放前面是为了意图更清楚，不依赖正则的巧合）
    if _WORKDAYS.search(t):
        return {"repeat": "weekdays"}
    m = _EVERY_MINUTES.search(t)
    if m:
        n = cn2num(m.group(1))
        if n:
            return {"repeat": "interval", "every_minutes": int(n)}
    m = _EVERY_HOURS.search(t)
    if m:
        n = 0.5 if m.group(1) == "半" else cn2num(m.group(1))
        if n:
            return {"repeat": "interval", "every_minutes": int(round(float(n) * 60))}
    m = _EVERY_DAYS.search(t)
    if m:
        n = cn2num(m.group(1))
        if n:
            return {"repeat": "interval", "every_days": int(n)}
    m = _EVERY_WEEKS.search(t)
    if m:
        n = int(cn2num(m.group(1)) or 1)
        out: dict = {}
        if n == 1:
            out["repeat"] = "weekly"
        elif n == 2:
            out["repeat"] = "biweekly"
        else:
            out["repeat"] = "interval"
            out["every_days"] = n * 7
        wd = _WEEKDAY_IN_TEXT.search(t)
        if wd:
            out["weekday"] = _WEEKDAY_CN[wd.group(1)]
        return out
    if _HOURLY.search(t):
        return {"repeat": "interval", "every_minutes": 60}
    if _DAILY.search(t):
        # ★改动★：以前返回 interval+every_days=1（= 上次结束的 24 小时后，会漂），
        # 现在是固定钟点的 daily——「每天早上七点吃药」就该天在七点。
        return {"repeat": "daily"}
    wd = _WEEKDAY_IN_TEXT.search(t)
    if re.search(r"每(?:个)?(?:周|星期|礼拜)", t):
        # 光说「每周」也算每周重复（只是没指定星期几，由调用方按说话那天兜底）
        return {"repeat": "weekly", "weekday": _WEEKDAY_CN[wd.group(1)]} if wd else {"repeat": "weekly"}
    return None


_UNTIL = re.compile(r"(?:到|直到)\s*(.+?)\s*(?:为止|以前|之前)")
_LEAD_UNITS = {"天": 1440, "日": 1440, "小时": 60, "钟头": 60, "分钟": 1, "分": 1, "刻": 15}
_LEAD_TOKEN = re.compile(r"(\d{1,3}|[一二三四五六七八九十两]+|半)\s*(?:个)?\s*(天|日|小时|钟头|分钟|分|刻)")
_LEAD_ON_TIME = re.compile(r"(到点|准时|正点|开始时|开始的时候)")


def parse_reminds(text: str, *, maximum: int = 5) -> list[int] | None:
    """解析「提前多久提醒」的多个提前量，返回分钟数组（0 = 到点）。

    「提前一天和半小时提醒我」-> [1440, 30]
    「提前30分钟、10分钟和到点提醒我」-> [30, 10, 0]
    「到点提醒我」-> [0]
    没说就返回 None（调用方用默认值）。
    """
    t = text or ""
    has_lead = bool(re.search(r"(提前|预先|事先)", t))
    if not has_lead:
        return [0] if _LEAD_ON_TIME.search(t) else None
    tail = re.split(r"(?:提前|预先|事先)", t, maxsplit=1)[-1]
    # 到「提醒/叫我」为止，别把后面「还有课」那半句也算进来
    tail = re.split(r"(?:提醒|叫我|喊我|通知|通知我)", tail)[0]
    leads: list[int] = []
    for m in _LEAD_TOKEN.finditer(tail):
        raw = m.group(1)
        n = 0.5 if raw == "半" else cn2num(raw)
        if n is None:
            continue
        leads.append(int(round(float(n) * _LEAD_UNITS[m.group(2)])))
    if _LEAD_ON_TIME.search(tail):
        leads.append(0)
    leads = sorted({v for v in leads if v >= 0}, reverse=True)
    return leads[:maximum] or None


def parse_until(text: str, now: datetime | None = None) -> date | None:
    """「到 12 月底为止」这种循环截止日期。"""
    t = text or ""
    m = _UNTIL.search(t)
    if not m:
        return None
    now = now or datetime.now()
    frag = m.group(1).strip()

    iso = re.search(r"(\d{4})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})", frag)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    # 「年底 / 月末」这类模糊说法统一当成那一段的最后一天
    if re.search(r"(年底|年末|今年底)", frag):
        return date(now.year, 12, 31)
    if re.search(r"(月底|月末|这个月底)", frag) and not re.search(r"\d|一二三四五六七八九十", frag):
        return date(now.year, now.month, _days_in_month(now.year, now.month))
    mo = re.search(r"(\d{1,2}|[一二三四五六七八九十]+)\s*月(?!\s*\d)", frag)
    if mo:
        month = int(cn2num(mo.group(1)) or 0)
        if 1 <= month <= 12:
            year = now.year + (1 if month < now.month else 0)
            if re.search(r"(底|末)", frag):
                return date(year, month, _days_in_month(year, month))
            return date(year, month, _days_in_month(year, month))

    day = parse_date_hint(frag, now)
    if day is not None:
        return day
    return None


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


_CN_NUM = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """把数字写成中文（0~9999），用于语音播报（15 -> 十五，153 -> 一百五十三）。"""
    n = int(n)
    if n < 0 or n > 9999:
        return str(n)
    if n < 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + (_CN_NUM[n - 10] if n > 10 else "")
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_NUM[tens] + "十" + (_CN_NUM[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = _CN_NUM[hundreds] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + _CN_NUM[rest]
        return head + cn_number(rest)
    thousands, rest = divmod(n, 1000)
    head = _CN_NUM[thousands] + "千"
    if rest == 0:
        return head
    if rest < 100:
        return head + "零" + cn_number(rest)
    return head + cn_number(rest)


def cn_quantity(n: int) -> str:
    """量词场景：两个 / 两天 / 两点（而不是「二个」）。"""
    return "两" if int(n) == 2 else cn_number(n)


def clock_text(target: datetime) -> str:
    """只取「时段 + 几点几分」，例：晚上十一点五十三分。"""
    h, m = target.hour, target.minute
    if h < 5:
        period, h12 = "凌晨", h
    elif h < 9:
        period, h12 = "早上", h
    elif h < 12:
        period, h12 = "上午", h
    elif h == 12:
        period, h12 = "中午", 12
    elif h < 18:
        period, h12 = "下午", h - 12
    elif h < 20:
        period, h12 = "傍晚", h - 12
    else:
        period, h12 = "晚上", h - 12

    hour_cn = cn_quantity(h12)
    if m == 0:
        return f"{period}{hour_cn}点"
    if m == 30:
        return f"{period}{hour_cn}点半"
    return f"{period}{hour_cn}点{cn_number(m)}分"


def day_text(target: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    delta_days = (target.date() - now.date()).days
    if delta_days == 0:
        return "今天"
    if delta_days == 1:
        return "明天"
    if delta_days == 2:
        return "后天"
    if delta_days == 3:
        return "大后天"
    if 0 < delta_days < 7:
        return f"{cn_quantity(delta_days)}天后"
    return f"{cn_number(target.month)}月{cn_number(target.day)}日"


def humanize(target: datetime, now: datetime | None = None) -> str:
    """把时间点转成自然中文说法，用于语音播报。"""
    now = now or datetime.now()
    return f"{day_text(target, now)}{clock_text(target)}"


def humanize_delta(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{cn_quantity(seconds)}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{cn_quantity(minutes)}分钟" + (f"{cn_number(sec)}秒" if sec else "")
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        text = f"{cn_quantity(days)}天"
        if hours:
            text += f"{cn_quantity(hours)}小时"
        return text + (f"{cn_quantity(minutes)}分钟" if minutes and not hours else "")
    return f"{cn_quantity(hours)}小时" + (f"{cn_quantity(minutes)}分钟" if minutes else "")
