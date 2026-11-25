"""角色思考/行动全量追踪自测：JSONL 落盘 + harness 事件带完整内容。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.agent_trace import AgentTrace      # noqa: E402
from voice_loop.harness import HarnessAgent        # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, want=True, detail="") -> None:
    ok = got == want
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


class FakeLlm:
    def __init__(self, script):
        self.script = script
        self.n = 0

    def build_messages(self, user_text, extra_messages=None, system_override=None):
        return [{"role": "user", "content": user_text}]

    def chat_events(self, messages, tools=None, model=None, num_ctx=None):
        step = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        for ev in step:
            yield ev


def settings(max_history: int = 24000):
    return SimpleNamespace(agent=SimpleNamespace(
        max_rounds=4, max_tool_chars=6000, max_history_chars=max_history,
    ))


def tool_call(name, args, cid):
    return {"id": cid, "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": args},
    }]}


def test_trace_file(tmp: Path) -> None:
    print("\n[1] AgentTrace：一行一事件，全量写盘")
    s = SimpleNamespace(root=str(tmp))
    trace = AgentTrace(s, SimpleNamespace(id="fish", name="海鱼"),
                       mode="harness", prompt="今天有什么安排",
                       turn_prompt="今天有什么安排\n运行信息", persona="你是海鱼",
                       tools=[{"function": {"name": "list_events"}}], model="deepseek-chat")
    trace.event({"kind": "step/end", "at": 1.23, "step": 1, "text": "我查一下。"})
    trace.event({"kind": "tool/result", "tool": "list_events", "ok": True,
                 "content": "19:30 排练"})
    trace.event({"kind": "llm/usage", "usage": {"prompt_cache_hit_tokens": 70}})
    trace.finish(answer="今晚 19:30 排练。", rounds=1, model_calls=2,
                 usages=[{"prompt_cache_hit_tokens": 70}],
                 calls=[{"id": "c1", "function": {"name": "list_events"}}])
    path = tmp / "sessions" / "agent_trace" / "fish"
    files = sorted(path.glob("*.jsonl"))
    check("文件按角色/日期落盘", len(files) == 1, True, str(files))
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    check("turn/start 记了原始输入与提示词", kinds[0] == "turn/start"
          and rows[0]["prompt"] == "今天有什么安排"
          and rows[0]["turn_prompt"].startswith("今天有什么安排\n"), True)
    check("工具名与模型记了", rows[0]["tools"] == ["list_events"]
          and rows[0]["model"] == "deepseek-chat", True)
    check("step/end 带完整文本",
          any(r["kind"] == "harness/step/end" and r.get("text") == "我查一下。"
              for r in rows), True)
    check("tool/result 带完整内容",
          any(r["kind"] == "harness/tool/result" and r.get("content") == "19:30 排练"
              for r in rows), True)
    check("turn/end 带最终答复/usage/工具调用",
          rows[-1]["kind"] == "turn/end" and rows[-1]["answer"] == "今晚 19:30 排练。"
          and rows[-1]["usages"][0]["prompt_cache_hit_tokens"] == 70
          and rows[-1]["calls"][0]["id"] == "c1", True)


def test_harness_events() -> None:
    print("\n[2] harness：模型文本 / 工具结果 / usage 都带全量字段")
    llm = FakeLlm([
        [tool_call("search", {"q": "天气"}, "c1"),
         {"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 70}}],
        [{"delta": "今天晴。", "delta2": "真的"}],
    ])
    # delta2 不是标准字段，换成两条 delta
    llm.script[1] = [{"delta": "今天晴。"}, {"delta": "真的"}]
    agent = HarnessAgent(llm, settings(), call_tool=lambda n, a: (True, "晴天"),
                         plan_tool=False)
    result = agent.run("今天天气？")
    kinds = {ev["kind"] for ev in result.events}
    check("有 llm/usage 事件", "llm/usage" in kinds, True, str(sorted(kinds)))
    check("step/end 带 text", any(
        ev.get("kind") == "step/end" and "text" in ev and "今天晴" in str(ev.get("text"))
        for ev in result.events), True)
    check("tool/result 带 content", any(
        ev.get("kind") == "tool/result" and ev.get("content") == "晴天"
        for ev in result.events), True)
    check("usage 也进了 AgentResult.usages", len(result.usages) == 1, True,
          str(result.usages))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent_trace_") as raw:
        test_trace_file(Path(raw))
    test_harness_events()
    print("\n" + "=" * 60)
    if _failures:
        print("  追踪自测失败：" + "、".join(_failures))
        return 1
    print("  追踪自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
