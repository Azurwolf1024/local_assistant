"""文本层（voice_loop/event_text.py）的离线测试。

要守住的底线：
1. ★**没有「闹钟」和「日程」这两个类型了**★——只有一张可填可不填的表：
   说了持续时间/重复规则/提前量就填上，没说就用缺省（`duration_minutes=0`、
   `repeat` 无、`remind_before=[0]`）；
2. ★**纯闹钟**★：可以完全没有内容（标题空），只负责准时响；
3. ★**原因从句要剥掉**★：「我工作日九点有课，那就需要定工作日八点半的闹钟」里有两个钟点，
   不剥就会把「九点」当成闹钟时间；
4. ★**提前量不是时长**★：「提前半小时」不该变成 30 分钟；
5. 播报按**提前量**分口径：准时 →「时间到了，X。」；提前 →「提醒你：十分钟后…」；
6. 端到端：一句话 → 落库 → 到期 → 播报。

    python scripts/test_event_text.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import event_text as et  # noqa: E402
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


NOW = datetime(2026, 9, 18, 12, 0)      # 2026-09-18 是星期五 中午


def fields(text: str, now: datetime = NOW) -> dict:
    return et.extract(text, now)


# --------------------------------------------------------- 1 标签（不是类型）
def test_category() -> None:
    print("\n[1] category 只是标签（course/meeting/task/activity），不是类型")
    check(et.classify("每周三上午九点提醒我上课"), "course", "上课 → course")
    check(et.classify("明天下午三点跟导师见面"), "meeting", "见面 → meeting")
    check(et.classify("后天下午两点的体检"), "activity", "体检 → activity")
    check(et.classify("周五交作业"), "task", "作业 → task")
    check(et.classify("十天后的考试"), "course", "考试 → course")
    check(et.classify("工作日早上八点半叫我起床"), "", "认不出就留空，**不猜成 task**")
    check(et.classify("AIAA3102 机器学习"), "", "认不出就留空")

    # ★同一个句子不会因为「有重复」或「有地点」而变成另一种东西★
    for text in ("每天提醒我吃药", "每天下午三点提醒我吃药", "每天在图书馆提醒我吃药"):
        check(et.extract(text, NOW)["repeat"], "daily", f"「{text}」都是每天，类型不参与判断")
    check(et.extract("提醒我在图书馆还书", NOW)["location"], "图书馆", "有地点就填上")


# --------------------------------------------------------- 2 说了就填、没说就缺省
def test_fields() -> None:
    print("\n[2] 说了就填，没说就缺省")
    f = fields("十分钟后提醒我喝水")
    check(f["start"].strftime("%H:%M"), "12:10", "「十分钟后」= 现在 + 10 分钟")
    check(f["repeat"], "once", "没说重复 → 一次性")
    check(f["remind_before"], [0], "★没说提前 → [0]（准时）★")
    check(f["duration_minutes"], 0, "★没说时长 → 0★")
    check(f["location"], "", "没说地点 → 空")
    check(f["category"], "", "认不出标签 → 空")

    f = fields("每天早上七点提醒我吃药")
    check((f["repeat"], f["time"]), ("daily", "07:00"), "每天 = 固定钟点")
    check(f["start"].strftime("%m-%d %H:%M"), "09-19 07:00", "今天七点已经过了 → 明天")

    f = fields("每天提醒我吃药")
    check(f["time"], "09:00", "循环但没说钟点 → 默认 09:00（不能用「现在」，会跟着说话时刻漂）")

    f = fields("每天早上七点提醒我吃药，到12月底为止")
    check(str(f["until"]), "2026-12-31", "「到…为止」→ until")

    f = fields("每周交周报")
    check((f["repeat"], f["weekday"]), ("weekly", 4), "「每周」没写星期几 → 按说话这天（周五）")

    f = fields("每两周周三下午两点开组会")
    check((f["repeat"], f["weekday"]), ("biweekly", 2), "每两周")

    f = fields("每月5号交房租")
    check((f["repeat"], f["day"]), ("monthly", 5), "每月 5 号")

    f = fields("每3天浇一次花")
    check((f["repeat"], f["every_days"]), ("interval", 3), "每 3 天")

    f = fields("明天下午三点到四点半跟导师见面")
    check(f["duration_minutes"], 90, "3 点到 4 点半 = 90 分钟")
    check(f["start"].strftime("%m-%d %H:%M"), "09-19 15:00", "明天下午三点")

    f = fields("每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302")
    check(f["location"], "教学楼 A302", "写「地点」就取到")
    check(f["title"], "AIAA3102机器学习", "标题去掉时间与空格")

    f = fields("提醒我在腾讯会议面试")
    check(f["location"], "腾讯会议", "没写「地点」也认得出线上地点")

    f = fields("明天下午三点开会，备注带上实验报告")
    check(f["note"], "带上实验报告", "「备注」取到")

    f = fields("把后天下午两点的体检记上")
    check((f["title"], f["location"]), ("体检", ""), "「把…记上」不该进标题")


# --------------------------------------------------------- 3 重复的闹钟（旧设计做不到）
def test_recurring_reminder() -> None:
    print("\n[3] ★闹钟也能重复★（旧设计里这条路径被让位规则堵住了）")
    f = fields("工作日早上八点半叫我起床")
    check((f["repeat"], f["time"]), ("weekdays", "08:30"), "工作日 8:30")
    check(f["title"], "起床", "标题只留事情本身")
    check(f["remind_before"], [0], "准时")
    item = et.to_item(f)
    check(item["repeat"], "weekdays", "★重复真的写进去了★")
    check_true("kind" not in item, "★落库的条目里没有 kind★")
    check(et.render_added(item, f["start"], NOW),
          "好，已记下：每个工作日 08:30，起床。", "确认话术")

    # 原因从句里有两个钟点，必须只用要求那半句的
    f = fields("我工作日早上9点有课，那就需要定所有工作日早上八点半的闹钟")
    check(f["time"], "08:30", "★取「闹钟」那半句的钟点，不是「有课」的九点★")
    check(f["repeat"], "weekdays", "周期还是工作日")
    check(f["title"], "上课", "没说清干什么 → 用原因从句的「有课」兜底")
    check(f["category"], "course", "标签也用得上")

    f = fields("每周三上午九点提醒我上课，提前半小时")
    check(f["remind_before"], [30], "提前半小时 → 提前量")
    check(f["duration_minutes"], 0, "★提前量不能变成时长★")
    check(f["start"].strftime("%m-%d %H:%M"), "09-23 09:00", "锚点落在下一个周三")


# --------------------------------------------------------- 4 纯闹钟
def test_pure_alarm() -> None:
    print("\n[4] ★纯闹钟：没有内容也能用★")
    f = fields("工作日早上八点半的闹钟")
    check(f["title"], "", "没说干什么 → 标题空着（不是报错）")
    check((f["repeat"], f["time"]), ("weekdays", "08:30"), "时间和周期照样认得出")
    item = et.to_item(f)
    check(ev.title_of(item), "", "落库后真的没有标题")
    check(ev.display_title(item), "闹钟", "列表里显示「闹钟」")
    start = datetime(2026, 9, 21, 8, 30)
    check(et.render_fire(item, start, start, 0), "时间到了。", "播报：时间到了。")

    f = fields("一个小时后提醒我")
    check(f["title"], "", "「提醒我」后面没内容 → 纯闹钟")
    check(f["start"].strftime("%H:%M"), "13:00", "时间认得出")

    check(ev.needs_confirm(et.to_item(fields("喝水", NOW))), False, "一次性的删改不用确认")
    check(ev.needs_confirm(et.to_item(fields("每天吃药", NOW))), True, "★重复的删改要确认★")

    # ★周期 / 提前量本身不是内容★（2026-09-24 修）：
    # 以前这几句的标题会是「每个工作日」「提前一天和」「到点」这种残渣，
    # 而且「提前一天和半小时」还会被当成 30 分钟时长。
    for text in ("每个工作日早上八点半提醒我", "到点提醒我",
                 "提前一天和半小时提醒我", "提前30分钟、10分钟和到点提醒我"):
        g = fields(text)
        check(g["title"], "", f"「{text}」没有内容 → 标题空着")
        check(g["duration_minutes"], 0, f"「{text}」提前量不是时长")
    check(fields("提前一天和半小时提醒我")["remind_before"], [1440, 30], "列表写法：两个提前量都认")
    check(fields("提前30分钟、10分钟和到点提醒我")["remind_before"], [30, 10, 0], "枚举写法也认")
    check(fields("提前一天和半小时提醒我吃药")["title"], "吃药", "有内容时照常取内容")
    check(fields("和面")["title"], "和面", "★不能把内容开头的「和」削掉★")


# --------------------------------------------------------- 5 播报
def test_render() -> None:
    print("\n[5] 播报按**提前量**分口径（不是按类型）")
    start = datetime(2026, 9, 21, 8, 30)
    alarm = et.to_item(fields("工作日早上八点半叫我起床"))
    check(et.render_fire(alarm, start, start, 0), "时间到了，起床。", "准时（闹钟口径）")

    event = et.to_item(fields("每周一上午十点开组会，地点教学楼 A302"))
    now = datetime(2026, 9, 21, 9, 50)
    check(et.render_fire(event, datetime(2026, 9, 21, 10, 0), now, 10),
          "提醒你：十分钟后，也就是10:00，开组会，地点教学楼 A302。", "提前 10 分钟")
    ten = datetime(2026, 9, 21, 10, 0)
    check(et.render_fire(event, ten, ten, 0), "时间到了，开组会，地点教学楼 A302。",
          "同一件事准时的口径和闹钟一样")

    check(et.leads_text([1440, 30]), "我会提前一天和提前三十分钟提醒你。", "多个提前量说人话")
    check(et.leads_text([0]), "我会到点提醒你。", "只有准时")
    check(et.when_text(alarm), "每个工作日 08:30", "周期说人话")
    check(et.norm_title(" 机器学习_1 "), "机器学习1", "标题归一化")


# --------------------------------------------------------- 6 端到端
def test_end_to_end() -> None:
    """一句话 → 落库 → 到期 → 播报。在**删掉旧处理器之前**证明新栈是通的。"""
    print("\n[6] 端到端：一句话 → 落库 → 到期 → 播报")
    with tempfile.TemporaryDirectory() as td:
        store = ev.EventStore(Path(td) / "events.json")

        f = et.extract("我工作日早上9点有课，那就需要定所有工作日早上八点半的闹钟", NOW)
        item = store.append(et.to_item(f))
        check(item.get("id"), 1, "落库拿到 id")
        check((item["repeat"], item["time"], item["title"]), ("weekdays", "08:30", "上课"),
              "库里的样子")

        sat = datetime(2026, 9, 19, 8, 30)      # 星期六
        check(store.due_now(sat), [], "★周六 8:30 不响★")
        mon = datetime(2026, 9, 21, 8, 30)      # 星期一
        due = store.due_now(mon)
        check(len(due), 1, "周一 8:30 响一次")
        got_item, got_start, got_lead = due[0]
        check(got_start.strftime("%m-%d %H:%M"), "09-21 08:30", "响的是那一次")
        check(et.render_fire(got_item, got_start, mon, got_lead), "时间到了，上课。", "播报")
        check(store.due_now(mon), [], "同一次不会响第二遍")
        check(len(store.due_now(mon + timedelta(days=1))), 1, "周二照响")

        # ★关机/服务停了三天后回来★：重复事件只认窗口内那次（下次很快就到），
        # 一次性事件补报（错过的那件事还是要提)
        check(store.due_now(mon + timedelta(days=3, hours=1)), [], "重复事件不补报三小时前那次")
        missed = store.append(et.to_item(et.extract("十分钟后提醒我关火", NOW)))
        late = NOW + timedelta(hours=5)                     # 17:00，远远错过了 12:10
        fired = [i["title"] for i, _s, _l in store.due_now(late)]
        check(fired, ["关火"], "★一次性补报，重复的不补★")

        # 提前量：说了才提前
        g = et.extract("每周一上午十点到十一点半开组会，地点教学楼 A302", NOW)
        event = store.append(et.to_item(g))
        check((event["repeat"], event["weekday"], event["duration_minutes"], event["location"]),
              ("weekly", 0, 90, "教学楼 A302"), "日程字段落库")
        check(ev.leads_of(event), [0], "★没说提前 → 准时★")
        check(store.due_now(datetime(2026, 10, 5, 9, 50)), [], "9:50 不响（没要提前）")
        got = store.due_now(datetime(2026, 10, 5, 10, 0))
        check([(i["title"], et.render_fire(i, s, datetime(2026, 10, 5, 10, 0), lead))
               for i, s, lead in got],
              [("开组会", "时间到了，开组会，地点教学楼 A302。")], "10:00 准时响")

        h = et.extract("每周一上午十点开组会，提前10分钟和到点提醒我", NOW)
        item2 = store.append(et.to_item(h))
        check(ev.leads_of(item2), [10, 0], "要了提前就有两段")
        check(len(store.due_now(datetime(2026, 10, 12, 9, 50))), 1, "下一周提前 10 分钟响（只有要了提前那条）")
        check(len(store.due_now(datetime(2026, 10, 12, 10, 0))), 2, "下一周准时响（两条都是准时的）")


def main() -> int:
    test_category()
    test_fields()
    test_recurring_reminder()
    test_pure_alarm()
    test_render()
    test_end_to_end()
    print(f"\n{'=' * 60}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
