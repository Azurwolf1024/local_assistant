"""「日上下文连续 + 做梦整理 + 压缩」的离线自测（★不联网、不调模型★）。

为什么这些点值得一条条钉住：这套机制会**静默地改写记忆**（合并、换新、删），
也会**悄悄把提示词变短**。错了不会报错，只会「慢慢记错」或者「突然答不上刚才的话」。
所以这里全部用临时目录 + 假模型断言：

    · 段封存后旧文件字节不变（前缀缓存能命中的前提）
    · uid 去重、缺字段补全、`render` 的三段顺序与预算
    · 规则版做梦真的把上下文压成摘要、并且封段
    · agent 版拿假提议走完五道校验（pinned 保护 / 证据必须是对方说的 / NOOP）
    · `--undo` 整批回滚（episodes + facts + last_dream_seq + digest 都退回去）
    · 双时间：`supersede` 之后旧值不删，`include_invalid` 还能翻出来
    · 调度器五种触发（时间用 `tick(now=...)` 注入，不靠真等）

    python scripts\\test_memory_dream.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.memory import MemoryHub, apply_supersede  # noqa: E402
from voice_loop.memory.context import ContextLog, est_tokens, turn_uid  # noqa: E402
from voice_loop.memory.dream import DreamScheduler, Dreamer, undo  # noqa: E402
from voice_loop.memory.model import Fact, now_iso  # noqa: E402
from voice_loop.settings import MemoryContextConfig, MemoryDreamConfig  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def make_settings(tmp: Path, *, dream: MemoryDreamConfig | None = None,
                  context: MemoryContextConfig | None = None) -> SimpleNamespace:
    """一个够 `MemoryHub`/`Dreamer` 用的 settings 壳子（★不读真实 config★）。"""
    memory = SimpleNamespace(
        enabled=True, dir=str(tmp / "memory"), inject=True, max_episodes=800,
        max_facts=200, keep_session_days=30.0, keep_session_files=10,
        knowledge_paths=[str(tmp / "knowledge")], world_title="设定", world="",
        context=context or MemoryContextConfig(),
        dream=dream or MemoryDreamConfig(),
    )
    return SimpleNamespace(root=tmp, memory=memory, skills=SimpleNamespace(data_dir="data"),
                           persona=SimpleNamespace(file=str(tmp / "characters.json"),
                                                   default="baize"),
                           resolve=lambda p: Path(p) if Path(p).is_absolute() else Path(tmp) / p)


def make_hub(tmp: Path, *, dream: MemoryDreamConfig | None = None,
             context: MemoryContextConfig | None = None):
    settings = make_settings(tmp, dream=dream, context=context)
    hub = MemoryHub(settings, root=tmp / "memory")
    return settings, hub


def turns(count: int, *, day: str = "2026-11-19", character: str = "baize",
          log: ContextLog | None = None) -> list[dict]:
    rows = []
    base = datetime.fromisoformat(f"{day}T09:00:00")
    for i in range(count):
        stamp = (base + timedelta(minutes=3 * i)).isoformat(timespec="seconds")
        rows.append({"ts": stamp, "role": "user",
                     "text": f"第 {i} 句：我最近在调记忆系统，咖啡喝得有点多。"})
        rows.append({"ts": stamp, "role": "assistant",
                     "text": f"第 {i} 句回：那先少喝点，晚上别喝。"})
    return rows


# --------------------------------------------------------------------------- #
def test_context_segments(tmp: Path) -> None:
    print("\n[1] 段封存后旧文件不变（★前缀缓存能命中的前提★）")
    ctx = ContextLog(tmp, "baize", segment_tokens=60, segment_turns=4)
    ctx.append(turns(4))
    first = ctx.segment_paths()[0]
    before = first.read_bytes()
    check("第一段写出来了", first.is_file())
    check("到量后自动封段（段号 +1）", ctx.state()["seg"], 2)

    ctx.append([{"ts": f"2026-11-19T10:{i:02d}:00", "role": "user",
                 "text": f"后面第 {i} 句"} for i in range(4)])
    check("★封存的段一个字节都没变★", first.read_bytes() == before, True,
          detail=f"{len(before)} 字节")
    check("新内容进了新段", len(ctx.segment_paths()), 2)
    check("行数在长", len(ctx.turns()), 12)

    uid = turn_uid("2026-11-19T10:00:00", "user", "重复的一句", "s.jsonl")
    got = ctx.append([{"ts": "2026-11-19T10:00:00", "role": "user",
                       "text": "重复的一句", "session": "s.jsonl"}])
    again = ctx.append([{"ts": "2026-11-19T10:00:00", "role": "user",
                         "text": "重复的一句", "session": "s.jsonl"}])
    check("第一次记进去", got, 1)
    check("★同一句再 append 不会写两遍★", again, 0)
    check("uid 是内容哈希（同内容同 uid）",
          any(t.uid == uid for t in ctx.turns()))

    seqs = [t.seq for t in ctx.turns()]
    check("序号是递增的", seqs, sorted(seqs))
    check("序号没有重复", len(set(seqs)), len(seqs))


def test_render(tmp: Path) -> None:
    print("\n[2] render：事实 → 往日摘要 → 刚才聊的（★这个顺序就是缓存前缀顺序★）")
    ctx = ContextLog(tmp / "r", "baize", segment_tokens=10_000, segment_turns=1000)
    for i in range(6):
        ctx.append([{"ts": f"2026-11-19T11:0{i}:00", "role": "user",
                     "text": f"今天第 {i} 句" + "（很长的一句）" * 4},
                    {"ts": f"2026-11-19T11:0{i}:00", "role": "assistant",
                     "text": f"回应第 {i} 句"}])
    ctx.write_digest("2026-11-18", {"summary": "昨天在调记忆", "open_threads": ["还没测完"],
                                    "decisions": [], "turns": 12})
    text = ctx.render("事实：用户喜欢黑咖啡", digest_days=3, tail_tokens=40)
    pos_facts = text.find("黑咖啡")
    pos_digest = text.find("昨天在调记忆")
    pos_tail = text.find("今天第")
    check("三块都在", all(p >= 0 for p in (pos_facts, pos_digest, pos_tail)))
    check("★顺序：事实 < 摘要 < 原文★", pos_facts < pos_digest < pos_tail)
    check("带了「没聊完」", "还没测完" in text)
    tail_rows = ctx._tail(40)                       # noqa: SLF001 - 只测私有预算函数
    check("尾预算生效（没把 12 条全塞进来）", 0 < len(tail_rows) < 12, True,
          detail=f"{len(tail_rows)} 条")
    check("★不切半句★（留下的都是完整的一轮）",
          all(r.text.startswith(("今天第", "回应第")) for r in tail_rows))
    check("est_tokens 中文≈一字一 token", est_tokens("一二三四五"), 5)


def test_rule_dream(tmp: Path) -> None:
    print("\n[3] 规则版做梦：抽事件 + 写摘要 + 封段 + 记账")
    settings, hub = make_hub(tmp)
    mem = hub.for_character("baize")
    mem.context.append([
        {"ts": "2026-11-19T09:00:00", "role": "user",
         "text": "记住我明天早上七点要开会，别忘了叫我。"},
        {"ts": "2026-11-19T09:00:10", "role": "assistant", "text": "好，我记着了。"},
        {"ts": "2026-11-19T09:05:00", "role": "user", "text": "我最近喜欢喝黑咖啡，不加糖。"},
        {"ts": "2026-11-19T09:05:10", "role": "assistant", "text": "记下了。"},
        {"ts": "2026-11-19T09:09:00", "role": "user", "text": "刚才说到哪了？"},
    ])
    dreamer = Dreamer(settings, hub, "baize")
    report = dreamer.run(day="2026-11-19", source="rule")
    check("梦做成了", report.ok, True)
    check("走的是规则版", report.source, "rule")
    check("读到的轮数", report.turns, 5)
    check("写了一天摘要", "2026-11-19" in report.digest.get("summary", "")
          or bool(report.digest.get("summary")))
    check("摘要落盘了", mem.context.digests_dir.joinpath("2026-11-19.json").is_file())
    check("★封了新段★（段号往前走了）", mem.context.state()["seg"], 2)
    check("记账：last_dream_seq 往前走了", mem.context.state()["last_dream_seq"] > 0)
    check("★已经整理过的不会再算一遍★", len(mem.context.new_turns()), 0)
    check("留下了可撤销的前镜像", bool(report.record) and Path(report.record).is_file())

    again = dreamer.run(day="2026-11-19", source="rule")
    check("没新内容时拒绝做梦", again.ok, False)
    check("拒绝的理由说得清楚", "没有新内容" in again.error, True)

    forced = dreamer.run(day="2026-11-19", source="rule", force=True)
    check("★--force 也不做空梦★（“今天聊了 0 轮”会污染摘要）", forced.ok, False)
    check("空梦的原因说得清楚", "没有新内容" in forced.error, True)
    digests = list(mem.context.digests_dir.glob("*.json"))
    check("没有多出第二个摘要", len(digests), 1)


def test_noop_without_context(tmp: Path) -> None:
    print("\n[4] 空上下文：不做梦、不写文件")
    settings, hub = make_hub(tmp)
    mem = hub.for_character("baize")
    report = Dreamer(settings, hub, "baize").run(day="2026-11-19")
    check("没内容就是失败（不写空摘要）", report.ok, False)
    check("digests 里没东西", list(mem.context.digests_dir.glob("*.json")), [])
    check("dreams 里没东西", list(mem.context.dreams_dir.glob("*.json")), [])


# --------------------------------------------------------------------------- #
class FakeLlm:
    """假模型：把「提交一份提议」演成一次 done() 工具调用（不联网）。"""

    def __init__(self, proposal: dict) -> None:
        self.proposal = proposal
        self.engine = "api"
        self.turns = 0

    def configure(self, engine: str, *, model: str = "", **kw) -> str:
        self.engine = engine
        return engine

    def build_messages(self, user_text: str, *, extra_messages=None,
                       prefix_messages=None, system_override=None) -> list[dict]:
        return [{"role": "system", "content": system_override or "sys"},
                {"role": "user", "content": user_text}]

    def chat_events(self, *, messages=None, tools=None, model=None, num_ctx=None, **kw):
        self.turns += 1
        if self.turns == 1:
            yield {"tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "done",
                "arguments": json.dumps(self.proposal, ensure_ascii=False)}}]}
        else:
            yield {"delta": "整理完了。"}


def _write_persona(tmp: Path) -> None:
    """agent 版会去读角色名（只为写进提示词）——给一个最小的角色索引。"""
    path = tmp / "characters.json"
    path.write_text(json.dumps({
        "default": "baize",
        "characters": [{"id": "baize", "name": "白泽", "engine": "api",
                        "persona": "你是白泽。"}],
    }, ensure_ascii=False), encoding="utf-8")


def test_agent_dream(tmp: Path) -> None:
    print("\n[5] agent 版：模型提交提议 → 确定性代码逐条校验后执行")
    _write_persona(tmp)
    dream_cfg = MemoryDreamConfig(model="api:fake-model", min_turns=2, max_actions=24)
    settings, hub = make_hub(tmp, dream=dream_cfg)
    mem = hub.for_character("baize")
    # 先把一条身份事实钉住：梦里的 forget 不许动它
    mem._upsert_facts([Fact(key="user.称呼", value="老板", about="user", pinned=True,
                            evidence=9, confidence=1.0)])
    mem.context.append([
        {"ts": "2026-11-19T09:00:00", "role": "user", "text": "我在做一个语音助手的记忆系统。"},
        {"ts": "2026-11-19T09:00:10", "role": "assistant", "text": "听起来有意思。"},
        {"ts": "2026-11-19T09:05:00", "role": "user", "text": "以后都叫我队长吧。"},
    ])
    seqs = {t.seq: t for t in mem.context.new_turns()}
    user_seqs = [s for s, t in seqs.items() if t.role == "user"]
    tool_seqs = [s for s, t in seqs.items() if t.role == "assistant"]

    proposal = {
        "digest": {"summary": "今天聊了记忆系统，他让我叫他队长。",
                   "open_threads": ["记忆还没测完"], "decisions": ["称呼改成队长"]},
        "actions": [
            {"op": "promote", "key": "user.称呼.偏好", "value": "队长",
             "evidence_seq": user_seqs[0], "why": "他明确要求"},
            # ★这条必须被拦下★：证据是助手说的，不是用户说的
            {"op": "promote", "key": "user.瞎编", "value": "熬夜",
             "evidence_seq": tool_seqs[-1], "why": "从工具输出里拿的"},
            # ★pinned 保护★
            {"op": "promote", "key": "user.称呼", "value": "同志", "why": "改称呼"},
            # ★NOOP★：已经记过
            {"op": "promote", "key": "user.称呼.偏好", "value": "队长",
             "evidence_seq": user_seqs[0], "why": "重复"},
            {"op": "insight", "text": "他最近把精力放在记忆机制上。", "confidence": 0.7,
             "why": "从两句里归纳"},
        ],
    }
    settings.memory.dream = dream_cfg
    report = Dreamer(settings, hub, "baize", llm_factory=lambda _c: FakeLlm(proposal)).run(
        day="2026-11-19")
    check("agent 版做成了", report.source, "agent")
    check("摘要用了模型给的", "队长" in report.digest.get("summary", ""))
    keys = {f.key for f in mem.facts()}
    check("★合法的事实提升进去了★", "user.称呼.偏好" in keys)
    check("★证据不是用户说的 → 拦下★", "user.瞎编" not in keys)
    check("★pinned 事实没被改写★",
          [f.value for f in mem.facts() if f.key == "user.称呼"], ["老板"])
    check("★重复的 promote 判 NOOP★",
          any("NOOP" in s for s in report.skipped), True)
    check("归纳的 insight 进了 L2",
          any(e.kind == "insight" for e in mem.episodes()), True)
    check("被拦下的都记在 skipped 里", len(report.skipped) >= 3)


def test_undo(tmp: Path) -> None:
    print("\n[6] --undo：整批回滚（episodes / facts / last_dream_seq / digest）")
    _write_persona(tmp)
    dream_cfg = MemoryDreamConfig(model="api:fake-model", min_turns=1)
    settings, hub = make_hub(tmp, dream=dream_cfg)
    mem = hub.for_character("baize")
    mem.context.append([
        {"ts": "2026-11-19T09:00:00", "role": "user", "text": "我以后都喝美式。"},
        {"ts": "2026-11-19T09:00:10", "role": "assistant", "text": "好。"},
    ])
    seq = [t for t in mem.context.new_turns() if t.role == "user"][0].seq
    proposal = {"digest": {"summary": "他改喝美式了"}, "actions": [
        {"op": "promote", "key": "user.饮料", "value": "美式", "evidence_seq": seq,
         "why": "他说的"}]}
    report = Dreamer(settings, hub, "baize", llm_factory=lambda _c: FakeLlm(proposal)).run(
        day="2026-11-19")
    check("先记进去了", "user.饮料" in {f.key for f in mem.facts()})
    rec = Path(report.record)
    check("前镜像在", rec.is_file())
    got = undo(settings, hub, "baize", "2026-11-19")
    check("回滚成功", got.get("ok"), True)
    check("★事实退回去了★", {f.key for f in mem.facts()}, set())
    check("★last_dream_seq 退回去了★", mem.context.state()["last_dream_seq"], 0)
    check("★摘要删掉了★", (mem.context.digests_dir / "2026-11-19.json").is_file(), False)
    check("★前镜像自己也清掉了（不能重复回滚）★", rec.is_file(), False)
    check("回滚后再查：还是没记过", got.get("episodes"), 0)


def test_supersede(tmp: Path) -> None:
    print("\n[7] 双时间：换新之后旧值不删（「我以前喝什么」还答得上来）")
    _, hub = make_hub(tmp)
    mem = hub.for_character("baize")
    mem._upsert_facts([Fact(key="user.咖啡", value="拿铁", about="user",
                            evidence=3, confidence=0.8)])
    fresh = mem.supersede_fact("user.咖啡", "美式", note="他说改了")
    check("换新成功", fresh is not None and fresh.value, "美式")
    current = [f.value for f in mem.facts() if f.key == "user.咖啡"]
    check("★当前值是新的★", current, ["美式"])
    allof = [f for f in mem.facts(include_invalid=True) if f.key == "user.咖啡"]
    check("★旧值还在，只是标了失效★", [f.value for f in allof], ["拿铁", "美式"])
    check("失效的那条有 invalid_at", bool(allof[0].invalid_at))
    check("失效的那条指向新值", allof[0].superseded_by, "user.咖啡=美式")
    check("pinned 不许这样换", apply_supersede(
        [Fact(key="user.名字", value="老王", pinned=True)], "user.名字", "小王"), None)


# --------------------------------------------------------------------------- #
def test_scheduler(tmp: Path) -> None:
    print("\n[8] 调度：到量 / 夜间随机窗口 / 空闲 / 模型主动请求（时间注入，不真等）")

    def build(name: str, **cfg_kw):
        room = tmp / name
        room.mkdir(parents=True, exist_ok=True)
        dream_cfg = MemoryDreamConfig(enabled=True, window="02:00-05:00", random=False,
                                      check_seconds=1, min_turns=1,
                                      min_interval_minutes=0, **cfg_kw)
        ctx_cfg = MemoryContextConfig(segment_tokens=10_000, segment_turns=1000)
        settings, hub = make_hub(room, dream=dream_cfg, context=ctx_cfg)
        made = {"n": 0}

        def _make(cid: str) -> Dreamer:
            made["n"] += 1
            return Dreamer(settings, hub, cid)

        sched = DreamScheduler(settings, hub, make_dreamer=_make,
                               character=lambda: "baize", activity=lambda: 0.0)
        return settings, hub, sched, made

    # ① 到量：只做规则压缩
    settings, hub, sched, _ = build("tokens", at_tokens=40, idle_minutes=0)
    mem = hub.for_character("baize")
    mem.context.append(turns(3))
    got = sched.tick(now=datetime(2026, 11, 19, 14, 0))
    check("到量触发了一次", got is not None and got.ok, True)
    check("★到量走规则版（不调模型）★", got.source, "rule")
    check("整理完就没新内容了", len(mem.context.new_turns()), 0)

    # ② 夜间窗口：先挑好时间，再把表拨过去
    settings, hub, sched, _ = build("window", at_tokens=0, idle_minutes=0)
    mem = hub.for_character("baize")
    mem.context.append(turns(2, day="2026-11-20"))
    off = sched.tick(now=datetime(2026, 11, 20, 0, 30))
    check("窗口还没到 → 不做", off, None)
    plan_path = Path(settings.memory.dir) / "_dream_schedule.json"
    plan = json.loads(plan_path.read_text("utf-8"))
    check("今天挑好了时间", plan.get("date"), "2026-11-20")
    check("挑的时间在窗口里", "02:00" <= str(plan.get("at")) <= "05:00", True,
          detail=str(plan.get("at")))
    night = sched.tick(now=datetime(2026, 11, 20, 4, 59))
    check("到点后做了一次", night is not None and night.ok, True)
    check("★一天只做一次★", json.loads(plan_path.read_text("utf-8")).get("done"), True)

    # ③ 空闲
    settings, hub, sched, _ = build("idle", at_tokens=0, idle_minutes=20,
                                    idle_min_turns=2)
    mem = hub.for_character("baize")
    mem.context.append(turns(2, day="2026-11-21"))
    sched.activity = lambda: 1.0
    idle = sched.tick(now=datetime(2026, 11, 21, 14, 0))
    check("空闲够久也能触发", idle is not None and idle.ok, True)

    # ④ 模型主动请求（写请求文件 → 下一轮被读走）
    settings, hub, sched, _ = build("tool", at_tokens=0, idle_minutes=0)
    mem = hub.for_character("baize")
    mem.context.append(turns(2, day="2026-11-22"))
    req = Path(settings.memory.dir) / "_dream_requests.jsonl"
    req.write_text(json.dumps({"who": "baize", "reason": "用户让我整理",
                               "at": 0}, ensure_ascii=False) + "\n", encoding="utf-8")
    tool = sched.tick(now=datetime(2026, 11, 22, 14, 0))
    check("request_dream 触发了", tool is not None and tool.ok, True)
    check("请求文件被消费掉", req.is_file(), False)

    # ⑤ 手动 request()（预留给「随机时间唤醒」）
    settings, hub, sched, made = build("manual", at_tokens=0, idle_minutes=0)
    mem = hub.for_character("baize")
    mem.context.append(turns(2, day="2026-11-23"))
    sched.request(reason="她自己想整理一下", source="manual")
    manual = sched.tick(now=datetime(2026, 11, 23, 14, 0))
    check("手动 request 也能触发", manual is not None and manual.ok, True)
    check("调度器每轮都会去拿 dreamer", made["n"] >= 1, True)
    check("此刻不该有排队里的请求了", sched._queue, [])          # noqa: SLF001


def test_cache_accounting(tmp: Path) -> None:
    print("\n[9] 缓存记账：真实 usage 记下来，能看出命中率")
    ctx = ContextLog(tmp / "c", "baize")
    ctx.record_usage(prompt_tokens=10_000, hit_tokens=9_000)
    ctx.record_usage(prompt_tokens=2_000, hit_tokens=0)
    stats = ctx.cache_stats()
    check("prompt 累加", stats["prompt"], 12_000)
    check("hit 累加", stats["hit"], 9_000)
    check("调用次数", stats["calls"], 2)
    check("命中率", stats["ratio"], 0.75)


def test_sync_context(tmp: Path) -> None:
    print("\n[10] 上下文也要上云（段按 uid 合并，不整段覆盖）")
    from voice_loop.memory import sync as msync

    _, hub = make_hub(tmp)
    mem = hub.for_character("baize")
    mem.context.append(turns(2))
    settings = make_settings(tmp)
    repo = tmp / "cloud"
    (repo / ".git").mkdir(parents=True)
    sync = msync.MemorySync(settings, clone_dir=repo)
    report = msync.SyncReport(action="export")
    sync._copy_char("baize", report)                       # noqa: SLF001
    got = report.characters["baize"]
    check("段文件镜到云端了", got["context"] > 0, True)
    check("state.json 也上去了",
          (repo / "memory" / "baize" / "context" / "state.json").is_file(), True)
    check("★锁文件不上云★",
          not list((repo / "memory" / "baize" / "context").rglob("*.lock")))

    # 云端多一段、本地没有 → 拉回来
    cloud_ctx = repo / "memory" / "baize" / "context"
    local_ctx = mem.context
    extra = {"uid": "deadbeefdeadbeef", "seq": 999, "ts": "2026-11-24T10:00:00",
             "role": "user", "text": "云端多出来的这一句", "session": "x.jsonl",
             "tools": [], "tokens": 12}
    seg = cloud_ctx / "segments" / "seg-000009.jsonl"
    seg.write_text(json.dumps(extra, ensure_ascii=False) + "\n", encoding="utf-8")
    merged = sync._merge_context(local_ctx.dir.parent, cloud_ctx.parent,  # noqa: SLF001
                                dry_run=False)
    check("合并说「变了」", merged["changed"], True)
    texts = [t.text for t in local_ctx.turns()]
    check("云端的行进来了", "云端多出来的这一句" in texts, True)
    check("本地的行没丢", len(texts) >= 3, True)


def test_pipeline_hooks(tmp: Path) -> None:
    print("\n[11] 流水线接线：每轮写进上下文 / 提示词带三段 / 缓存能记账")
    import logging

    from voice_loop.pipeline import TurnStats, VoiceLoop

    settings, hub = make_hub(tmp)
    loop = VoiceLoop.__new__(VoiceLoop)          # ★不初始化真语音链路★：只测这几个钩子
    loop.settings = settings
    loop.memory_hub = hub
    loop.character = SimpleNamespace(id="baize")
    loop.log = logging.getLogger("voice_loop.test")
    loop._session_file = tmp / "session-20261119-090000.jsonl"
    loop._last_activity = 0.0
    loop.llm = SimpleNamespace(engine="api")      # 云端那档预算（本地那档见下面单独断言）

    loop._append_context(TurnStats(turn=1, user_text="我昨天说想吃火锅。",
                                   answer="那今晚去吃？", total_seconds=4.0))
    mem = hub.for_character("baize")
    texts = [t.text for t in mem.context.turns()]
    check("用户那句进了上下文", "我昨天说想吃火锅。" in texts)
    check("助手那句也进了", "那今晚去吃？" in texts)
    loop._write_session(TurnStats(turn=2, user_text="第二句。", answer="好。"))
    check("记了活动时间（空闲触发要看它）", loop._last_activity > 0, True)
    after_two = len(mem.context.turns())
    check("第二轮也进去了", after_two, len(texts) + 2)

    loop._record_usage({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800})
    check("★云端 usage 记进缓存账★", mem.context.cache_stats()["ratio"], 0.8)
    loop._record_usage({"prompt_tokens": 500,
                        "prompt_tokens_details": {"cached_tokens": 500}})
    check("OpenAI 系字段也认", mem.context.cache_stats()["hit"], 1300)

    block = loop._memory_context("火锅")
    check("提示词里带了刚才那两句",
          "我昨天说想吃火锅。" in block and "那今晚去吃？" in block, True)
    loop.llm = SimpleNamespace(engine="local")
    check("★本地那档预算更小（同一份上下文，提示词更短）★",
          len(loop._memory_context("火锅")) <= len(block), True)

    # 再写一次**同一轮** → 不该长
    loop._write_session(TurnStats(turn=1, user_text="我昨天说想吃火锅。",
                                  answer="那今晚去吃？", total_seconds=4.0))
    check("★同一轮重复写不会进两遍★", len(mem.context.turns()), after_two)
    check("原始日志照写（去重只作用在上下文上，日志留给排查）",
          len(loop._session_file.read_text("utf-8").strip().splitlines()), 2)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vl-dream-"))
    print("=" * 70)
    print(" 记忆：日上下文连续 / 做梦整理 / 压缩 —— 离线自测")
    print("=" * 70)
    try:
        for i, fn in enumerate((test_context_segments, test_render, test_rule_dream,
                                test_noop_without_context, test_agent_dream, test_undo,
                                test_supersede, test_scheduler, test_cache_accounting,
                                test_sync_context, test_pipeline_hooks), start=1):
            room = tmp / f"case{i}"
            room.mkdir(parents=True, exist_ok=True)
            fn(room)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f"× {len(_failures)} 项没通过：")
        for name in _failures:
            print(f"  · {name}")
        return 1
    print("√ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
