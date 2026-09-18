"""技能路由 + 数据持久化 + 提醒文案自测。

覆盖用户实测日志里暴露出来的四个问题，以及新加的「关屏幕」技能：
    python scripts/test_skills_route.py

不依赖麦克风/ASR/Ollama，也不会真的关掉你的屏幕（monitor_off 被打桩）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import system_ops  # noqa: E402
from voice_loop.scheduler import ReminderScheduler  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.skills import Skills  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# 打桩，避免测试真的把屏幕关掉
_calls: list[str] = []


def _fake_off():
    _calls.append("off")
    return True, ""


def _fake_on():
    _calls.append("on")
    return True, ""


system_ops.monitor_off = _fake_off  # type: ignore[assignment]
system_ops.monitor_on = _fake_on  # type: ignore[assignment]


def make_skills(tmp: Path, **overrides) -> Skills:
    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.alarm_file = str(tmp / "alarms.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.skills.schedule_file = str(tmp / "schedule.json")
    for k, v in overrides.items():
        setattr(settings.skills, k, v)
    return Skills(settings)


def route(skills: Skills, q: str) -> str:
    r = skills.handle(q)
    if r is None:
        return "（未命中 → LLM）"
    return f"[{r.action}] {r.reply}"


# --------------------------------------------------------------------------- #
def test_regressions() -> None:
    print("\n[1] 实测日志里暴露的四个问题")
    now = datetime(2026, 9, 17, 22, 30)  # 周四晚
    tmp = Path(tempfile.mkdtemp(prefix="route_"))
    skills = make_skills(tmp)

    cases = [
        # 备忘查询不能被当成新增
        ("我现在有什么备忘录吗？", "memo_list"),
        ("我的备忘有哪些", "memo_list"),
        # 备忘录这个说法也要能查（旧代码在这里存成了「录吗？」）
        ("我有哪些备忘录", "memo_list"),
        # 「记录晚上7点半有跆拳道课」应该入日程，而不是被当成查询
        ("帮我记录晚上7点半有跆拳道课。", "schedule_add"),
        # 内容在前、动词在后：「…有跆拳道课，到时候记得提醒我」
        ("我说我晚上7点有跆拳道课，到时候记得提醒我", "alarm_add"),
        # 内容不能是命令本身
        ("定一个今天早上8点半的闹钟。", "alarm_add"),
        # 正常的查询/新增仍然要正常
        ("记一下买牛奶", "memo_add"),
        ("今天有什么课", "schedule_query"),
        ("提醒我明天早上七点起床", "alarm_add"),
    ]
    for q, expect in cases:
        r = skills.handle(q)
        action = r.action if r else "（未命中 → LLM）"
        ok = action == expect
        check(f"{q[:22]:<24} → {action}", ok, "" if ok else f"（期望 {expect}）")
        if r:
            print(f"        {r.reply}")

    # 内容是否真的是用户说的那件事
    text = json.dumps(skills.alarms.load(), ensure_ascii=False)
    check("闹钟内容含「跆拳道」", "跆拳道" in text, text[:120])
    text2 = json.dumps(skills.alarms.load(), ensure_ascii=False)
    check("闹钟内容不是命令本身", "定一个" not in text2 and "闹钟" not in text2.replace('"kind"', ""), text2[:120])
    memo = json.dumps(skills.memos.load(), ensure_ascii=False)
    check("备忘只写进了「买牛奶」", "录吗" not in memo, memo[:120])


# --------------------------------------------------------------------------- #
def test_screen_skill() -> None:
    print("\n[2] 关屏幕技能（只是关屏，不是休眠）")
    tmp = Path(tempfile.mkdtemp(prefix="screen_"))
    skills = make_skills(tmp)

    for q in ("关屏幕", "把屏幕关掉", "关闭显示器", "黑屏", "息屏", "帮我关掉屏幕"):
        r = skills.handle(q)
        ok = r is not None and r.action == "screen_off"
        check(f"{q:<12} → {r.action if r else '未命中'}", ok)
    check("确实调用了 monitor_off", _calls.count("off") == 6, str(_calls))

    for q in ("开屏幕", "打开显示器", "亮屏"):
        r = skills.handle(q)
        ok = r is not None and r.action == "screen_on"
        check(f"{q:<12} → {r.action if r else '未命中'}", ok)

    # 不能把普通句子误判成关屏
    for q in ("这个办法屏幕道了", "我看了一下屏幕时间", "关闭闹钟"):
        r = skills.handle(q)
        ok = r is None or r.action != "screen_off"
        check(f"不误判：{q:<16} → {r.action if r else '未命中'}", ok)

    # 开关
    off = make_skills(Path(tempfile.mkdtemp(prefix="screen_off_")), allow_system_commands=False)
    r = off.handle("关屏幕")
    ok = r is not None and "禁用" in r.reply
    check("allow_system_commands=false 时拒绝执行", ok, r.reply if r else "")

    # 系统层实现是否可用（不真的执行）
    check("system_ops.supported()", system_ops.supported() is True)
    print(f"    电源信息：{system_ops.power_info()}")


# --------------------------------------------------------------------------- #
def test_weekly_course() -> None:
    print("\n[3] 每周重复的课表（课程提醒的主场景）")
    tmp = Path(tempfile.mkdtemp(prefix="weekly_"))
    skills = make_skills(tmp)
    skills.schedule.save([])

    r = skills.handle("每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302")
    check("「每周三…」被当成新增", r is not None and r.action == "schedule_add_weekly", r.reply if r else "")
    items = skills.schedule.load()
    check("存成了 weekly", items and items[0].get("repeat") == "weekly", str(items[:1]))
    check("weekday=2（周三）", items and items[0].get("weekday") == 2)
    check("time=09:00", items and items[0].get("time") == "09:00")
    check("事项是课程名", items and "AIAA3102" in str(items[0].get("title")), str(items[:1]))
    check("地点被拆出来", items and items[0].get("location") == "教学楼 A302", str(items[:1]))

    # 「下午两点」必须是 14:00 而不是 02:00
    skills.handle("每周五下午两点组会")
    fri = [it for it in skills.schedule.load() if it.get("weekday") == 4]
    check("「下午两点」= 14:00", fri and fri[0].get("time") == "14:00", str(fri[:1]))

    # 问句不能被当成新增
    before = len(skills.schedule.load())
    q = skills.handle("每周五有什么课")
    check("「每周五有什么课」仍然是查询", q is not None and q.action == "schedule_query", q.reply if q else "")
    check("查询不会写数据", len(skills.schedule.load()) == before)


# --------------------------------------------------------------------------- #
def test_reminder_content() -> None:
    print("\n[4] 提醒文案是否带上了「什么事 / 几点 / 在哪」")
    tmp = Path(tempfile.mkdtemp(prefix="remind_"))
    skills = make_skills(tmp)
    skills.schedule.save(
        [
            {
                "title": "AIAA3102 机器学习",
                "kind": "course",
                "repeat": "weekly",
                "weekday": 2,          # 2 = 周三（0 = 周一）
                "time": "09:00",
                "location": "教学楼 A302",
                "remind_before": 15,
                "duration_minutes": 90,
            }
        ]
    )
    # 周三 08:45 —— 正好在提前 15 分钟的提醒窗口里
    now = datetime(2026, 9, 23, 8, 45, 10)
    out = skills.due_schedule(now)
    check("日程提醒被触发", len(out) == 1, str(out))
    if out:
        text = out[0][1]
        print(f"    {text}")
        for kw in ("AIAA3102", "A302", "09:00", "分钟后"):
            check(f"文案含 {kw}", kw in text, text)
    # 同一条日程当天只提醒一次
    check("同一天不重复提醒", len(skills.due_schedule(now)) == 0)

    # 闹钟文案
    skills.alarms.save(
        [{"when": (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
          "what": "喝水", "fired": False, "kind": "alarm"}]
    )
    spoken: list[str] = []
    sch = ReminderScheduler(skills, skills.settings, spoken.append)
    n = sch.tick(now)
    check("调度器播报了 1 条", n == 1, str(spoken))
    check("闹钟文案含事项", spoken and "喝水" in spoken[0], str(spoken))


# --------------------------------------------------------------------------- #
def test_persistence() -> None:
    print("\n[5] 重启服务会不会丢数据")
    tmp = Path(tempfile.mkdtemp(prefix="persist_"))
    now = datetime(2026, 9, 17, 22, 30)

    a = make_skills(tmp)
    a.handle("提醒我明天早上七点起床")
    a.handle("记一下买牛奶")
    a.handle("明天下午三点安排组会")
    counts = (len(a.alarms.load()), len(a.memos.load()), len(a.schedule.load()))
    print(f"    写入后：闹钟 {counts[0]} / 备忘 {counts[1]} / 日程 {counts[2]}")
    check("三个文件都落盘", all(counts), str(counts))

    # 模拟「服务重启」：全新实例读同一批文件
    b = make_skills(tmp)
    c2 = (len(b.alarms.load()), len(b.memos.load()), len(b.schedule.load()))
    check("重启后数据仍在", c2 == counts, f"{counts} → {c2}")

    # 已播报的闹钟重启后不能重复响
    alarms = b.alarms.load()
    alarms[0]["fired"] = True
    b.alarms.save(alarms)
    d = make_skills(tmp)
    check("已响过的闹钟不再重复", d.due_alarms(now) == [])

    # 已经提醒过的日程，重启后当天也不再提醒
    sched = [
        {
            "title": "组会", "kind": "meeting", "repeat": "weekly", "weekday": 2,
            "time": "14:00", "remind_before": 10, "duration_minutes": 60,
        }
    ]
    b.schedule.save(sched)
    t = datetime(2026, 9, 23, 13, 55)
    check("第一次会提醒", len(b.due_schedule(t)) == 1)
    e = make_skills(tmp)
    check("重启后同一天不重复提醒", len(e.due_schedule(t)) == 0)

    # 落盘文件必须是合法 JSON（写到一半断电也不会坏）
    for name in ("alarms.json", "memos.json", "schedule.json"):
        raw = (tmp / name).read_text(encoding="utf-8")
        try:
            json.loads(raw)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check(f"{name} 是合法 JSON", ok)
    print(f"    文件所在目录：{tmp}")


# --------------------------------------------------------------------------- #
def main() -> int:
    test_regressions()
    test_screen_skill()
    test_weekly_course()
    test_reminder_content()
    test_persistence()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  {len(_failures)} 项未通过：")
        for f in _failures:
            print(f"    - {f}")
        print("=" * 66)
        return 1
    print(" 全部通过 √")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
