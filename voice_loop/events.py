"""统一的事件存储：闹钟、日程、事件链都是**同一种东西**。

## 为什么合并

原来有两份文件、两套字段、两套处理器：

    data/alarms.json    {when, what, fired}                       ← 一次性响一声
    data/schedule.json  {title, repeat, start, time, remind_before, skip, _fired, …}

它们的区别其实只有三处**属性**，不是三种东西：

| 维度 | 闹钟 | 日程 |
|---|---|---|
| 时间 | 一个时刻 | 一个时刻 +（可选）重复规则 |
| 提醒 | 到点响（= 提前量 0） | 可配多个提前量 `[1440, 30, 0]` |
| 去重 | `fired` 一维 | `(发生时间, 提前量)` 二维 |
| 操作 | 只能按序号/时间取消 | 能改、删、跳过一次、按名字找 |

所以现在只有一种 **事件**：

```jsonc
{
  "id": 7,
  "kind": "event",                  // reminder（叫一声）| event（要占时间的事）
  "title": "组会",
  "category": "meeting",            // 可选：course / meeting / task，只影响文案
  "start": "2026-09-23 15:30:00",   // 一次性时刻（秒级——「十分钟后」也是它）
  "repeat": "weekly",               // 缺省 = once（= 原来的闹钟）
  "weekday": 2, "day": 5, "month": 3, "every_days": 3, "every_minutes": 30,
  "time": "09:00",                  // 重复时的钟点
  "duration_minutes": 0,            // 0 = 不占时间
  "location": "", "note": "",
  "remind_before": [0],            // 提前量数组；闹钟就是 [0]
  "until": "",                      // 截止日期
  "chain": {"after": 3, "on": "done", "then": "notify"},   // 事件链（可选）
  "state": {"fired": [], "done": [], "skipped": []},
  "created_at": "2026-09-23 15:00:00"
}
```

**「闹钟」= `repeat` 缺省 + `remind_before: [0]` 的 reminder**，
所以旧的两个文件都能一对一映射过来（见 :func:`migrate_legacy`），
而「提醒」「日程」「事件链」共用同一个到期引擎 :func:`due`。

## 三种状态分开记（这是统一的代价，也是收益）

    state.fired    已播报过的「(发生时间|提前量)」——二维去重，最多留 40 条
    state.done     已完成的「发生时间」——事件链要靠它（「A 完成后再做 B」）
    state.skipped  被单独跳过的「日期」——「下周三的课不上了」

分开记的原因：一个重复事件每发生一次都要重新播报（fired 是**每次**的），
但「这一次来没来过」是**条目级**的事实（done/skipped 是**每次**的，但语义不同）。
混在一个布尔里就会出现「今天的课完成过 → 下周的课不再提醒」这种 bug。

## 兼容窗口

`view(kind)` 返回一个**只按 kind 过滤、不改字段名**的视图，`skills.alarms` / `skills.schedule`
现在还挂在它上面。它**不做任何字段翻译**——因为统一后的 schema 本来就用
`title` / `start` / `repeat` / `remind_before` 这些旧日程字段名（旧日程那套名字就很好），
所以唯一变的是「闹钟」那三个名字：`when → start`、`what → title`、`fired → state.fired`。
新代码一律直接用 `EventStore`，视图只给旧调用点过渡。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .store import JsonStore

# ----------------------------------------------------------------- 常量与归一
REPEATS = ("once", "weekly", "biweekly", "monthly", "yearly", "interval")
KINDS = ("reminder", "event")
DEFAULT_LEAD = 10          # 没有 remind_before 时的默认提前量（分钟）
FIRED_KEEP = 40            # state.fired 最多留多少条
LEAD_GRACE = timedelta(minutes=5)   # 「到点那一次」允许迟到多久还算数

_REPEAT_ALIASES = {
    "每周": "weekly", "周": "weekly", "星期": "weekly", "每周重复": "weekly",
    "每两周": "biweekly", "双周": "biweekly", "两周一": "biweekly",
    "每月": "monthly", "每个月": "monthly", "月": "monthly",
    "每年": "yearly", "年": "yearly", "每年重复": "yearly",
    "间隔": "interval", "after_end": "interval", "每天": "interval",
    "一次": "once", "一次性": "once", "none": "once", "": "once",
}


def repeat_of(item: dict) -> str:
    """把各种写法的 repeat 归一成 once / weekly / biweekly / monthly / yearly / interval。"""
    raw = str(item.get("repeat", "once")).strip().lower()
    if raw in REPEATS:
        return raw
    return _REPEAT_ALIASES.get(raw, "once")


def kind_of(item: dict) -> str:
    """``reminder`` = 叫一声；``event`` = 要占时间的事。认不出来就当 event。"""
    raw = str(item.get("kind", "")).strip().lower()
    if raw in KINDS:
        return raw
    return "reminder" if raw == "alarm" else "event"


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("/", "-"))
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    dt = parse_dt(value)
    return dt.date() if dt else None


def anchor_of(item: dict) -> datetime | None:
    return parse_dt(item.get("start") or item.get("when"))


def until_of(item: dict) -> date | None:
    return parse_date(item.get("until"))


def leads_of(item: dict, default: int = DEFAULT_LEAD) -> list[int]:
    """提前提醒的分钟数组（0 = 到点）。兼容旧数据的整数写法。"""
    raw = item.get("remind_before", item.get("notify"))
    if raw is None:
        raw = default
    if isinstance(raw, (int, float, str)):
        try:
            return [max(0, int(raw))]
        except (TypeError, ValueError):
            return [default]
    out: list[int] = []
    for v in raw:
        try:
            out.append(max(0, int(v)))
        except (TypeError, ValueError):
            continue
    return sorted(set(out), reverse=True) or [default]


def parse_hhmm(value: Any) -> tuple[int, int]:
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        return 9, 0


def interval_delta(item: dict) -> timedelta:
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


def state_of(item: dict) -> dict:
    """保证 item 里有 state 这个 dict（旧数据没有就现建）。"""
    st = item.get("state")
    if not isinstance(st, dict):
        st = {}
        item["state"] = st
    for key in ("fired", "done", "skipped"):
        if not isinstance(st.get(key), list):
            st[key] = list(st.get(key) or [])
    return st


# ----------------------------------------------------------------- 发生时间
def occurrences(item: dict, since: datetime, limit: int = 16) -> list[datetime]:
    """``since``（含）往后最多 ``limit`` 个开始时间；跳过 skip 里的日子、到 until 为止。

    这是**唯一的**发生时间引擎——闹钟（once）和日程（weekly/interval/…）走同一条路。
    """
    rep = repeat_of(item)
    skipped = {str(d) for d in (state_of(item).get("skipped") or [])}
    until = until_of(item)
    anchor = anchor_of(item)
    hh, mm = parse_hhmm(item.get("time", "09:00"))
    out: list[datetime] = []

    def usable(cand: datetime) -> bool:
        if until is not None and cand.date() > until:
            return False
        return str(cand.date()) not in skipped

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
            if cand < since:
                continue
            if until is not None and cand.date() > until:
                break
            if not usable(cand):
                continue
            out.append(cand)
        return out

    if rep == "monthly":
        day = int(item.get("day", anchor.day if anchor else since.day))
        year, month = since.year, since.month
        k = 0
        while len(out) < limit and k <= limit + 24:
            # 「31 号」这种在小月里没有 → 回退到月末
            last = _days_in_month(year, month)
            cand = datetime(year, month, min(day, last), hh, mm)
            k += 1
            month += 1
            if month > 12:
                month, year = 1, year + 1
            if cand < since:
                continue
            if until is not None and cand.date() > until:
                break
            if not usable(cand):
                continue
            out.append(cand)
        return out

    if rep == "yearly":
        month = int(item.get("month", anchor.month if anchor else since.month))
        day = int(item.get("day", anchor.day if anchor else since.day))
        year = since.year
        k = 0
        while len(out) < limit and k <= limit + 4:
            last = _days_in_month(year, month)
            cand = datetime(year, month, min(day, last), hh, mm)
            k += 1
            year += 1
            if cand < since:
                continue
            if until is not None and cand.date() > until:
                break
            if not usable(cand):
                continue
            out.append(cand)
        return out

    # once / interval
    base = anchor or since
    if rep != "interval":
        if base >= since and usable(base):
            out.append(base)
        return out
    period = interval_delta(item)
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


def first_after(item: dict, since: datetime) -> datetime | None:
    """下一次开始时间（严格晚于 ``since``）——调度器和「下一项」用它。"""
    for cand in occurrences(item, since - timedelta(seconds=1)):
        if cand > since:
            return cand
    return None


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def fired_key(start: datetime, lead: int) -> str:
    """二维去重键：同一次发生配上同一个提前量只播报一次。"""
    return f"{start.isoformat()}|{int(lead)}"


def is_fired(item: dict, start: datetime, lead: int) -> bool:
    return fired_key(start, lead) in {str(k) for k in state_of(item).get("fired") or []}


def mark_fired(item: dict, start: datetime, lead: int) -> str:
    st = state_of(item)
    key = fired_key(start, lead)
    keys = {str(k) for k in st["fired"]}
    keys.add(key)
    st["fired"] = sorted(keys)[-FIRED_KEEP:]
    return key


def mark_done(item: dict, start: datetime | None = None) -> None:
    """**手动**标记某一次（或整条）提前完成——是链的时间判定的**覆盖**，不是必要条件。

    默认的完成判定是**纯时间推导**（见 :func:`end_of` / :func:`chain_blocked`）：
    「到 A 的结束时刻就算完成」，所以不需要用户开口说「做完了」。
    这个函数留着给两种场景：① 提前完成了（早于结束时刻）；② 事后补记。
    """
    st = state_of(item)
    key = start.isoformat() if start else "*"
    done = {str(k) for k in st["done"]}
    done.add(key)
    st["done"] = sorted(done)[-FIRED_KEEP:]


def is_done(item: dict, start: datetime | None = None) -> bool:
    """有没有被**手动**标记过完成（不看时间）。整条标记过（"*"）也算。"""
    done = {str(k) for k in state_of(item).get("done") or []}
    return "*" in done or (start is not None and start.isoformat() in done)


def end_of(item: dict, start: datetime) -> datetime:
    """一次发生的**结束时刻** = 开始 + ``duration_minutes``。

    ★没有时长就等于开始时刻★（「没有时长的 A」= 开始即结束），
    所以那种情况下 ``on: done`` 和 ``on: start`` 是一回事。
    """
    minutes = max(0, int(item.get("duration_minutes", 0) or 0))
    return start + timedelta(minutes=minutes)


def _occurrence_before(item: dict, at: datetime) -> datetime | None:
    """``at`` 之前（含）最近的一次发生；没有就返回 None。"""
    best: datetime | None = None
    for cand in occurrences(item, at - timedelta(days=400), limit=8):
        if cand <= at and (best is None or cand > best):
            best = cand
    return best


def chain_blocked(item: dict, start: datetime, all_items: list[dict], now: datetime) -> bool:
    """这次发生该不该被链挡住（上游还没达到条件）。**纯时间推导，不写状态**。

    语义（2026-09-24 与用户确认）：

    * ``then`` 固定是 **notify**——**解锁提醒**：B 到点该不该提醒，看上游满足没有；
      而不是「把 B 也标记成做完」。
    * ``on: done``（默认）：上游那次发生**的结束时刻已经过去**就解锁；
      ``on: start``：上游那次发生**已经开始**就解锁。
    * 按**每一次发生**判定：重复的链（「每周三组会结束后提醒我写周报」）
      要求每个 B 之前有一次已结束的 A——这样不会「第一次解锁后永远解锁」。
    * 上游被删掉 → 不再挡（否则下游永远不响，这是链最容易埋的死锁）。
    * ``state.done`` 里手动标记过算提前完成（可选的覆盖）。
    """
    chain = item.get("chain")
    if not isinstance(chain, dict):
        return False
    up = next((it for it in all_items if str(it.get("id")) == str(chain.get("after"))), None)
    if up is None:
        return False
    if is_done(up) or is_done(up, _occurrence_before(up, now)):
        return False
    prev = _occurrence_before(up, now)
    if prev is None:
        return True                    # 上游还没发生过 → 挡
    mark = prev if str(chain.get("on", "done")) == "start" else end_of(up, prev)
    return not (mark <= now)            # 上游那一刻还没到 → 挡


def chain_targets_of(items: Iterable[dict], upstream: dict, on: str) -> list[dict]:
    """下游里哪些挂在 ``upstream`` 上、且 ``on`` 相同的（报告/调试用）。"""
    uid = upstream.get("id")
    out = []
    for item in items:
        chain = item.get("chain")
        if not isinstance(chain, dict):
            continue
        if str(chain.get("after")) != str(uid) or str(chain.get("on", "done")) != on:
            continue
        out.append(item)
    return out


def blocked_ids(items: list[dict], now: datetime) -> set[str]:
    """当前被链挡住的「下一次发生」属于哪些事件——给报告用（`due()` 内部按每一次算）。"""
    out: set[str] = set()
    for item in items:
        if not isinstance(item.get("chain"), dict):
            continue
        nxt = first_after(item, now - timedelta(seconds=1)) or _occurrence_before(item, now)
        if nxt is not None and chain_blocked(item, nxt, items, now):
            out.add(str(item.get("id")))
    return out


# ----------------------------------------------------------------- 到期引擎
def due(
    items: list[dict],
    now: datetime,
    default_lead: int = DEFAULT_LEAD,
) -> list[tuple[dict, datetime, int]]:
    """到点的 (事件, 发生时间, 提前量)；**会就地写回 state.fired**。

    两种事件共用这一条路径（都是「(发生时间, 提前量) 到点了没播过」），
    只有两处策略不同，而且都是故意的：

    * ``reminder``（闹钟）：一次性闹钟**没有回看上限**——机器关机、服务没跑时错过的，
      回来以后仍然该提醒你（原来 ``due_alarms`` 就是这样）；
    * ``event``（日程）：提前量为 0 的那次只在 :data:`LEAD_GRACE` 内算数——
      三天前那节课的「开始了」现在再播一遍毫无意义（原来 ``due_schedule`` 就是这样）。

    ★看的时间窗是 ``now + 最大提前量`` 而不是 ``now``★：提前 30 分钟的提醒
    必须在「开始时间之前」就发出来，只盯着已经过去的时刻是永远发不出来的。

    事件链在**每一次发生**上现算（:func:`chain_blocked`）：上游没结束之前，
    这一次不播报——但**不写任何状态**，所以改上游时间/删上游都能立刻反应。
    """
    out: list[tuple[dict, datetime, int]] = []
    for item in items:
        kind = kind_of(item)
        leads = leads_of(item, default_lead)
        max_lead = max(leads) if leads else 0
        if kind == "reminder" and repeat_of(item) == "once":
            anchor = anchor_of(item)
            candidates = [anchor] if anchor is not None else []
        else:
            lookback = LEAD_GRACE if kind == "event" else timedelta(days=1)
            candidates = occurrences(item, now - timedelta(minutes=max_lead) - lookback, limit=4)
        horizon = now + timedelta(minutes=max_lead)
        for start in candidates:
            if start is None or start > horizon:
                continue
            for lead in leads:
                fire_at = start - timedelta(minutes=lead)
                if fire_at > now:
                    continue
                if kind == "event" and lead == 0 and now >= start + LEAD_GRACE:
                    continue   # 迟太久的那次不算（见上面的策略说明）
                if is_fired(item, start, lead):
                    continue
                if chain_blocked(item, start, items, now):
                    continue   # 上游还没到（链门）
                mark_fired(item, start, lead)
                out.append((item, start, lead))
    return out


# ----------------------------------------------------------------- 链的报告
# 真正的链判定在 :func:`chain_blocked`（在上面，按每一次发生现算）；
# 这里只剩「给人和日志看」的汇总。


# ----------------------------------------------------------------- 存储
class EventStore:
    """一个 JSON 文件装全部事件；外面按需取视图。"""

    def __init__(self, path: str | Path, default_lead: int = DEFAULT_LEAD) -> None:
        self.default_lead = int(default_lead)
        self._store = JsonStore(Path(path), default=[])

    @property
    def path(self) -> Path:
        return self._store.path

    # 透传基本操作（id/created_at 由 JsonStore.append 负责）
    def load(self, force: bool = False) -> list[dict]:
        return self._store.load(force=force)

    def save(self, items: list[dict]) -> None:
        self._store.save(items)

    def append(self, item: dict) -> dict:
        return self._store.append(self.normalize(item))

    def update(self, index: int, **fields: Any) -> dict | None:
        return self._store.update(index, **fields)

    def remove_at(self, index: int) -> dict | None:
        return self._store.remove_at(index)

    def remove_where(self, predicate) -> list[dict]:
        return self._store.remove_where(predicate)

    def clear(self) -> int:
        return self._store.clear()

    def find(self, predicate) -> list[tuple[int, dict]]:
        return self._store.find(predicate)

    def next_id(self) -> int:
        return max([int(it.get("id") or 0) for it in self.load()] or [0]) + 1

    def by_id(self, eid: Any) -> dict | None:
        return next((it for it in self.load() if str(it.get("id")) == str(eid)), None)

    def by_title(self, title: str) -> list[dict]:
        return [it for it in self.load() if str(it.get("title", "")) == title]

    # ------------------------------------------------------------ 字段归一
    def normalize(self, item: dict) -> dict:
        """补齐默认值、把可省的字段去掉（写进文件的要干净）。"""
        out = {k: v for k, v in item.items() if v not in (None, "", [], {})}
        out["kind"] = kind_of(item)
        st = state_of(item)
        if any(st.values()):
            out["state"] = {k: v for k, v in st.items() if v}
        else:
            out.pop("state", None)
        if "title" not in out and item.get("what"):
            out["title"] = item["what"]          # 旧闹钟字段
        if "start" not in out and item.get("when"):
            out["start"] = item["when"]
        out.setdefault("remind_before", [0] if out["kind"] == "reminder" else [self.default_lead])
        return out

    # -------------------------------------------------------------- 视图
    def view(self, kind: str) -> "KindView":
        """只看某一类事件（``reminder`` / ``event``）的视图，**不改字段名**。"""
        return KindView(self, kind)

    def reminders(self) -> "KindView":
        return KindView(self, "reminder")

    def schedules(self) -> "KindView":
        return KindView(self, "event")

    # ------------------------------------------------------------ 到期封装
    def due_now(self, now: datetime) -> list[tuple[dict, datetime, int]]:
        items = self.load()
        got = due(items, now, self.default_lead)
        if got:
            self.save(items)      # due() 已经就地写回了 state.fired
        return got

    def pending_context(self, now: datetime | None = None) -> dict[str, Any]:
        """给「A 结束之后才提醒 B」类问题做报告：谁被挡着、为什么。"""
        items = self.load()
        now = now or datetime.now()
        return {
            "items": items,
            "blocked": blocked_ids(items, now),
            "chains": [it for it in items if isinstance(it.get("chain"), dict)],
        }


# ----------------------------------------------------------------- 迁移
def migrate_legacy(alarm_file: Path, schedule_file: Path) -> tuple[list[dict], dict[str, int]]:
    """把旧的两个文件读成一套事件。返回 (items, 统计)。

    想迁完再去重、而且带备份：落在哪、怎么写由调用方决定（见 scripts/migrate_events.py）。
    映射规则很简单，因为新 schema 本来就是旧 schedule 的超集：

        alarms.json   {when, what, fired}
            → {kind: reminder, title: what, start: when, remind_before: [0], state.fired: […]}
        schedule.json {title, kind: course/meeting, _fired, skip, …}
            → {kind: event, category: <原 kind>, …, state: {fired, skipped}}
    """
    items: list[dict] = []
    stats = {"alarm": 0, "schedule": 0, "fired": 0, "skipped": 0}

    def read(path: Path) -> list[dict]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get(JsonStore.ITEMS_KEY, [])
        return [x for x in raw if isinstance(x, dict)]

    for old in read(alarm_file):
        when = str(old.get("when") or "").strip()
        if not when:
            continue
        item: dict = {
            "kind": "reminder",
            "title": str(old.get("what") or old.get("title") or "提醒"),
            "start": when,
            "remind_before": [0],
        }
        for key in ("id", "created_at", "_note"):
            if old.get(key) is not None:
                item[key] = old[key]
        st: dict[str, list[str]] = {}
        if old.get("fired"):
            # 旧的一维 fired → 新的二维键（提前量 0 就是那次「到点」）
            dt = parse_dt(when)
            if dt is not None:
                st["fired"] = [fired_key(dt, 0)]
        if old.get("fired_at"):
            item["_fired_at"] = old["fired_at"]
        if st:
            item["state"] = st
        items.append(item)
        stats["alarm"] += 1
        stats["fired"] += 1 if old.get("fired") else 0

    for old in read(schedule_file):
        title = str(old.get("title") or "").strip()
        if not title:
            continue
        item = {k: v for k, v in old.items() if not k.startswith("_") or k == "_note"}
        item["kind"] = "event"
        cat = str(old.get("kind") or "").strip()
        if cat in ("course", "meeting", "task"):
            item["category"] = cat
        item.pop("kind_original", None)
        st = {}
        if old.get("_fired"):
            st["fired"] = [str(x) for x in old["_fired"]]
        if old.get("skip"):
            st["skipped"] = [str(x) for x in old["skip"]]
        item.pop("_fired", None)
        item.pop("skip", None)
        if st:
            item["state"] = st
        items.append(item)
        stats["schedule"] += 1
        stats["fired"] += len(st.get("fired") or [])
        stats["skipped"] += len(st.get("skipped") or [])

    # 重排 id：两个文件各自从 1 开始，合起来会撞
    for new_id, item in enumerate(items, start=1):
        item["id"] = new_id
    return items, stats


class KindView:
    """只看某一类事件的视图——**只过滤，不改字段名**。

    `skills.alarms` / `skills.schedule` 现在还挂在它上面，让旧调用点与那 170 多处断言
    继续可用，同时保证库里只有一份数据。三条规矩：

    * ``load()`` 返回**库里的原对象**（不是副本）——旧代码有「就地改字段再 save」的写法，
      返回副本会把改动弄丢；
    * ``save(items)`` 只替换**这一类**，另一类原样保留（否则闹钟一保存就把日程洗掉）；
    * ``append`` 强制把 kind 写成这一类（调用方不用记得写）。
    """

    def __init__(self, store: EventStore, kind: str) -> None:
        self.store = store
        self.kind = kind

    @property
    def path(self) -> Path:
        return self.store.path

    def _mine(self, item: dict) -> bool:
        return kind_of(item) == self.kind

    def load(self, force: bool = False) -> list[dict]:
        return [it for it in self.store.load(force=force) if self._mine(it)]

    def save(self, items: list[dict]) -> None:
        others = [it for it in self.store.load() if not self._mine(it)]
        mine = []
        for item in items:
            new = dict(item)
            new["kind"] = self.kind
            mine.append(self.store.normalize(new))
        self.store.save(others + mine)

    def append(self, item: dict) -> dict:
        new = dict(item)
        new["kind"] = self.kind
        return self.store.append(new)

    def update(self, index: int, **fields: Any) -> dict | None:
        """index 是「这一类里的第几个」（和旧代码的语义一致）。"""
        mine = self.store.find(self._mine)
        if not (1 <= index <= len(mine)):
            return None
        real_index, _item = mine[index - 1]
        return self.store.update(real_index, **fields)

    def remove_at(self, index: int) -> dict | None:
        mine = self.store.find(self._mine)
        if not (1 <= index <= len(mine)):
            return None
        return self.store.remove_at(mine[index - 1][0])

    def remove_where(self, predicate) -> list[dict]:
        return self.store.remove_where(lambda it: self._mine(it) and predicate(it))

    def clear(self) -> int:
        return len(self.store.remove_where(self._mine))

    def find(self, predicate) -> list[tuple[int, dict]]:
        """返回 (这一类里的序号, 元素)，序号从 1 开始。"""
        return [(i, it) for i, it in enumerate(self.load(), start=1) if predicate(it)]


def new_event(
    title: str,
    *,
    kind: str = "event",
    start: str = "",
    repeat: str = "",
    remind_before: list[int] | None = None,
    duration_minutes: int = 0,
    location: str = "",
    note: str = "",
    category: str = "",
    until: str = "",
    chain: dict | None = None,
    **extra: Any,
) -> dict:
    """建一条事件（只填有用的字段，保持文件干净）。"""
    item: dict = {"kind": kind, "title": title}
    if category:
        item["category"] = category
    if start:
        item["start"] = start
    if repeat and repeat != "once":
        item["repeat"] = repeat
    # ★闹钟是「提前量 0」的事件★：reminder 一律显式写 [0]，event 用调用方给的（空就交给默认）
    if remind_before:
        item["remind_before"] = sorted({int(x) for x in remind_before}, reverse=True)
    elif kind == "reminder":
        item["remind_before"] = [0]
    if duration_minutes:
        item["duration_minutes"] = int(duration_minutes)
    if location:
        item["location"] = location
    if note:
        item["note"] = note
    if until:
        item["until"] = until
    if chain:
        item["chain"] = chain
    item.update(extra)
    return item


def describe(item: dict) -> str:
    """一行人类可读的描述（日志、`main.py skills` 用）。"""
    rep = repeat_of(item)
    start = item.get("start") or ""
    when = "一次性" if rep == "once" else f"重复({rep})"
    extra = f" 链←{item['chain'].get('after')}" if isinstance(item.get("chain"), dict) else ""
    return f"[{item.get('id')}] {when} {start} {item.get('title')}{extra}"


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
