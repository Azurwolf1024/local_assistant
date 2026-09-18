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
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

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
        m2 = re.match(r"\s*(\d{1,2}|[一二三四五六七八九十两]+)\s*分", tail)
        if m2:
            v = cn2num(m2.group(1))
            if v is not None:
                minute = v
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        return None
    return hour % 24 if hour == 24 else hour, minute


def _apply_period(hour: int, text: str) -> int:
    """根据「下午 / 晚上」等词把 12 小时制补成 24 小时制。"""
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

    m = re.search(r"(下{0,2})(?:个)?(?:周|星期|礼拜)\s*([一二三四五六日天末])", t)
    if m:
        prefix, day_char = m.group(1), m.group(2)
        target = _WEEKDAY_CN.get(day_char)
        if target is not None:
            delta = (target - now.weekday()) % 7
            if prefix:
                delta += 7 * len(prefix)
            elif delta == 0:
                delta = 0
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
    hour = _apply_period(hour, t)

    day = parse_date_hint(t, now)
    explicit_day = day is not None
    if day is None:
        day = now.date()

    target = datetime.combine(day, time(hour, minute))
    if roll_forward and not explicit_day and target <= now:
        target += timedelta(days=1)
    return target


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
