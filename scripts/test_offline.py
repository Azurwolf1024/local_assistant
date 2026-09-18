"""离线自测：分块器 / 中文时间解析 / 生活技能 / 唤醒词。

不依赖麦克风、ASR 模型和 Ollama，几秒就能跑完：
    python scripts/test_offline.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.nlp_time import (  # noqa: E402
    cn_number,
    cn_quantity,
    humanize,
    parse_datetime,
    parse_duration,
)
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.skills import WEEKDAY_NAMES, Skills  # noqa: E402
from voice_loop.text import SpeechChunker, prepare_for_reading, transplant_punctuation  # noqa: E402
from voice_loop.wake import WakeWordMatcher  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, expect=None, contains: str | None = None) -> None:
    ok = True
    if expect is not None:
        ok = got == expect
    elif contains is not None:
        ok = contains in str(got)
    print(f"  {PASS if ok else FAIL} {name}: {got!r}" + ("" if ok else f"   (期望 {expect or contains!r})"))
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
def test_chunker() -> None:
    print("\n[1] 流式分块（语调自然的关键）")
    c = SpeechChunker(max_chars=60, first_min_chars=8, min_chunk_chars=14, max_hold_seconds=99)
    text = "好的。今天天气不错，要不要出去走走？我觉得可以，那就下午三点吧。"
    out: list[str] = []
    for ch in text:
        out.extend(c.feed(ch))
    out.extend(c.flush())
    print(f"    输入: {text}")
    for i, s in enumerate(out, 1):
        print(f"    块{i}: {s}")
    check("句末切分（不在逗号处切）", all(not b.endswith("，") for b in out), True)
    check("短句被合并", out[0].startswith("好的。今天"), True)

    c2 = SpeechChunker(max_chars=20, min_chunk_chars=1)
    long_sentence = "这是一句很长很长的话" * 4
    chunks = c2.feed(long_sentence) + c2.flush()
    check("超长句兜底切分", len(chunks) >= 2, True)
    check("每块都有内容", all(s.strip() for s in chunks), True)
    check("不丢字", "".join(chunks).replace("。", "") == long_sentence, True)

    c3 = SpeechChunker(max_chars=60, min_chunk_chars=14, max_hold_seconds=0.0)
    held = c3.feed("好的。")
    check("超时后会先送出去", len(held) >= 0, True)
    check("标点移植", transplant_punctuation("开放时间早上九点", "开饭时间早上9点。"), contains="。")
    check("Markdown 清理", prepare_for_reading("**你好**，这是 `代码`"), contains="你好")
    check("开头语气词不被删", prepare_for_reading("好的，我知道了"), contains="好的")


# --------------------------------------------------------------------------- #
def test_time() -> None:
    print("\n[2] 中文时间解析")
    now = datetime(2026, 9, 17, 22, 30)  # 周四晚上十点半
    cases = [
        ("十分钟后提醒我喝水", "2026-09-17 22:40"),
        ("半小时后提醒我", "2026-09-17 23:00"),
        ("一个半小时后提醒我", "2026-09-18 00:00"),
        ("明天早上七点叫我起床", "2026-09-18 07:00"),
        ("下午三点开会", "2026-09-18 15:00"),
        ("后天中午十二点半提醒我", "2026-09-19 12:30"),
        ("今晚九点提醒我吃药", "2026-09-17 21:00"),
        ("19:30 提醒我", "2026-09-18 19:30"),
    ]
    for text, expect in cases:
        got = parse_datetime(text, now)
        check(f"{text}", got.strftime("%Y-%m-%d %H:%M") if got else None, expect)
    check("时长解析", str(parse_duration("2小时15分钟")), "2:15:00")
    check("时间口语化", humanize(datetime(2026, 9, 18, 7, 0), now), "明天早上七点")
    check("月日读法", humanize(datetime(2026, 9, 24, 9, 0), now), "九月二十四日上午九点")
    check("中文数字", cn_number(153), "一百五十三")
    check("量词两", cn_quantity(2), "两")

    # 星期说法：「这/本」是本周（可能已过）、「下」是下一周。
    # 这里踩过坑：以前 (target-today)%7 之后再 +7，「下周三」会多算一周
    # （周五说 → 9/30 而不是 9/23），已经写进提醒里过。
    fri = datetime(2026, 9, 18, 18, 48)          # 周五
    for text, expect in [
        ("周三下午三点", "2026-09-23"),           # 最近的将来那个周三
        ("这周三下午三点", "2026-09-16"),         # 本周三（已过）
        ("本周三下午三点", "2026-09-16"),
        ("下周三下午三点", "2026-09-23"),         # ★ 下一周的周三
        ("下周一上午十点", "2026-09-21"),
        ("上周三下午三点", "2026-09-09"),
        ("下下周三上午九点", "2026-09-30"),
        ("下周三上午九点有 AIAA3102 课", "2026-09-23"),
    ]:
        got = parse_datetime(text, fri)
        check(f"周说法 {text}", got.strftime("%Y-%m-%d") if got else None, expect)
    wed = datetime(2026, 9, 16, 10, 0)           # 周三当天
    check("周三当天说周三=今天", parse_datetime("周三下午三点", wed).strftime("%Y-%m-%d"), "2026-09-16")


# --------------------------------------------------------------------------- #
def test_skills() -> None:
    print("\n[3] 生活技能")
    settings = load_settings()
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_test_"))
    settings.skills.alarm_file = str(tmp / "alarms.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.skills.schedule_file = str(tmp / "schedule.json")
    settings.skills.data_dir = str(tmp)
    skills = Skills(settings)

    # 写入一条每周三 09:00 的课
    skills.schedule.save(
        [
            {"title": "AIAA3102 机器学习", "repeat": "weekly", "weekday": 3, "time": "09:00",
             "location": "A302", "remind_before": 15, "duration_minutes": 90},
            {"title": "组会", "repeat": "weekly", "weekday": 3, "time": "14:00", "duration_minutes": 60},
        ]
    )
    now = datetime(2026, 9, 17, 22, 30)  # 周四

    def run(q: str) -> str:
        r = skills.handle(q)
        if r is None:
            return "（未命中技能 → 交给 LLM）"
        return f"[{r.action}] {r.reply}"

    for q in [
        "现在几点了",
        "今天星期几",
        "十分钟后提醒我喝水",
        "明天早上七点叫我起床",
        "我的提醒有哪些",
        "记一下，明天买牛奶",
        "记以下，明天要买牛奶",    # 模拟语音识别的听错
        "提醒我买牛奶",          # 没时间 -> 存备忘
        "我的备忘有哪些",
        "今天有什么课",
        "这周有什么安排",
        "下一个会议是什么",
        "你能做什么",
        "给我讲个笑话",          # 应该走到 LLM
    ]:
        r = skills.handle(q)
        label = "（交给 LLM）" if r is None else f"[{r.action}] {r.reply}"
        print(f"    {q:24s} -> {label}")

    check("闹钟被创建", len(skills.alarms.load()) >= 2, True)
    check("备忘被创建", len(skills.memos.load()) >= 2, True)
    check("闲聊不抢话", skills.handle("给我讲个笑话"), None)
    check("时间类命中", skills.handle("现在几点了") is not None, True)
    check("今天能查到课", "课程" in skills.handle("今天有什么课").reply or "安排" in skills.handle("今天有什么课").reply, True)

    # 单条取消（放到最后，避免影响前面的计数）
    r = skills.handle("取消第1个提醒")
    print(f"    {'取消第1个提醒':24s} -> [{r.action}] {r.reply}")
    check("按序号取消单条", r.action, "alarm_cancel")
    r = skills.handle("清空所有提醒")
    check("清空全部", r.action, "alarm_clear")

    # ---- 日程的改 / 删 / 只跳过这一次 ----
    # 以前这三件事都做不了：「删掉每周三那节课」因为句子里有「每周三+时刻」
    # 被当成新增，反而往课表里塞一条垃圾；「取消…那节课」掉进查询分支答非所问。
    print("    · 日程改/删/跳过")
    before = len(skills.schedule.load())
    r = skills.handle("把组会挪到周五上午十点")
    moved = [i for i in skills.schedule.load() if i["title"] == "组会"]
    check(
        "改：挪到周五十点",
        (getattr(r, "action", None), bool(moved) and moved[0].get("weekday") == 4,
         bool(moved) and moved[0].get("time") == "10:00"),
        ("schedule_edit", True, True),
    )
    check("改：没多出一条来", len(skills.schedule.load()), before)

    # 「周五的课不上了」= 只跳过这一次（用条目自己的星期说，免得依赖今天是星期几）
    first = [i for i in skills.schedule.load() if i.get("repeat") == "weekly"][0]
    wd_name = WEEKDAY_NAMES[int(first["weekday"])]
    r = skills.handle(f"{first['title']} {wd_name}的课不上了")
    skipped = [i for i in skills.schedule.load() if i.get("skip")]
    check(
        "跳过：只记一天",
        (getattr(r, "action", None), len(skipped), len(skipped[0]["skip"]) if skipped else 0),
        ("schedule_skip", 1, 1),
    )
    if skipped:
        day = skipped[0]["skip"][0]
        nxt = skills.next_occurrence(skipped[0], datetime(2026, 9, 17, 22, 30))
        check("跳过：下次不算这一天", nxt.strftime("%Y-%m-%d") != day, True)
        check("跳过：这一天已经过去就不算", datetime.strptime(day, "%Y-%m-%d").date() >= datetime.now().date(), True)
    r2 = skills.handle(f"{first['title']} {wd_name}的课不上了")
    check("跳过：重复说不叠加", len([i for i in skills.schedule.load() if i.get("skip")]), 1)
    check("跳过：重复说会说明白", "本来就没安排" in (r2.reply or ""), True)

    r = skills.handle("以后不上组会了")
    check("删：彻底删除", (getattr(r, "action", None), len(skills.schedule.load())), ("schedule_delete", before - 1))

    # 问句绝不能真的动手
    keep = len(skills.schedule.load())
    skills.handle("今天的课不上吗？")
    check("问句不会误删", len(skills.schedule.load()), keep)

    # 到点检测
    due = skills.due_alarms(datetime.now())
    print(f"    到点闹钟：{[d.get('what') for d in due]}")
    tasks = []
    for item in skills.schedule.load():
        nxt = skills.next_occurrence(item, datetime.now())
        tasks.append((item["title"], nxt.strftime("%Y-%m-%d %H:%M") if nxt else None))
    print(f"    下次日程：{tasks}")
    check("日程能算出下次时间", all(t[1] for t in tasks), True)

    # 数据文件是带注释的包装格式，写入后注释要保留
    raw = json.loads((tmp / "alarms.json").read_text(encoding="utf-8"))
    print(f"    写入后的 alarms.json：{json.dumps(raw, ensure_ascii=False)[:120]}")

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_schedule_model() -> None:
    """日程模型：多个提前提醒 / 循环周期 / 截止 / 结束后重排。"""
    print("\n[4] 日程模型（多个提醒 + 循环周期）")
    from datetime import timedelta

    from voice_loop.nlp_time import parse_reminds, parse_repeat, parse_until
    from voice_loop.skills import Skills

    settings = load_settings()
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_sched_"))
    # ★ 三个文件都要指到临时目录，不然会写进真实 data/
    settings.skills.data_dir = str(tmp)
    settings.skills.alarm_file = str(tmp / "alarms.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.skills.schedule_file = str(tmp / "schedule.json")
    skills = Skills(settings)
    skills.schedule.save([])

    print("    · 解析")
    for text, expect in [
        ("每周三上午九点有课", {"repeat": "weekly", "weekday": 2}),
        ("每两周周三下午两点组会", {"repeat": "biweekly", "weekday": 2}),
        ("每月5号交房租", {"repeat": "monthly", "day": 5}),
        ("每年3月1日开学", {"repeat": "yearly", "month": 3, "day": 1}),
        ("每3天浇一次花", {"repeat": "interval", "every_days": 3}),
        ("每2小时起来活动", {"repeat": "interval", "every_minutes": 120}),
        ("每天吃药", {"repeat": "interval", "every_days": 1}),
        ("明天下午三点开会", None),
    ]:
        check(f"周期 {text}", parse_repeat(text), expect)
    for text, expect in [
        ("提前一天和半小时提醒我", [1440, 30]),
        ("提前30分钟、10分钟和到点提醒我", [30, 10, 0]),
        ("提前十分钟提醒我", [10]),
        ("到点提醒我", [0]),
        ("明天下午三点开会", None),
    ]:
        check(f"提前量 {text}", parse_reminds(text), expect)
    check("截止 12月底", parse_until("每周三有课，到12月底为止", datetime(2026, 9, 18)), datetime(2026, 12, 31).date())

    print("    · 新增各类日程")
    cases = [
        ("每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302", "schedule_add_weekly"),
        ("每两周周三下午两点开组会", "schedule_add_weekly"),
        ("每月5号下午三点交房租", "schedule_add"),
        ("每年3月1日上午九点开学典礼", "schedule_add"),
        ("每3天浇一次花，第一次是明天上午八点", "schedule_add"),
        ("明天下午三点安排项目评审会，提前30分钟、10分钟和到点提醒我", "schedule_add"),
        ("每天提醒我吃药，到12月底为止", "schedule_add"),
    ]
    for text, action in cases:
        r = skills.handle(text)
        check(f"新增 [{action}] {text[:16]}", getattr(r, "action", None), action)
        if r is not None:
            print(f"        {r.reply}")

    items = {i.get("title", ""): i for i in skills.schedule.load()}

    def find(key: str) -> dict:
        hit = next((it for t, it in items.items() if key in t), None)
        assert hit is not None, f"没找到标题含「{key}」的日程：{list(items)}"
        return hit

    print(f"    落盘 {len(items)} 条：")
    for title, it in items.items():
        print(f"      {title}: repeat={it.get('repeat')} "
              f"weekday={it.get('weekday')} day={it.get('day')} time={it.get('time')} "
              f"leads={it.get('remind_before')} until={it.get('until')}")

    check("多个提前量写进了数组", find("评审").get("remind_before"), [30, 10, 0])
    check("每周课的提前量也支持多个", find("AIAA3102").get("remind_before"), [10])
    check("每月存下了几号", find("房租").get("day"), 5)
    check("每月标题不带「每月5号」", "每月" not in next(t for t in items if "房租" in t), True)
    check("每年存下了月日",
          (find("开学").get("month"), find("开学").get("day")), (3, 1))
    check("间隔循环存的是间隔", find("吃药").get("every_days"), 1)
    check("截止日期落盘", find("吃药").get("until"), "2026-12-31")

    print("    · 周期推进（next_occurrence）")
    now = datetime(2026, 9, 18, 21, 0)          # 周五晚
    marks = {"AIAA3102": "mach", "组会": "group", "房租": "rent",
             "开学": "school", "浇": "water"}
    steps = {}
    for key, name in marks.items():
        it = find(key)
        first = skills.next_occurrence(it, now)
        second = skills.next_occurrence(it, first + timedelta(seconds=1)) if first else None
        steps[name] = (first, second)
        print(f"      {it.get('title')}: {first} -> {second}")
    check("每周课下一次是下周三", steps["mach"][0].strftime("%m-%d %H:%M"), "09-23 09:00")
    check("双周下一次隔两周", (steps["group"][1] - steps["group"][0]).days, 14)
    check("每月下一次是下月同日",
          (steps["rent"][0].strftime("%m-%d"), steps["rent"][1].strftime("%m-%d")),
          ("10-05", "11-05"))
    check("每年下一次是明年同日", steps["school"][0].strftime("%Y-%m-%d"), "2027-03-01")
    check("每3天：第一次是明天上午八点", steps["water"][0].strftime("%m-%d %H:%M"), "09-19 08:00")
    check("每3天：结束后再排下一次（间隔 3 天）",
          (steps["water"][1] - steps["water"][0]).days, 3)

    print("    · 每月 31 号碰上小月要回退到月末")
    skills.schedule.append({"title": "月底对账", "repeat": "monthly", "day": 31,
                            "time": "20:00", "start": "2026-09-30 20:00", "remind_before": [0]})
    feb = skills.next_occurrence(
        [i for i in skills.schedule.load() if i["title"] == "月底对账"][0], datetime(2027, 1, 31, 21, 0)
    )
    check("1/31 之后是 2/28", feb.strftime("%Y-%m-%d"), "2027-02-28")

    print("    · 多个提前量各自只响一次")
    ev = find("评审")
    start = skills.next_occurrence(ev, datetime(2026, 9, 18, 21, 0))
    fired = []
    for mins in (31, 30, 11, 10, 0):
        got = [t for _i, t in skills.due_schedule(start - timedelta(minutes=mins))]
        fired.append((mins, len(got)))
        print(f"      提前 {mins:>3} 分钟 -> {got}")
    check("31 分钟时不该响", fired[0][1], 0)
    check("30 分钟时响一次", fired[1][1], 1)
    check("11 分钟时不该响", fired[2][1], 0)
    check("10 分钟时响一次", fired[3][1], 1)
    check("到点响一次", fired[4][1], 1)
    check("再问一遍不会重复提醒（已记 _fired）",
          len(skills.due_schedule(start - timedelta(minutes=30))), 0)
    check("开始 6 分钟后不再补报",
          len(skills.due_schedule(start + timedelta(minutes=6))), 0)

    print("    · 循环重复/多个提前量的说法要交给日程，不是闹钟")
    check("每周三提醒我上课 -> 日程",
          getattr(skills.handle("每周三上午九点提醒我上课"), "action", "").startswith("schedule"), True)
    check("十分钟后提醒我喝水 -> 还是闹钟",
          getattr(skills.handle("十分钟后提醒我喝水"), "action", ""), "alarm_add")

    print("    · 一段时间要报一段时间（以前「下周」只报下周一）")
    # 换一套干净的固定数据：周三 09:00 课、周四 14:00 组会、每月 30 号对账
    skills.schedule.save(
        [
            {"title": "AIAA3102 机器学习", "repeat": "weekly", "weekday": 2, "time": "09:00",
             "duration_minutes": 90, "location": "教学楼 A302", "remind_before": [15]},
            {"title": "组会", "repeat": "weekly", "weekday": 3, "time": "14:00",
             "duration_minutes": 60, "remind_before": [10]},
            {"title": "月度对账", "repeat": "monthly", "day": 30, "time": "20:00",
             "remind_before": [30]},
        ]
    )
    fri = datetime(2026, 9, 18, 21, 0)          # 周五
    for text, expect in [
        ("今天有什么课", ("2026-09-18", "2026-09-18", "今天")),
        ("明天有什么安排", ("2026-09-19", "2026-09-19", "明天")),
        ("这周有什么安排", ("2026-09-14", "2026-09-20", "这周")),      # 周一到周日
        ("下周有什么安排", ("2026-09-21", "2026-09-27", "下周")),
        ("下下周有什么安排", ("2026-09-28", "2026-10-04", "下下周")),
        ("这个周末有什么安排", ("2026-09-19", "2026-09-20", "这个周末")),
        ("这个月有什么安排", ("2026-09-01", "2026-09-30", "这个月")),
        ("下个月有什么安排", ("2026-10-01", "2026-10-31", "下个月")),
        ("今年有什么安排", ("2026-01-01", "2026-12-31", "今年")),
        ("明年有什么安排", ("2027-01-01", "2027-12-31", "明年")),
        ("未来三天有什么安排", ("2026-09-18", "2026-09-20", "未来三天")),
        ("未来一周有什么安排", ("2026-09-18", "2026-09-24", "未来一周")),
        ("这几天有什么安排", ("2026-09-18", "2026-09-21", "这几天")),
        ("有什么安排", ("2026-09-18", "2026-09-18", "今天")),          # 没说范围 = 今天
    ]:
        s, e, label = skills._range_of(text, fri)          # noqa: SLF001
        check(f"范围 {text}",
              (s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), label), expect)

    r = skills.handle("下周有什么安排")
    print(f"        {r.reply}")
    check("「下周」真的报一整周（含下周三、周四）",
          ("下周（9月21日到9月27日）" in r.reply and "周三上午九点" in r.reply
           and "周四下午两点" in r.reply), True)
    r = skills.handle("下周呢")
    check("「下周呢」也能当日程查询", getattr(r, "action", None), "schedule_query")
    r = skills.handle("这个月有什么安排")
    check("「这个月」报整月（含 9月30日 对账）",
          ("这个月" in r.reply and "9月30日" in r.reply and "月度对账" in r.reply), True)
    r = skills.handle("下个月有什么安排")
    check("「下个月」报下个月", ("下个月" in r.reply and "10月" in r.reply), True)
    r = skills.handle("这周有什么安排")
    print(f"        {r.reply}")
    check("「这周」不列已经过去的（周五问，周三周四的课不该出现）",
          ("9月16日" not in r.reply and "AIAA3102" not in r.reply), True)

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_wake() -> None:
    print("\n[5] 唤醒词匹配")
    settings = load_settings()
    path = settings.resolve(settings.wake.file)
    matcher = WakeWordMatcher(path)
    print(f"    配置：{'、'.join(matcher.settings.words)}  ack={matcher.settings.ack!r}")
    print(f"    idle_timeout={matcher.settings.idle_timeout:.0f}s  fuzzy_ratio={matcher.settings.fuzzy_ratio}")
    cases = [
        ("凯尔希", True),
        ("凯尔希，现在几点了", True),
        ("凯尔西", True),
        ("凯尔希医生在吗", True),
        ("凯尔惜我明天要开会", True),
        ("开尔希", True),
        ("卡尔希", True),
        ("今天天气不错", False),
        ("帮我查一下明天的课", False),
        ("开尔文是热力学温标", False),
        ("凯旋而归", False),
        ("希尔顿酒店", False),
    ]
    ok = True
    for text, expect_hit in cases:
        hit = matcher.match(text)
        got = hit is not None
        mark = PASS if (expect_hit is None or got == expect_hit) else FAIL
        if expect_hit is not None and got != expect_hit:
            ok = False
        rest = matcher.strip_word(text, hit) if hit else "-"
        print(f"    {mark} {text:18s} 命中={got} 词={hit.word if hit else '-'} 剩余={rest!r}")
    check("唤醒判定符合预期", ok, True)


# --------------------------------------------------------------------------- #
def test_lifecycle() -> None:
    """验证「唤醒加载 / 空闲回收」状态机（不碰硬件，不加载模型）。"""
    print("\n[6] 唤醒服务生命周期")
    from voice_loop.pipeline import VoiceLoop

    settings = load_settings()
    settings.wake.idle_action = "standby"
    # 生命周期测试不需要往屏幕上弹东西
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        check("懒加载模式已开启", loop.lazy, True)
        check("初始为待唤醒", loop._active, False)  # noqa: SLF001
        check("TTS 初始未加载", getattr(loop.tts, "loaded", None), False)
        check("空闲超时来自唤醒词 json", loop._idle_timeout, 180.0)  # noqa: SLF001
        check("唤醒后进入活跃", (loop._activate("测试"), loop._active)[1], True)  # noqa: SLF001
        loop._deactivate()  # noqa: SLF001
        check("回待唤醒后释放", loop._active, False)  # noqa: SLF001

        # idle_action = exit 时应该结束进程
        settings.wake.idle_action = "exit"
        loop._activate("测试")  # noqa: SLF001
        check("idle_action=exit 会置停止位", loop._deactivate(), True)  # noqa: SLF001
        check("停止事件已设置", loop._stop.is_set(), True)  # noqa: SLF001
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 66)
    print(" 离线自测（不占用麦克风 / 不加载模型）")
    print("=" * 66)
    test_chunker()
    test_time()
    test_skills()
    test_schedule_model()
    test_wake()
    test_lifecycle()
    print("\n" + "=" * 66)
    if _failures:
        print(f" 失败 {len(_failures)} 项：{_failures}")
    else:
        print(" 全部通过 √")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
