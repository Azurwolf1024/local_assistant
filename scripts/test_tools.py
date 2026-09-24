"""工具层离线测试（不需要 Ollama，也不碰真实数据）。

思路：模型只负责「选哪个工具 + 把原话塞进 text」，所以这里不需要模型也能测全部逻辑：
给 call() 构造一个假 tool_call，看它有没有落到正确的技能分支、守卫还在不在。

用法：
    python scripts/test_tools.py            # 离线，秒级
    python scripts/test_tools.py --live     # 再让真模型选一遍工具（需要 Ollama）
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging  # noqa: E402

from voice_loop.settings import load_settings  # noqa: E402

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


def tool_call(name: str, **args) -> dict:
    return {"function": {"name": name, "arguments": args}}


def build(tmp: Path):
    from voice_loop.skills import Skills
    from voice_loop.tools import ToolRegistry

    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    log = logging.getLogger("voice_loop")
    skills = Skills(settings, log)
    skills.store.save([])
    skills.store.save([])
    skills.memos.save([])
    return settings, skills, ToolRegistry(settings, skills, log)


def test_specs() -> None:
    print("\n[1] 工具定义")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    _settings, _skills, reg = build(tmp)
    specs = reg.specs()
    names = [s["function"]["name"] for s in specs]
    check("八个工具都在", sorted(names),
          ["add_event", "add_memo", "change_event", "fix_last",
           "list_events", "list_memos", "next_event", "now"])
    check("每个都有 description", all(s["function"].get("description") for s in specs), True)
    check("每个都有 parameters", all(s["function"].get("parameters") for s in specs), True)
    add = next(s for s in specs if s["function"]["name"] == "add_event")
    check("add_event 只收原话 text（不让模型算日期）",
          list(add["function"]["parameters"]["properties"].keys()), ["text"])
    check("add_event 的 text 是必填",
          add["function"]["parameters"].get("required"), ["text"])
    check("list_events 也收原话（时间说法照抄）",
          list(next(s for s in specs if s["function"]["name"] == "list_events")
               ["function"]["parameters"]["properties"].keys()), ["text"])
    check("next_event 不需要参数",
          next(s for s in specs if s["function"]["name"] == "next_event")
          ["function"]["parameters"]["properties"], {})
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_add_schedule() -> None:
    print("\n[2] add_event：模型给原话，日期由 nlp_time 算")
    tmp = Path(tempfile.mkdtemp(prefix="voicelool_tool_"))
    _settings, skills, reg = build(tmp)

    r = reg.call(tool_call("add_event", text="下周三下午三点半跟导师见面"))
    print(f"        {r.reply}")
    check("排进日程了", r.ok, True)
    check("动作是新增", r.action, "event_add")
    item = skills.store.load()[0]
    # ★别写死日期★：「下周三」是相对真实日期算的，真实日期一变就不是 9/23 了
    #（实测：2026-09-21 周一跑到这里，下周三已经变成 9/30）。改成日期无关的断言。
    start = datetime.fromisoformat(str(item.get("start")))
    check("时间对", start.strftime("%H:%M"), "15:30")
    check("是周三（周一=0）", start.weekday(), 2)
    nxt_monday = datetime.now().date() + timedelta(days=7 - datetime.now().weekday())
    check("落在下周（没跳到更远的周三，模型自己算会错成 10-04）",
          nxt_monday <= start.date() <= nxt_monday + timedelta(days=6), True)
    check("标题干净", item.get("title"), "跟导师见面")

    r = reg.call(tool_call("add_event", text="下周三下午三点半跟导师见面"))
    check("再说一遍不重复存", r.action, "event_exists")
    check("库里还是一条", len(skills.store.load()), 1)

    r = reg.call(tool_call("add_event", text="每周四上午九点有 AIA3102 机器学习"))
    check("每周重复也认（模型会漏 repeat，这里不会）", skills.store.load()[-1].get("repeat"), "weekly")
    check("星期几也对", skills.store.load()[-1].get("weekday"), 3)

    r = reg.call(tool_call("add_event", text="你好"))
    check("听不懂时不假装记下了", r.ok, False)
    check("还给了个说法", "没记上" in r.reply or "没听懂" in r.reply, True)
    check("而且真的没写进去", len(skills.store.load()), 2)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_memo() -> None:
    print("\n[3] add_memo")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    _settings, skills, reg = build(tmp)
    r = reg.call(tool_call("add_memo", text="买牛奶"))
    check("没带「记一下」也认", r.ok, True)
    check("备忘里有了", [m.get("content") for m in skills.memos.load()], ["买牛奶"])
    r = reg.call(tool_call("add_memo", text="记一下明天带伞"))
    check("带了「记一下」也认，而且时间词留着（备忘没有时间字段）",
          [m.get("content") for m in skills.memos.load()][-1], "明天带伞")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_alarm() -> None:
    print("\n[3b] add_alarm：一次性提醒不能塞进日程")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    _s, skills, reg = build(tmp)
    r = reg.call(tool_call("add_event", text="明天早上七点叫我起床"))
    print(f"        {r.reply}")
    check("记下来了", r.action, "event_add")
    check("库里一条", len(skills.store.load()), 1)
    check("内容对", skills.store.load()[0].get("title"), "起床")
    check("是准时提醒（没说提前）", skills.store.load()[0].get("remind_before"), [0])
    r = reg.call(tool_call("add_event", text="十分钟后提醒我喝水"))
    check("相对时间也认", r.action, "event_add")
    r = reg.call(tool_call("add_event", text="随便说点什么"))
    check("听不懂时不假装定了", r.ok, False)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_reads() -> None:
    print("\n[4] 只读工具：模型能查到真数据（这是它以前会编的地方）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    _settings, skills, reg = build(tmp)
    # 下次见面排到「下周的周三」——动态算，才能保证「下周」一定含它
    monday = datetime.now().date() - timedelta(days=datetime.now().weekday())
    next_wed = monday + timedelta(days=9)
    skills.store.save([
        {"title": "AIAA3102 机器学习", "category": "course", "repeat": "weekly", "weekday": 2,
         "time": "09:00", "location": "教学楼 A302", "remind_before": [15]},
        {"title": "跟导师见面", "category": "meeting", "repeat": "once", "start": f"{next_wed} 15:30",
         "time": "15:30", "remind_before": [10]},
        {"title": "起床", "start": "2026-09-19 07:00:00", "remind_before": [0]},
    ])
    skills.memos.save([{"content": "买牛奶", "done": False}])

    r = reg.call(tool_call("list_events", text="下周有什么安排"))
    print(f"        {r.reply}")
    check("查下周两个都能查到（周三的课 + 9/23 见面）",
          ("AIAA3102" in r.reply and "见面" in r.reply), True)
    r = reg.call(tool_call("list_events", text="今天有什么安排"))
    check("查今天不报错", r.ok, True)
    r = reg.call(tool_call("list_events", text="随便说点什么"))
    check("模型传了句废话也不会崩，退回今天", r.ok, True)

    # ★别把时间段悄悄换成「今天」★：以前解析不出来就退到「今天」，
    # 「下周三下午」会被答成「今天没有安排」，模型拿这句当依据去下结论（实测踩过）。
    r = reg.call(tool_call("list_events", text="下周三下午"))
    print(f"        {r.reply}")
    check("「下周三下午」答的是下周三（不是今天）", "下周三" in r.reply, True)
    r = reg.call(tool_call("list_events", text="这两周有安排吗"))
    check("时间段解析不出来时，如实说没听懂而不是编今天", "今天" in r.reply, False)

    r = reg.call(tool_call("next_event"))
    print(f"        {r.reply}")
    check("下一项查得到", ("AIAA3102" in r.reply or "见面" in r.reply), True)

    r = reg.call(tool_call("list_events"))
    check("提醒查得到", ("起床" in r.reply), True)
    r = reg.call(tool_call("list_memos"))
    check("备忘查得到", ("买牛奶" in r.reply), True)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_robustness() -> None:
    print("\n[5] 参数花样：字符串 JSON / 缺参数 / 不存在的工具")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    _settings, skills, reg = build(tmp)

    r = reg.call({"function": {"name": "add_memo", "arguments": '{"text": "买咖啡"}'}})
    check("arguments 是 JSON 字符串也能解", r.ok, True)
    check("内容对了", [m.get("content") for m in skills.memos.load()], ["买咖啡"])

    r = reg.call(tool_call("add_memo"))
    check("缺 text 要拒绝", r.ok, False)
    check("说明缺什么", "text" in r.error, True)

    r = reg.call(tool_call("delete_everything"))
    check("不存在的工具要拒绝", r.ok, False)
    check("错误里带上名字", "delete_everything" in r.error, True)

    r = reg.call({"function": {"name": "add_memo", "arguments": "这不是 JSON"}})
    check("参数不是 JSON 也不会崩", r.ok, False)

    check("调用次数被记下来（评估要用）", reg.calls, 4)
    check("失败次数被记下来", reg.errors, 3)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_live() -> None:
    print("\n[6] 真模型选工具（需要 Ollama）")
    from voice_loop.llm import OllamaClient, OllamaError

    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_tool_"))
    settings, _skills, reg = build(tmp)
    wanted = None
    for i, a in enumerate(sys.argv):
        if a == "--model" and i + 1 < len(sys.argv):
            wanted = sys.argv[i + 1]
        elif a.startswith("--model="):
            wanted = a.split("=", 1)[1]
    if wanted:
        settings.llm.model = wanted
    print(f"    模型：{settings.llm.model}  think={settings.llm.think}")
    llm = OllamaClient(settings.llm)
    try:
        llm.ensure_model()
    except OllamaError as exc:
        print(f"    ! Ollama 不可用，跳过：{exc}")
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        return

    from voice_loop.tools import TOOL_HINT

    cases = [
        ("这周有什么安排", "list_events"),
        ("下周三下午三点半跟导师见面", "add_event"),
        ("记一下买牛奶", "add_memo"),
        ("明天早上七点叫我起床", "add_event"),
        ("我的备忘里有什么", "list_memos"),
        ("下一个会议是什么", "next_event"),
        ("你好，你是谁", None),          # 闲聊不该调工具
        ("讲一下插头DP", None),
    ]
    hit = 0
    for text, want in cases:
        _content, calls = llm.chat_tools(
            [
                {"role": "system", "content": settings.llm.system_prompt},
                {"role": "system", "content": TOOL_HINT},
                {"role": "user", "content": text},
            ],
            tools=reg.specs(),
        )
        got = calls[0]["function"]["name"] if calls else None
        ok = got == want
        hit += ok
        print(f"    {PASS if ok else FAIL} 「{text}」 -> {got!r}" + ("" if ok else f"  (期望 {want!r})"))
        if not ok:
            _failures.append(f"live:{text}")
    print(f"    命中 {hit}/{len(cases)}")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    live = "--live" in sys.argv
    print("=" * 66)
    print(" 工具层测试（模型选工具，确定性代码干活）")
    print("=" * 66)
    test_specs()
    test_add_schedule()
    test_memo()
    test_alarm()
    test_reads()
    test_robustness()
    if live:
        test_live()
    print("\n" + "=" * 66)
    if _failures:
        print(f" 失败 {len(_failures)} 项：{_failures}")
        print("=" * 66)
        return 1
    print(" 全部通过 √")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
