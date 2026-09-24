"""统一事件层（voice_loop/events.py）的离线测试。

要守住的底线：
1. **发生时间引擎对三种事件是同一套**：一次性（含秒级）、按周/双周、按月（31 号要回退月末）、
   按年、间隔循环（「结束后 N 天」）、**每天/工作日（固定钟点）**，以及 until / skip 两个刹车；
2. **二维去重**：(发生时间, 提前量) 各播一次，第二次调用不能再响；
3. **两种事件共用到期引擎**，但**故意的策略差异**要保住：闹钟错过多久都会补报，
   日程「到点」那次只在 5 分钟内算数（否则三天前那节课的播报会突然冒出来）；
4. **一个文件两个视图**：闹钟视图保存时不能把日程洗掉，反之亦然（这是合并的最大风险）；
5. 迁移要对得上账：旧 `fired` / `_fired` / `skip` 一个都不能丢，id 不能撞。

    python scripts/test_events.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import events as ev  # noqa: E402

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


MON = datetime(2026, 9, 21, 10, 0)          # 2026-09-21 是星期一
WED = datetime(2026, 9, 23, 8, 0)


# ------------------------------------------------------------------ 1 归一
def test_normalize() -> None:
    print("\n[1] 字段归一（可选字段模型，**没有 kind**）")
    check(ev.repeat_of({"repeat": "每周"}), "weekly", "「每周」→ weekly")
    check(ev.repeat_of({}), "once", "缺省 → once")
    check(ev.repeat_of({"repeat": "banana"}), "once", "认不出来 → once")
    check(ev.has_repeat({"repeat": "weekly"}), True, "有重复")
    check(ev.has_repeat({"repeat": "once"}), False, "一次性不算重复")
    check(ev.needs_confirm({"repeat": "weekdays"}), True, "★重复的删改要确认★")
    check(ev.needs_confirm({"title": "喝水"}), False, "纯闹钟不用确认")

    check(ev.leads_of({}, 10), [10], "没写提醒 → 用调用方给的默认值")
    check(ev.leads_of({}), [0], "★连默认值都不给 → 0（准时，闹钟的本质）★")
    check(ev.leads_of({"remind_before": 30}), [30], "旧写法的整数 → [30]")
    check(ev.leads_of({"remind_before": [0, 30, 30]}), [30, 0], "去重 + 从大到小")
    check(ev.leads_of({"remind_before": []}, 15), [15], "空数组 → 默认值")
    check(ev.leads_of({"remind_before": ["x", 5]}), [5], "坏值跳过")
    check(ev.parse_hhmm("09:30"), (9, 30), "HH:MM")
    check(ev.parse_hhmm("坏"), (9, 0), "解析不了 → 09:00")
    check(ev.title_of({}), "", "★标题可以是空的（纯闹钟）★")
    check(ev.display_title({}), "闹钟", "列表里空的就写「闹钟」")

    check(ev.interval_delta({"every_days": 3, "duration_minutes": 30}),
          timedelta(minutes=30 + 3 * 24 * 60), "间隔 = 结束后 + N 天")
    check(ev.interval_delta({"every_minutes": 10}), timedelta(minutes=10), "按分钟间隔")
    check(ev.interval_delta({}), timedelta(minutes=24 * 60), "缺省每天")


# ------------------------------------------------------------- 2 发生时间
def test_occurrences() -> None:
    print("\n[2] 发生时间（同一种事件、同一套引擎）")
    once = ev.new_event("喝水", start="2026-09-21 10:05:30")
    got = ev.occurrences(once, MON)
    check([d.strftime("%H:%M:%S") for d in got], ["10:05:30"], "一次性保留秒（「十分钟后」靠它）")
    check(ev.occurrences(once, MON + timedelta(hours=1)), [], "已经过去的一次性不再产生发生")

    weekly = ev.new_event("组会", start="2026-09-23 14:00", repeat="weekly", remind_before=[10])
    weekly["weekday"] = 2
    got = ev.occurrences(weekly, MON, limit=3)
    check([d.strftime("%m-%d %H:%M") for d in got],
          ["09-23 14:00", "09-30 14:00", "10-07 14:00"], "每周三")

    weekly["until"] = "2026-09-30"
    check([d.strftime("%m-%d") for d in ev.occurrences(weekly, MON, limit=5)],
          ["09-23", "09-30"], "until 之后不再发生")

    weekly.pop("until")
    ev.mark_done(weekly, datetime(2026, 9, 23, 14, 0))     # 顺带试一下 done 不影响发生
    weekly["state"]["skipped"] = ["2026-09-23"]
    check([d.strftime("%m-%d") for d in ev.occurrences(weekly, MON, limit=3)],
          ["09-30", "10-07", "10-14"], "skip 跳过某一天，但课表留着")

    bi = ev.new_event("双周组会", start="2026-09-23 14:00", repeat="biweekly")
    bi["weekday"] = 2
    check([d.strftime("%m-%d") for d in ev.occurrences(bi, MON, limit=3)],
          ["09-23", "10-07", "10-21"], "双周要有锚点相位（不然会漂）")

    monthly = ev.new_event("对账", start="2026-01-31 21:00", repeat="monthly")
    monthly["day"] = 31
    check([d.strftime("%m-%d") for d in ev.occurrences(monthly, datetime(2026, 1, 1), limit=3)],
          ["01-31", "02-28", "03-31"], "每月 31 号：小月回退到月末（2026 不是闰年）")

    yearly = ev.new_event("开学典礼", start="2026-03-01 09:00", repeat="yearly")
    yearly["month"], yearly["day"] = 3, 1
    check([d.strftime("%Y-%m-%d") for d in ev.occurrences(yearly, datetime(2026, 1, 1), limit=2)],
          ["2026-03-01", "2027-03-01"], "每年")

    interval = ev.new_event("浇花", start="2026-09-21 08:00", repeat="interval")
    interval["every_days"] = 3
    interval["duration_minutes"] = 10
    # since 要早于锚点，否则 08:00 那次会被当成「已经过去」而不列出来
    got = ev.occurrences(interval, datetime(2026, 9, 21, 7, 0), limit=3)
    check([d.strftime("%m-%d %H:%M") for d in got],
          ["09-21 08:00", "09-24 08:10", "09-27 08:20"], "间隔循环：每次从**上次结束**往后算")

    plain = ev.new_event("组会", start="2026-09-23 14:00", repeat="weekly")
    plain["weekday"] = 2
    check(ev.first_after(plain, MON).strftime("%m-%d"), "09-23", "first_after 给调度器用")
    check(ev.first_after(once, MON + timedelta(days=1)), None, "过期的一次性没有下一次")


# ------------------------------------------- 2b 工作日 / 每天（固定钟点）
def test_workdays() -> None:
    """用户的真实场景：「我工作日早上 9 点有课，所以每个工作日 8:30 给我个闹钟」。

    ★这是旧设计明确做不到的★：旧 `_handle_alarm` 碰到 repeat 就丢给日程处理，
    而日程只认「每周X / 每N天」，根本没有「工作日」这个概念。
    """
    print("\n[2b] 工作日 / 每天（固定钟点，不是漂移的 24 小时）")
    WK0 = datetime(2026, 9, 21, 7, 0)      # 星期一 07:00（当天 08:30 还没到）
    check(ev.repeat_of({"repeat": "工作日"}), "weekdays", "「工作日」→ weekdays")
    check(ev.repeat_of({"repeat": "每天"}), "daily", "「每天」→ daily")

    wk = ev.new_event("起床", start="2026-09-18 08:30",
                      repeat="weekdays", remind_before=[0])
    check(ev.repeat_of(wk), "weekdays", "闹钟可以带周期（旧设计里这条路径不通）")
    # 2026-09-18 是星期五；从星期五中午往后看，周末必须跳过
    got = [d.strftime("%m-%d %a") for d in ev.occurrences(wk, datetime(2026, 9, 18, 12, 0), limit=6)]
    check(got, ["09-21 Mon", "09-22 Tue", "09-23 Wed", "09-24 Thu", "09-25 Fri", "09-28 Mon"],
          "★工作日 08:30：周一到周五，跳掉 09-26/27★")
    check(all(d.hour == 8 and d.minute == 30 for d in ev.occurrences(wk, MON, limit=8)),
          True, "每个工作日的时刻都是 08:30")

    from_stale = ev.occurrences(dict(wk, start="2026-08-03 08:30"), WK0, limit=2)
    check([d.strftime("%m-%d") for d in from_stale], ["09-21", "09-22"],
          "★锚点很早以前也不能失控：只从 since 往后发★")

    check(ev.occurrences(wk, MON, limit=1)[0].strftime("%m-%d %H:%M"),
          "09-22 08:30", "★当天那一场已经过了就不再发（不补昨天的）★")

    wk["until"] = "2026-09-24"
    check([d.strftime("%m-%d") for d in ev.occurrences(wk, WK0, limit=9)],
          ["09-21", "09-22", "09-23", "09-24"], "工作日也遵守 until（放寒假了就停）")
    wk.pop("until")
    wk["state"] = {"skipped": ["2026-09-22", "2026-09-23"]}
    # 注意 limit 是「要几个」，不是「看到哪天」；10-01~10-02 是国庆调休也照样照发（以后要接节假日表）
    check([d.strftime("%m-%d") for d in ev.occurrences(wk, WK0, limit=9)],
          ["09-21", "09-24", "09-25", "09-28", "09-29", "09-30", "10-01", "10-02", "10-05"],
          "工作日也遵守 skip（中秋放假一天），周末始终跳掉")

    dy = ev.new_event("吃药", start="2026-09-18 07:00",
                      repeat="daily", remind_before=[0])
    check([d.strftime("%m-%d %H:%M") for d in ev.occurrences(dy, datetime(2026, 9, 18, 12, 0), limit=3)],
          ["09-19 07:00", "09-20 07:00", "09-21 07:00"], "每天 07:00：包括周末")
    check([d.strftime("%m-%d %H:%M") for d in ev.occurrences(dy, datetime(2026, 9, 18, 6, 0), limit=2)],
          ["09-18 07:00", "09-19 07:00"], "当天还没到就含当天")

    check(ev.repeat_text(wk), "每个工作日", "播报说人话")
    check(ev.repeat_text(dy), "每天", "播报说人话（每天）")


# --------------------------------------------------------------- 3 状态
def test_state() -> None:
    print("\n[3] 三种状态分开记")
    item = ev.new_event("吃药", start="2026-09-24 09:00")
    check(ev.is_fired(item, datetime(2026, 9, 24, 9, 0), 10), False, "一开始没播过")
    key = ev.mark_fired(item, datetime(2026, 9, 24, 9, 0), 10)
    check(key, "2026-09-24T09:00:00|10", "去重键 = 发生时间|提前量")
    check(ev.is_fired(item, datetime(2026, 9, 24, 9, 0), 10), True, "播过就记住了")
    check(ev.is_fired(item, datetime(2026, 9, 24, 9, 0), 0), False, "★同一个发生的另一个提前量不受影响★")
    check(ev.is_fired(item, datetime(2026, 10, 1, 9, 0), 10), False, "★下一次发生也不受影响★")

    for i in range(60):                    # 上限：不能无限增长
        ev.mark_fired(item, datetime(2026, 9, 24, 9, 0) + timedelta(minutes=i), 10)
    check(len(item["state"]["fired"]), ev.FIRED_KEEP, f"fired 最多留 {ev.FIRED_KEEP} 条")

    other = ev.new_event("开会", start="2026-09-24 15:00")
    check(ev.is_done(other), False, "默认没完成")
    ev.mark_done(other, datetime(2026, 9, 24, 15, 0))
    check(ev.is_done(other, datetime(2026, 9, 24, 15, 0)), True, "某一次完成")
    check(ev.is_done(other, datetime(2026, 10, 1, 15, 0)), False, "★但下一次不算完成★")
    ev.mark_done(other)
    check(ev.is_done(other, datetime(2026, 10, 1, 15, 0)), True, "整条标记完成 → 每次算完成")


# --------------------------------------------------------------- 4 到期
def test_due() -> None:
    print("\n[4] 到期引擎（一条路径；策略差异按「一次性 / 重复」分，**不按闹钟/日程分**）")
    # 一次性：无回看上限——关机/服务没跑时错过的，回来必须响（这是「准时响起」的兵底）
    gone = ev.new_event("很久以前的闹钟", start="2026-09-18 07:00:00")
    once_meeting = ev.new_event("跟导师见面", start="2026-09-21 10:30", remind_before=[10, 0])
    items = [gone, once_meeting]
    now = datetime(2026, 9, 21, 10, 25)
    got = ev.due(items, now, 10)
    check(sorted(it["title"] for it, _s, _l in got), ["很久以前的闹钟", "跟导师见面"],
          "一次性补报 + 提前量在开始前就发")
    check([lead for it, _s, lead in got if it["title"] == "跟导师见面"], [10],
          "10:25 时先响 10 分钟那次")

    got2 = ev.due(items, now, 10)
    check(got2, [], "★同一个到期的第二次调用不能再响★")
    got3 = ev.due(items, datetime(2026, 9, 21, 10, 30), 10)
    check([it["title"] for it, _s, lead in got3 if lead == 0], ["跟导师见面"], "到点那一次（lead=0）")
    check([it["title"] for it, _s, _l in ev.due([ev.new_event("很久以后的会", start="2026-09-25 09:00")],
                                                now, 10)], [],
          "还没到的不响")

    # 重复：lead=0 那次只在 LEAD_GRACE（5 分钟）内算数——三天前那节课不该现在才播
    weekly = ev.new_event("周三组会", start="2026-09-23 14:00", repeat="weekly")
    weekly["weekday"] = 2
    check([it["title"] for it, _s, _l in ev.due([weekly], datetime(2026, 9, 23, 14, 3), 0)],
          ["周三组会"], "重复事件：3 分钟内赶到还算数")
    late = ev.new_event("周三组会", start="2026-09-23 14:00", repeat="weekly")
    late["weekday"] = 2
    check(ev.due([late], datetime(2026, 9, 23, 14, 10), 0), [],
          "★重复事件迟过 5 分钟就不补——下次很快就到★")
    check(ev.due([weekly], datetime(2026, 9, 23, 14, 20), 0), [], "14:20 也不再补报")

    # ★纯闹钟：没有标题也能响（只负责准时）；播报文案在 test_event_text.py 里验
    pure = ev.new_event(start="2026-09-21 11:00")
    check(len(ev.due([pure], datetime(2026, 9, 21, 11, 0), 0)), 1, "★纯闹钟（无标题）也照响★")


# ---------------------------------------------------------------- 5 事件链
def test_chain() -> None:
    print("\n[5] 事件链（上游没结束 → 下游的提醒被挡住；判定是纯时间的）")
    # A 有 60 分钟时长：10:00 开始 → 11:00 结束（提前量写死 [0]，免得受 default_lead 影响）
    a = ev.new_event("写完报告", start="2026-09-21 10:00", duration_minutes=60, remind_before=[0])
    a["id"] = 1
    # A 的「准时」早就播过了（10:00）——不标的话现在也会补报，会干扰下面的断言
    ev.mark_fired(a, datetime(2026, 9, 21, 10, 0), 0)
    # B 自己定在 10:30（A 还在进行）——★故意让「被挡」只有一个原因：链★
    b = ev.new_event("发邮件给导师", start="2026-09-21 10:30")
    b["id"] = 2
    b["chain"] = {"after": 1, "on": "done", "then": "notify"}
    c = ev.new_event("不相关的闹钟", start="2026-09-21 10:30")
    c["id"] = 3
    items = [a, b, c]

    early, during, after = (datetime(2026, 9, 21, 9, 30),
                            datetime(2026, 9, 21, 10, 30),
                            datetime(2026, 9, 21, 11, 5))

    check(ev.blocked_ids(items, early), {"2"}, "★A 还没开始 → B 被挡，C 不受影响★")
    check(ev.blocked_ids(items, during), {"2"}, "★A 进行中（10:30）也挡着★")
    check(ev.blocked_ids(items, after), set(), "★A 结束（11:00）之后自动解锁——不需要谁来说「做完了」★")

    got = ev.due(items, during, default_lead=10)
    check(sorted(it["title"] for it, _s, _l in got), sorted(["不相关的闹钟"]),
          "★到点了但被链挡住，所以不播报（同时刻的 C 照播）★")
    got = ev.due(items, after, default_lead=10)
    check([it["title"] for it, _s, _l in got], ["发邮件给导师"],
          "★解锁后补播（闹钟没有回看上限）★")

    check(ev.chain_targets_of(items, a, "done"), [b], "A 完成后该唤醒谁（报告用）")
    check(ev.chain_targets_of(items, a, "start"), [], "on=start 的下游不匹配 on=done")

    # 手动标记完成 = 提前解锁（可选覆盖）
    early = ev.new_event("提前做完的报告", start="2026-09-21 10:00", duration_minutes=60)
    early["id"] = 11
    follower = ev.new_event("提醒我发邮件", start="2026-09-21 10:30")
    follower["id"] = 12
    follower["chain"] = {"after": 11, "on": "done"}
    check(ev.blocked_ids([early, follower], during), {"12"}, "默认按时间挡着")
    ev.mark_done(early, datetime(2026, 9, 21, 10, 0))
    check(ev.blocked_ids([early, follower], during), set(), "手动标记完成 → 提前解锁")

    # on=start：上游一开始就解锁
    starter = ev.new_event("上课", start="2026-09-21 09:00")
    starter["id"] = 5
    after_start = ev.new_event("课后交作业", start="2026-09-21 09:00")
    after_start["id"] = 6
    after_start["chain"] = {"after": 5, "on": "start"}
    check(ev.blocked_ids([starter, after_start], datetime(2026, 9, 21, 8, 30)), {"6"},
          "on=start：上游还没开始（8:30）→ 挡")
    check(ev.blocked_ids([starter, after_start], datetime(2026, 9, 21, 9, 5)), set(),
          "on=start：上游一开始（9:00）就解锁")

    # ★重复链要按「每一次发生」算，不能第一次解锁后永远解锁★
    weekly = ev.new_event("周三组会", start="2026-09-23 14:00", repeat="weekly",
                          duration_minutes=60)
    weekly["weekday"] = 2
    weekly["id"] = 21
    report = ev.new_event("写周报", start="2026-09-23 15:30")
    report["id"] = 22
    report["chain"] = {"after": 21, "on": "done"}
    check(ev.blocked_ids([weekly, report], datetime(2026, 9, 23, 14, 30)), {"22"},
          "本周组会还在开（14:30）→ 挡")
    check(ev.blocked_ids([weekly, report], datetime(2026, 9, 23, 15, 5)), set(),
          "本周组会 15:00 结束 → 解锁")
    check(ev.blocked_ids([weekly, report], datetime(2026, 9, 30, 14, 30)), {"22"},
          "★下一周（9-30）同样要等那次结束——不是「第一次解锁后永远解锁」★")
    check(ev.blocked_ids([weekly, report], datetime(2026, 9, 30, 15, 5)), set(),
          "下周 15:00 之后又解锁")

    orphan = ev.new_event("上游被删的", start="2026-09-21 20:00")
    orphan["id"] = 4
    orphan["chain"] = {"after": 99, "on": "done"}
    check(ev.blocked_ids([orphan], during), set(), "★上游被删了就不再挡★（否则永远不响）")

    # 没有时长的 A：开始即结束（on=done 与 on=start 等价）
    nodur = ev.new_event("没写时长的会", start="2026-09-21 12:00")
    nodur["id"] = 31
    nodur["chain"] = {"after": 31, "on": "done"}
    check(ev.end_of(nodur, datetime(2026, 9, 21, 12, 0)), datetime(2026, 9, 21, 12, 0),
          "没有时长 → 结束时刻 = 开始时刻")


# ---------------------------------------------------------------- 6 存储
def test_store() -> None:
    print("\n[6] 存储（一个文件、一种事件、没有视图）")
    with tempfile.TemporaryDirectory() as tmp:
        store = ev.EventStore(Path(tmp) / "events.json")

        a = store.append(ev.new_event("起床", start="2026-09-22 07:00:00"))
        s = store.append(ev.new_event("组会", start="2026-09-23 14:00", repeat="weekly",
                                      remind_before=[30, 0]))
        check(a["id"], 1, "append 自动给 id")
        check_true(bool(a.get("created_at")), "append 自动给 created_at")
        check(store.next_id(), 3, "next_id 取最大值 +1")
        check(store.by_id(2)["title"], "组会", "by_id")
        check([it["title"] for it in store.by_title("组会")], ["组会"], "by_title")
        check(a["remind_before"], [0], "★没写提前量就是 [0]（准时）★")
        check_true("state" not in a, "没有状态时不写空的 state（文件要干净）")
        check_true("kind" not in a and "kind" not in s, "★不再写 kind（不分闹钟/日程）★")

        # 纯闹钟：没有标题也能存
        p = store.append(ev.new_event(start="2026-09-22 07:05:00"))
        check(ev.title_of(p), "", "★纯闹钟：标题可以是空的★")
        check(ev.display_title(p), "闹钟", "显示时给个名字")
        store.remove_at(3)                                        # 删掉那条纯闹钟
        check(len(store.load()), 2, "纯闹钟也能按序号删")

        check(store.update(2, start="2026-09-23 15:00")["start"], "2026-09-23 15:00", "update 按序号")
        check(store.load()[0]["title"], "起床", "update 没有动到别条")
        store.remove_at(1)
        check([it["title"] for it in store.load()], ["组会"], "remove_at 按序号")

        # 就地改字段再 save（旧代码的写法）必须生效
        for it in store.load():
            it["state"] = {"fired": [ev.fired_key(datetime(2026, 9, 23, 15, 0), 0)]}
        store.save(store.load())
        check(bool(store.load()[0].get("state", {}).get("fired")), True,
              "★load() 给的是原对象，就地改完再 save 能写回★")

        # find 给出的是 (1 起的序号, 元素)
        found = store.find(lambda it: ev.has_repeat(it))
        check([i for i, _ in found], [1], "find 返回 (序号, 元素)")
        check(store.remove_where(lambda it: ev.title_of(it) == "不存在"), [], "没命中就不动")
        check(len(store.load()), 1, "剩下的还在")
        check(len(store.remove_where(lambda it: ev.title_of(it) == "组会")), 1, "remove_where 命中一条")
        check(store.load(), [], "删干净了")

        # 文件本身要能被人看懂
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        check(isinstance(raw, list), True, "存的是数组")
        check(all("kind" not in it for it in raw), True, "文件里没有 kind")


# ---------------------------------------------------------------- 7 迁移
def test_migrate() -> None:
    print("\n[7] 迁移旧数据")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        alarms = base / "alarms.json"
        sched = base / "schedule.json"
        alarms.write_text(json.dumps({
            "_说明": "闹钟",
            "items": [
                {"when": "2026-09-23 15:30:00", "what": "约导师见面", "fired": False,
                 "kind": "alarm", "id": 1, "created_at": "2026-09-18 18:48:12", "_note": "双保险"},
                {"when": "2026-09-20 17:00:00", "what": "乐队排练", "fired": True,
                 "kind": "alarm", "id": 2, "fired_at": "2026-09-20 17:17:23"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        sched.write_text(json.dumps({
            "_说明": "日程",
            "items": [
                {"title": "跟导师见面", "kind": "meeting", "repeat": "once",
                 "start": "2026-09-23 15:30", "time": "15:30", "remind_before": [10], "id": 1},
                {"title": "AIAA3102", "kind": "course", "repeat": "weekly", "weekday": 2,
                 "time": "09:00", "duration_minutes": 90, "location": "教学楼 A302",
                 "remind_before": [1440, 30], "id": 2, "skip": ["2026-09-30"],
                 "_fired": ["2026-09-23T09:00:00|30"]},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        items, stats = ev.migrate_legacy(alarms, sched)
        check(stats, {"alarm": 2, "schedule": 2, "fired": 2, "skipped": 1}, "统计对得上")
        check([it["id"] for it in items], [1, 2, 3, 4], "★id 重新编号，两个文件不会再撞★")

        # ★旧文件的 kind 不再带过来；日程的 kind 变成标签 category★
        first = items[0]
        check((first["title"], first["start"], first["remind_before"]),
              ("约导师见面", "2026-09-23 15:30:00", [0]),
              "闹钟 → title/start/[0]（且没有 kind）")
        check_true("kind" not in first, "★迁移后不再有 kind★")
        check(first["_note"], "双保险", "注释字段带过来")
        check(first["created_at"], "2026-09-18 18:48:12", "created_at 带过来")

        second = items[1]
        check(ev.is_fired(second, datetime(2026, 9, 20, 17, 0), 0), True, "★fired=true → 二维键记上★")
        check(second.get("_fired_at"), "2026-09-20 17:17:23", "fired_at 留着当备注")

        course = items[3]
        check(course["category"], "course", "★日程的 kind=course → 标签 category★")
        check_true("kind" not in course, "kind 不再保留")
        check(course["remind_before"], [1440, 30], "多个提前量原样")
        check(course["state"]["skipped"], ["2026-09-30"], "★skip → state.skipped★")
        check(course["state"]["fired"], ["2026-09-23T09:00:00|30"], "★_fired → state.fired★")
        check_true("skip" not in course and "_fired" not in course, "旧的字段名不再留")

        # 迁移结果直接能喂给引擎
        got = ev.occurrences(course, datetime(2026, 9, 28), limit=2)
        check([d.strftime("%m-%d") for d in got], ["10-07", "10-14"], "迁完就能算发生（9-30 被跳过）")


def main() -> int:
    test_normalize()
    test_occurrences()
    test_workdays()
    test_state()
    test_due()
    test_chain()
    test_store()
    test_migrate()
    print(f"\n{'=' * 60}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
