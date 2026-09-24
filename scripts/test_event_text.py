"""文本层（voice_loop/event_text.py）的离线测试。

要守住的底线：
1. **一句话 → 事件字段**：时间、周期、地点、时长、提前量、截止日期都要对得上；
2. ★闹钟与日程的分界不再是「有没有重复」★——而是「是不是一件占时间的事」，
   所以「每个工作日八点半叫我起床」必须是**带周期的提醒**（旧代码会把重复踢给日程，
   然后因为日程不认「工作日」而退化成一次性闹钟）；
3. ★原因从句要剥掉★：「我工作日九点有课，那就需要定工作日八点半的闹钟」里有两个钟点，
   不剥就会把「九点」当成闹钟时间；
4. ★提前量不是时长★：「提前半小时」不该变成 30 分钟的会议；
5. 播报文案分两套口径（闹钟说「时间到了」、日程说「有…」），但共用同一个函数。

    python scripts/test_event_text.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import event_text as et  # noqa: E402

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


NOW = datetime(2026, 9, 18, 12, 0)      # 2026-09-18 是星期五 中午


def fields(text: str, now: datetime = NOW) -> dict:
    return et.extract(text, now)


# --------------------------------------------------------- 1 分界规则
def test_classify() -> None:
    print("\n[1] 闹钟还是日程：看「是不是一件占时间的事」")
    check(et.classify("工作日早上八点半叫我起床"), ("reminder", ""), "只有「叫我」→ 提醒")
    check(et.classify("每天早上七点提醒我吃药"), ("reminder", ""), "吃药 → 提醒")
    check(et.classify("每周三上午九点提醒我上课"), ("event", "course"), "上课 → 日程（课程）")
    check(et.classify("明天下午三点跟导师见面"), ("event", "meeting"), "见面 → 日程（会议）")
    check(et.classify("后天下午两点的体检"), ("event", "activity"), "体检 → 日程（活动）")
    check(et.classify("周五交作业"), ("event", "task"), "作业 → 日程（任务）")
    check(et.classify("提醒我在图书馆还书"), ("event", ""), "有地点 → 日程（但不瞎猜类别）")
    check(et.classify("周三上午九点到十一点开组会"), ("event", "meeting"), "有区间+会议")
    check(et.classify("十分钟后提醒我喝水"), ("reminder", ""), "相对时间 → 提醒")

    check_true(et.looks_like_block("在腾讯会议面试"), "线上地点也算地点")
    check(et.category_of("AIAA3102 机器学习"), "", "认不出类别就留空，不猜成 task")


# --------------------------------------------------------- 2 时间与周期
def test_time() -> None:
    print("\n[2] 时间 / 周期")
    f = fields("十分钟后提醒我喝水")
    check(f["start"].strftime("%H:%M"), "12:10", "「十分钟后」= 现在 + 10 分钟")
    check(f["repeat"], "once", "没写重复 → 一次性")
    check(f["remind_before"], [0], "闹钟默认到点提醒")

    f = fields("每天早上七点提醒我吃药")
    check((f["kind"], f["repeat"], f["time"]), ("reminder", "daily", "07:00"), "每天 = 固定钟点")
    check(f["start"].strftime("%m-%d %H:%M"), "09-19 07:00", "今天七点已经过了 → 明天")

    f = fields("每天早上七点提醒我吃药，到12月底为止")
    check(str(f["until"]), "2026-12-31", "「到…为止」→ until")

    f = fields("每天提醒我吃药")
    check(f["time"], "09:00", "循环但没说钟点 → 默认 09:00（不能用「现在」，会跟着说话时刻漂）")

    f = fields("每周交周报")
    check((f["repeat"], f["weekday"]), ("weekly", 4), "「每周」没写星期几 → 按说话这天（周五）")

    f = fields("每周三上午九点提醒我上课")
    check((f["repeat"], f["weekday"], f["time"]), ("weekly", 2, "09:00"), "每周三")

    f = fields("每两周周三下午两点开组会")
    check((f["repeat"], f["weekday"]), ("biweekly", 2), "每两周")

    f = fields("每月5号交房租")
    check((f["repeat"], f["day"]), ("monthly", 5), "每月 5 号")

    f = fields("每3天浇一次花")
    check((f["repeat"], f["every_days"]), ("interval", 3), "每 3 天")

    f = fields("明天下午三点到四点半跟导师见面")
    check(f["duration_minutes"], 90, "3 点到 4 点半 = 90 分钟")
    check(f["start"].strftime("%m-%d %H:%M"), "09-19 15:00", "明天下午三点")


# --------------------------------------------------------- 3 用户的真实场景
def test_recurring_reminder() -> None:
    print("\n[3] ★闹钟也能重复★（旧设计做不到的那件事）")
    f = fields("工作日早上八点半叫我起床")
    check((f["kind"], f["repeat"], f["time"]), ("reminder", "weekdays", "08:30"), "工作日 8:30 的闹钟")
    check(f["title"], "起床", "标题只留事情本身")
    check(f["remind_before"], [0], "到点提醒")
    item = et.to_item(f)
    check(item["kind"], "reminder", "落库是 reminder")
    check(item["repeat"], "weekdays", "★重复真的写进去了★")
    check(et.render_added(item, f["start"], NOW),
          "好，已记下：每个工作日 08:30，起床。", "确认话术")

    # 原因从句里有两个钟点，必须只用要求那半句的
    f = fields("我工作日早上9点有课，那就需要定所有工作日早上八点半的闹钟")
    check(f["time"], "08:30", "★取「闹钟」那半句的钟点，不是「有课」的九点★")
    check(f["repeat"], "weekdays", "周期还是工作日")
    check(f["title"], "上课", "没说清干什么 → 用原因从句的「有课」兜底")

    f = fields("每周三上午九点提醒我上课，提前半小时")
    check(f["remind_before"], [30], "提前半小时 → 提前量")
    check(f["duration_minutes"], 0, "★提前量不能变成时长★")
    check(f["start"].strftime("%m-%d %H:%M"), "09-23 09:00", "锚点落在下一个周三")


# --------------------------------------------------------- 4 地点 / 备注 / 标题
def test_extra_fields() -> None:
    print("\n[4] 地点 / 备注 / 标题")
    f = fields("每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302")
    check(f["location"], "教学楼 A302", "写「地点」就取到")
    check(f["title"], "AIAA3102机器学习", "标题去掉时间与空格")
    check((f["repeat"], f["weekday"]), ("weekly", 2), "每周三")

    f = fields("提醒我在腾讯会议面试")
    check(f["location"], "腾讯会议", "没写「地点」也认得出线上地点")

    f = fields("明天下午三点开会，备注带上实验报告")
    check(f["note"], "带上实验报告", "「备注」取到")

    f = fields("把后天下午两点的体检记上")
    check((f["title"], f["location"]), ("体检", ""), "「把…记上」不该进标题")

    f = fields("工作日早上八点半的闹钟")
    check(f["title"], "", "没说干什么 → 标题留空，让 handler 去追问")


# --------------------------------------------------------- 5 播报口径
def test_render() -> None:
    print("\n[5] 播报文案（同一个函数，两套口径）")
    start = datetime(2026, 9, 21, 8, 30)
    reminder = et.to_item(fields("工作日早上八点半叫我起床"))
    check(et.render_fire(reminder, start, start, 0), "时间到了，起床。", "闹钟到点")

    event = et.to_item(fields("每周一上午十点开组会，地点教学楼 A302"))
    now = datetime(2026, 9, 21, 9, 50)
    check(et.render_fire(event, datetime(2026, 9, 21, 10, 0), now, 10),
          "提醒你：十分钟后，也就是10:00，有开组会，地点教学楼 A302。",
          "日程提前 10 分钟")
    check(et.render_fire(event, datetime(2026, 9, 21, 10, 0), datetime(2026, 9, 21, 10, 0), 0),
          "提醒你：现在就是10:00，有开组会，地点教学楼 A302。", "日程到点")

    check(et.leads_text([1440, 30]), "我会提前一天和提前三十分钟提醒你。", "多个提前量说人话")
    check(et.leads_text([0]), "我会到点提醒你。", "只有到点")
    check(et.when_text(reminder), "每个工作日 08:30", "周期说人话")
    check(et.norm_title(" 机器学习_1 "), "机器学习1", "标题归一化")


def main() -> int:
    test_classify()
    test_time()
    test_recurring_reminder()
    test_extra_fields()
    test_render()
    print(f"\n{'=' * 60}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
