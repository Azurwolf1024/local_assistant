"""路由自测：`[llm] route = model`（模型先选工具）与 `rules`（模式匹配先）。

为什么不联网也能测：这一层测的是**顺序与兜底**，不是模型能力——
假的 Ollama 客户端按脚本返回「文字」或「工具调用」，就能把每种组合走一遍。

跑法：
    python scripts/test_route.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.pipeline import VoiceLoop  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

_ok = 0
_bad: list[str] = []


def check(label: str, got, expect=True) -> None:
    global _ok
    if got == expect:
        _ok += 1
        print(f"  √ {label}: {got!r}")
    else:
        _bad.append(label)
        print(f"  × {label}: {got!r}   (期望 {expect!r})")


class FakeLLM:
    """脚本化的假模型：要么吐文字，要么给工具调用，要么什么都不给。"""

    def __init__(self, kind: str = "text", payload=None) -> None:
        self.kind = kind
        self.payload = payload
        self.turns = 0
        self.last_tools: list[dict] | None = None
        self.history: list[tuple[str, str]] = []

    # 与 OllamaClient 同名的接口（VoiceLoop 只会用到这些）
    def chat_events(
        self,
        user_text: str = "",
        images=None,
        model=None,
        num_ctx=None,
        tools=None,
        extra_messages=None,
        messages=None,
        prefix_messages=None,
    ):
        self.turns += 1
        self.last_tools = tools
        self.last_prefix = prefix_messages
        if self.kind == "tool":
            yield {"tool_calls": list(self.payload or [])}
        elif self.kind == "text":
            for ch in str(self.payload or ""):
                yield {"delta": ch}
        # kind == "empty"：什么都不吐

    def commit(self, user_text: str, answer: str) -> None:
        self.history.append((user_text, answer))

    def reset(self) -> None:
        self.history.clear()

    # close() / 生命周期会用到的那几个
    def release(self, model=None) -> bool:
        return True

    def is_loaded(self, model=None):
        return True

    def ensure_model(self) -> None:
        return None

    def warmup(self, model=None) -> float:
        return 0.0


def call(name: str, **args) -> dict:
    return {"function": {"name": name, "arguments": args}}


def make_loop(tmp: Path, route: str = "model"):
    settings = load_settings()
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.llm.route = route
    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    loop.tts_enabled = False          # 不真的出声
    loop.skills.store.save([])
    loop.skills.memos.save([])
    loop.skills.store.save([])
    return loop


# --------------------------------------------------------------------------- #
def test_gate() -> None:
    """先看「这句话要不要攒着等决定」判断得对不对（多包含没事，漏了才会答错）。"""
    print("\n[1] 兜底触发条件（skills.needs_attention）")
    from voice_loop.skills import Skills

    with tempfile.TemporaryDirectory(prefix="route_gate_") as td:
        settings = load_settings()
        settings.skills.data_dir = td
        settings.skills.event_file = str(Path(td) / "a.json")
        settings.skills.memo_file = str(Path(td) / "m.json")
        sk = Skills(settings)
        sk.store.save([])
        for text, expect in [
            ("这周有什么安排", True),
            ("我的备忘里有什么", True),
            ("取消明天早上的安排", True),
            ("现在几点", True),
            ("看看桌面上有什么", True),
            ("记一下买牛奶", True),
            ("讲一下插头DP", False),
            ("你好，你是谁", False),
            ("帮我写个快排", False),
        ]:
            check(f"{text:16s} {'要看技能层' if expect else '不用'}", sk.needs_attention(text), expect)

        # 刚记下一条 → 后面那句（哪怕是句闲聊）也要攒着，好接「我说是今晚8点45」
        sk._remember_add(1)  # noqa: SLF001
        check("刚记下一条之后，下一句也攒着", sk.needs_attention("你好"), True)
        sk._last_add["at"] -= 200.0  # noqa: SLF001
        check("超过 3 分钟就不再攒", sk.needs_attention("你好"), False)


# --------------------------------------------------------------------------- #
def test_model_first() -> None:
    """route = model：模型先选工具；它漏了才轮到技能层兜底。"""
    print("\n[2] route = model（模型先选工具）")
    with tempfile.TemporaryDirectory(prefix="route_model_") as td:
        tmp = Path(td)

        # --- 2.1 模型调了工具：一趟就完事，技能层不参与 ---
        loop = make_loop(tmp)
        loop.llm = FakeLLM("tool", [call("list_events", text="这周有什么安排")])
        stats = loop.respond("这周有什么安排")
        check("模型调工具：路由标记", stats.extra.get("route"), "model")
        check("工具名记进了统计", "list_events" in str(stats.extra.get("tool")), True)
        check("没有落在技能路径上", stats.extra.get("skill"), None)
        check("只问了一次模型（没有多余往返）", loop.llm.turns, 1)
        check("模型确实拿到了工具清单", len(loop.llm.last_tools or []), 8)
        check("答话来自工具（不是模型自己编）", "没" in stats.answer or "安排" in stats.answer, True)
        loop.close()

        # --- 2.2 模型没调工具，只说了句废话 → 技能层兜住，模型的话不念 ---
        loop = make_loop(tmp)
        loop.skills.store.save([{
            "title": "跟导师见面", "kind": "meeting", "repeat": "once",
            "start": "2026-09-23 15:30", "time": "15:30", "remind_before": [10],
        }])
        loop.llm = FakeLLM("text", "我不知道，你查查日历吧。")
        stats = loop.respond("这周有什么安排")
        check("模型漏了：路由标记改成兜底", stats.extra.get("route"), "model→skills")
        check("答话来自技能层", stats.extra.get("skill"), "event_query")
        check("★模型那句没被念出来★", "你查查日历吧" in stats.answer, False)
        check("技能层的话进了回答", "安排" in stats.answer, True)
        loop.close()

        # --- 2.3 闲聊：不给模型添麻烦，直接边生成边播 ---
        loop = make_loop(tmp)
        loop.llm = FakeLLM("text", "插头 DP 是轮廓线 DP 的一种。")
        stats = loop.respond("讲一下插头DP")
        check("闲聊：路由标记就是 model", stats.extra.get("route"), "model")
        check("没有走兜底", stats.extra.get("deferred"), None)
        check("★没被攒住（边说边播）★", stats.extra.get("hold"), False)
        check("答的就是模型说的话", stats.answer, "插头 DP 是轮廓线 DP 的一种。")
        loop.close()

        # --- 2.4 模型什么都不说：技能能接住就由技能答，接不住才补念一句兜底话 ---
        loop = make_loop(tmp)
        loop.llm = FakeLLM("empty")
        stats = loop.respond("这周有什么安排")
        check("模型空话但技能接得住：走技能兜底", stats.extra.get("route"), "model→skills")
        check("答话不是空的", bool(stats.answer.strip()), True)
        loop.close()

        loop = make_loop(tmp)
        loop.llm = FakeLLM("empty")
        # 把技能层按住，专门看「两边都没话说」时的兵底话
        loop.skills.handle = lambda *a, **k: None  # type: ignore[assignment]
        stats = loop.respond("这周有什么安排")
        check("技能也接不住：走了 deferred 路径", stats.extra.get("deferred"), True)
        check("补念了一句兜底话，不是空白", bool(stats.answer.strip()), True)
        loop.close()

        # --- 2.5 看图这种「模型根本做不到」的事，仍然由技能层接住 ---
        loop = make_loop(tmp)
        loop.llm = FakeLLM("text", "我看不到桌面。")
        called: list[str] = []

        def fake_vision(stats, skill, user_text, on_delta=None):
            called.append(skill.action)
            stats.extra["route"] = "model→skills"
            return stats

        loop._respond_vision = fake_vision  # noqa: SLF001
        stats = loop.respond("看看桌面上有什么")
        check("看图交给技能层", called, ["vision"])
        check("路由标记是兜底", stats.extra.get("route"), "model→skills")
        loop.close()


# --------------------------------------------------------------------------- #
def test_rules_first() -> None:
    """route = rules：老顺序，先模式匹配；没接住才给模型。"""
    print("\n[3] route = rules（模式匹配先，老行为）")
    with tempfile.TemporaryDirectory(prefix="route_rules_") as td:
        loop = make_loop(Path(td), route="rules")
        loop.llm = FakeLLM("text", "模型不该被叫到")
        stats = loop.respond("现在几点")
        check("技能先接住", stats.extra.get("route"), "rules→skills")
        check("★模型一次都没被叫★", loop.llm.turns, 0)
        check("答的是技能的话", "点" in stats.answer, True)

        loop.llm = FakeLLM("text", "随便聊两句。")
        stats = loop.respond("讲一下插头DP")
        check("技能接不住的才给模型", stats.extra.get("route"), "rules→llm")
        check("模型被叫了一次", loop.llm.turns, 1)
        check("答的是模型的话", stats.answer, "随便聊两句。")
        loop.close()


# --------------------------------------------------------------------------- #
def test_new_tools() -> None:
    """新铺开的四个工具：改 / 取消 / 报时 / 纠正（都复用技能层的守卫）。"""
    print("\n[4] 新工具（change_schedule / cancel_alarm / now / fix_last）")
    with tempfile.TemporaryDirectory(prefix="route_tools_") as td:
        tmp = Path(td)
        loop = make_loop(tmp)
        reg = loop.tools
        assert reg is not None
        check("工具总数", len(reg.specs()), 8)

        got = reg.call(call("now", text="现在几点"))
        check("now → 报时", got.ok and "点" in got.reply, True)

        got = reg.call(call("add_event", text=f"{(datetime.now() + timedelta(hours=3)).strftime('%H:%M')}提醒我喝水"))
        check("add_event → 建了一条", got.ok, True)
        check("库里 1 条", len(loop.skills.store.load()), 1)

        # ★这个工具必须只做取消★：模型把「提醒我」递进来时，不能反而多一条
        got = reg.call(call("change_event", text="提醒我喝水"))
        check("change_event 不会新建提醒", got.ok, False)
        check("库里还是 1 条", len(loop.skills.store.load()), 1)

        when = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
        got = reg.call(call("add_event", text=f"{when}提醒我练琴"))
        check("先建一条用来改", got.ok, True)
        got = reg.call(call("fix_last", text=f"{when}"))
        check("fix_last → 改了刚记下的那条", got.ok, True)
        check("没有多出一条", len(loop.skills.store.load()), 2)

        got = reg.call(call("change_event", text=f"取消{when}的闹钟"))
        check("change_event → 取消了", got.ok and got.action.startswith("event_cancel"), True)

        loop.skills.store.save([{
            "title": "组会", "category": "meeting", "repeat": "weekly",
            "weekday": 4, "time": "14:00", "remind_before": [10],
        }])
        got = reg.call(call("change_event", text="删掉组会"))
        check("change_event → 走到了删/跳过的分支",
              got.ok and got.action.startswith("event_"), True)
        check("重复的那条默认只跳过一次（不轻易删）",
              len(loop.skills.store.load()) == 1 and "event_skip" == got.action, True)
        loop.close()


def test_repair_args() -> None:
    """模型改写原话时，把丢了时间的参数换回来（机械校验，不靠模型自觉）。"""
    print("\n[5] repair_args（把被模型改写的参数换回原话）")
    from voice_loop.tools import repair_args

    user = "下周三下午三点半跟导师见面"
    stripped = call("add_event", text="跟导师见面")
    fixed = repair_args(stripped, user)
    check("丢时间 → 换回原话",
          fixed["function"]["arguments"]["text"], user)

    same = call("add_event", text=user)
    check("本来就是原话 → 不动", repair_args(same, user) is same, True)

    other = call("add_event", text="明天上午十点跟导师见面")
    check("模型自己填了别的时间 → 不动（可能是在纠正听错）",
          repair_args(other, user) is other, True)

    q = call("list_events", text="下周")
    check("查询类不改（模型缩小范围是合理的）",
          repair_args(q, "帮我看看下周都有什么事") is q, True)

    no_time = call("add_memo", text="买牛奶")
    check("备忘没有时间可算 → 不动", repair_args(no_time, "记一下买牛奶") is no_time, True)

    check("原话为空时不乱动", repair_args(stripped, "") is stripped, True)

    # 修正句（「我说是今晚十点」）一律用原话：实测模型会把「十点」改写成「8点45」
    invented = call("fix_last", text="我说今天晚上8点45")
    check("fix_last 一律用原话（不看模型改写）",
          repair_args(invented, "我说是今晚十点")["function"]["arguments"]["text"],
          "我说是今晚十点")

    # 动词被吞掉也要换回原话：时间还在，但技能层认不出是要新建提醒
    verb_gone = call("add_event", text="明天早上七点练琴")
    check("动词被吞掉 → 换回原话",
          repair_args(verb_gone, "提醒我明天早上七点练琴")["function"]["arguments"]["text"],
          "提醒我明天早上七点练琴")
    check("模型自己补上了动词 → 就用它的",
          repair_args(call("add_event", text="明天早上七点叫我起床"),
                      "提醒我明天早上七点起床")["function"]["arguments"]["text"],
          "明天早上七点叫我起床")


# --------------------------------------------------------------------------- #
def test_reroute_correction() -> None:
    """刚记下一条 + 这句只说时间 → 把 add_* 掰回 fix_last（否则会响两次）。"""
    print("\n[6] reroute_correction（紧接着的新时间 = 改上一条，不是新建）")
    from voice_loop.tools import reroute_correction

    with tempfile.TemporaryDirectory(prefix="route_reroute_") as td:
        settings = load_settings()
        settings.skills.data_dir = td
        settings.skills.event_file = str(Path(td) / "a.json")
        settings.skills.memo_file = str(Path(td) / "m.json")
        from voice_loop.skills import Skills

        sk = Skills(settings)
        sk.store.save([])
        add = call("add_event", text="今晚十点提醒我练琴")

        check("还没记过东西时不掰（第一次说就是新建）",
              reroute_correction(add, "我说是今晚十点", sk), add)

        sk.handle("提醒我明天早上七点练琴")
        got = reroute_correction(add, "我说是今晚十点", sk)
        check("★刚记下一条 + 纯时间 → 改走 fix_last★",
              (got.get("function") or {}).get("name"), "fix_last")
        check("参数用用户原话",
              (got["function"]["arguments"])["text"], "我说是今晚十点")

        check("带别的内容就不掰（「九点提醒我写作业」是新的一条）",
              reroute_correction(call("add_event", text="九点提醒我写作业"),
                                 "九点提醒我写作业", sk).get("function", {}).get("name"),
              "add_event")
        check("查询类不碰",
              reroute_correction(call("list_events"), "今晚有什么提醒", sk).get(
                  "function", {}).get("name"), "list_events")


# --------------------------------------------------------------------------- #
def test_no_double_reminder() -> None:
    """同一句话既排了日程、又定了闹钟 → 闹钟是重复的，不该定。

    实测（2026-09-20）：「下周三下午3点，我有社团活动，到时候记得提醒我。」
    模型同时调 add_schedule 与 add_alarm，text 一模一样——日程本身带提前提醒，
    再来个闹钟就是同一件事响两次。
    """
    print("\n[7] 同一句话不重复记（同一工具被连着调两次）")
    with tempfile.TemporaryDirectory(prefix="route_dedup_") as td:
        tmp = Path(td)
        text = "下周三下午3点，我有社团活动，到时候记得提醒我。"
        loop = make_loop(tmp)
        loop.llm = FakeLLM("tool", [call("add_event", text=text),
                                    call("add_event", text=text)])
        stats = loop.respond(text)
        check("记上了", len(loop.skills.store.load()), 1)
        check("★第二遍没再多一条★", len(loop.skills.store.load()), 1)
        check("念的是「已记下」那句", "已记下" in stats.answer, True)
        loop.close()

        # 日程没排成时，闹钟照旧要定（不能因为「可能重复」就把事丢了）
        text2 = "明天早上七点叫我起床"
        loop = make_loop(tmp)
        loop.llm = FakeLLM("tool", [call("add_event", text=text2),
                                    call("add_event", text=text2)])
        stats = loop.respond(text2)
        check("照样只记一条（提醒也有去重）", len(loop.skills.store.load()), 1)
        loop.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 66)
    print(" 路由自测（假模型脚本化，不联网、不加载模型、不碰真实数据）")
    print("=" * 66)
    test_gate()
    test_model_first()
    test_rules_first()
    test_new_tools()
    test_repair_args()
    test_reroute_correction()
    test_no_double_reminder()
    print("\n" + "=" * 66)
    if _bad:
        print(f" 失败 {len(_bad)} 项（共 {_ok + len(_bad)}）：")
        for b in _bad:
            print(f"   - {b}")
    else:
        print(f" 全部通过 √（{_ok} 项）")
    print("=" * 66)
    return 1 if _bad else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
