"""Harness 循环自测：Turn/Step 事件、pre-execute 闸门、取消语义、去重与异常。

    python scripts\\tests\\test_harness.py

不联网、不依赖真实 LLM：用脚本化的 chat_events 驱动整条循环。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.harness import HarnessAgent  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


class FakeLlm:
    def __init__(self, script: list[list[dict]]) -> None:
        self.script = script
        self.n = 0

    def build_messages(self, user_text, extra_messages=None, system_override=None):
        return [{"role": "user", "content": user_text}]

    def chat_events(self, messages, tools=None, model=None, num_ctx=None):
        step = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        for ev in step:
            yield ev


class BoomLlm(FakeLlm):
    def chat_events(self, messages, tools=None, model=None, num_ctx=None):
        raise RuntimeError("API 断了")
        yield {}  # pragma: no cover - 让它成为生成器


def settings(max_history: int = 24000):
    return SimpleNamespace(agent=SimpleNamespace(
        max_rounds=6, max_tool_chars=6000, max_history_chars=max_history,
    ))


def tool_call(name: str, args: dict, cid: str) -> dict:
    return {"id": cid, "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": args},
    }]}


def test_turn_step() -> None:
    print("\n[1] Turn/Step 事件 + 工具结果回灌")
    llm = FakeLlm([
        [tool_call("search", {"q": "天气"}, "c1")],
        [{"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 70}},
         {"delta": "今天晴。"}],
    ])
    seen: list[tuple[str, dict]] = []

    def call_tool(name, args):
        seen.append((name, args))
        return True, "晴天"

    agent = HarnessAgent(llm, settings(), call_tool=call_tool, plan_tool=False,
                         on_event=lambda ev: seen.append((ev["kind"], {})))
    result = agent.run("今天天气？")
    check("最终回答", result.text == "今天晴。", result.text)
    check("工具真执行了", ("search", {"q": "天气"}) in seen)
    kinds = [ev["kind"] for ev in result.events]
    for kind in ("turn/start", "step/start", "tool/pre-execute", "tool/call",
                 "tool/result", "step/end", "turn/end"):
        check(f"有 {kind} 事件", kind in kinds)
    check("工具结果进了 messages",
          any(m.get("role") == "tool" and m.get("content") == "晴天"
              for m in result.messages))
    check("记下了上下文前缀长度", result.initial_messages == 1,
          str(result.initial_messages))
    check("usage 事件被收下（缓存记账用）",
          result.usages == [{"prompt_tokens": 100, "prompt_cache_hit_tokens": 70}],
          str(result.usages))
    check("ok=True", result.ok)


def test_guard_and_cancel() -> None:
    print("\n[2] pre-execute 闸门 + ask_user 取消语义")
    llm = FakeLlm([
        [tool_call("danger", {}, "c1")],
        [{"delta": "我不做。"}],
    ])
    called: list[str] = []
    agent = HarnessAgent(
        llm, settings(), call_tool=lambda n, a: (called.append(n), (True, "x"))[1],
        guard=lambda n, a: (False, "禁止这项"), plan_tool=False,
    )
    r = agent.run("试试")
    check("★闸门拒绝后工具没执行★", called == [], str(called))
    check("拒绝理由喂回模型",
          any("权限闸门拒绝" in str(m.get("content")) for m in r.messages))
    check("ok=False", not r.ok)

    llm2 = FakeLlm([
        [tool_call("ask_user", {"question": "选哪个？"}, "c1")],
        [{"delta": "好吧。"}],
    ])
    agent2 = HarnessAgent(llm2, settings(), ask_user=lambda q, o: "（用户取消了）",
                          plan_tool=False)
    r2 = agent2.run("选一下")
    check("★取消不再是成功回答★", not r2.ok)
    check("取消时如实回模型",
          any("用户没有回答" in str(m.get("content")) for m in r2.messages))


def test_dedupe_budget_and_error() -> None:
    print("\n[3] 重复调用 / 历史预算 / 模型异常 / 打断")
    llm = FakeLlm([
        [tool_call("read_file", {"path": "a"}, "c1")],
        [tool_call("read_file", {"path": "a"}, "c2")],
        [{"delta": "完。"}],
    ])
    n = {"calls": 0}

    def call_tool(name, args):
        n["calls"] += 1
        return True, "x" * 5000

    agent = HarnessAgent(llm, settings(max_history=2000), call_tool=call_tool,
                         plan_tool=False)
    r = agent.run("读两次")
    check("★相同调用只执行一次★", n["calls"] == 1, str(n))
    check("有 skipped 事件", any(e["kind"] == "tool/skipped" for e in r.events))
    check("★早期工具原文被压缩★",
          any("早期结果已压缩" in str(m.get("content")) for m in r.messages))

    bad = HarnessAgent(BoomLlm([]), settings(), call_tool=lambda n, a: ("", True),
                       plan_tool=False)
    r2 = bad.run("会断线")
    check("★模型异常不抛出★", r2.text == "" and not r2.ok)
    check("有 llm/error 事件", any(e["kind"] == "llm/error" for e in r2.events))

    stopped = HarnessAgent(FakeLlm([[{"delta": "不该出现"}]]), settings(),
                           should_stop=lambda: True, plan_tool=False)
    r3 = stopped.run("打断")
    check("★打断后不留文本★", r3.interrupted and r3.text == "", r3.text)


def main() -> int:
    test_turn_step()
    test_guard_and_cancel()
    test_dedupe_budget_and_error()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  harness 自测：{len(_failures)} 条没过：" + "、".join(_failures[:6]))
        return 1
    print("  harness 自测：全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
