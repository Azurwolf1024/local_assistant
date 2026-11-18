"""Harness agent 运行时（API 角色专用）。

这一层只做「模型 ↔ 工具」的循环，不碰麦克风 / TTS：

    messages = llm.build_messages(...)
    for round in range(max_rounds):
        model 输出文字 + tool_calls
        如果有 tool_calls：执行、把结果作为 tool 消息喂回去
        如果没有：这就是最终回答
    （轮数到顶时再问一次，不给工具，强制收口）

工具执行、提问回调、停止判断都由外面注入，所以它既能被语音管线用，
也能被自测单独驱动。``ask_user`` 与 ``update_plan`` 是运行时自带的两个工具。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

ASK_USER_SPEC = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "向用户提一个问题并等待回答。缺少关键信息、需要确认、或要在几个选项里选一个时用。"
            "问题要短、一次只问一件事；有选项就放进 options。"
            "★只有在真的需要用户决定时才问，不要为了确认而确认。★"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选项（可选；最多 6 个）",
                },
            },
            "required": ["question"],
        },
    },
}

UPDATE_PLAN_SPEC = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "更新当前任务的步骤计划。多步任务（查资料、改文件、逐项确认）时用；"
            "每步一句短中文。计划只用于让用户知道进展，不会自动执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按顺序的步骤（最多 12 步）",
                },
            },
            "required": ["steps"],
        },
    },
}


@dataclass
class AgentResult:
    text: str = ""
    rounds: int = 0                  # 真正执行了工具的轮数
    model_calls: int = 0             # 模型调用次数（含最后的收口）
    calls: list[dict] = field(default_factory=list)
    ok: bool = True
    interrupted: bool = False
    first_token: float = 0.0
    seconds: float = 0.0
    plan: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


@dataclass
class _Call:
    id: str
    name: str
    arguments: dict
    message: dict


class HarnessAgent:
    def __init__(
        self,
        llm,
        settings,
        logger: logging.Logger | None = None,
        *,
        tool_specs: list[dict] | None = None,
        call_tool: Callable[[str, dict], tuple[bool, str]] | None = None,
        ask_user: Callable[[str, list[str]], str] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop")
        self.call_tool = call_tool
        self.ask_user = ask_user
        self.should_stop = should_stop or (lambda: False)
        cfg = getattr(settings, "agent", None)
        self.max_rounds = max(1, int(getattr(cfg, "max_rounds", 6) or 6))
        self.max_tool_chars = max(500, int(getattr(cfg, "max_tool_chars", 6000) or 6000))
        self.tools = list(tool_specs or [])
        if ask_user is not None:
            self.tools.append(ASK_USER_SPEC)
        self.tools.append(UPDATE_PLAN_SPEC)
        self.plan: list[str] = []

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _to_call(raw: dict, round_no: int, index: int) -> _Call:
        fn = raw.get("function") or raw
        name = str(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args_obj = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args_obj = {}
        elif isinstance(args, dict):
            args_obj = args
        else:
            args_obj = {}
        if not isinstance(args_obj, dict):
            args_obj = {}
        call_id = str(raw.get("id") or f"call_{round_no}_{index}")
        message = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args_obj, ensure_ascii=False),
            },
        }
        return _Call(id=call_id, name=name, arguments=args_obj, message=message)

    def _clip(self, text: str) -> str:
        text = str(text or "")
        if len(text) > self.max_tool_chars:
            text = text[: self.max_tool_chars - 1].rstrip() + "…"
        return text

    def _execute(self, call: _Call) -> tuple[bool, str, str]:
        """返回 ``(ok, 喂回模型的文本, 给日志的一句话)``。"""
        if call.name == "ask_user":
            question = str(call.arguments.get("question") or "").strip()
            options = [str(x).strip() for x in (call.arguments.get("options") or [])
                       if str(x).strip()][:6]
            if not question:
                return False, "ask_user 缺少 question。", "缺少问题"
            answer = self.ask_user(question, options) if self.ask_user else "（当前不能提问）"
            answer = str(answer or "").strip() or "（用户没有回答）"
            return True, f"用户回答：{answer}", f"问：{question[:40]} → {answer[:40]}"
        if call.name == "update_plan":
            steps = [str(x).strip() for x in (call.arguments.get("steps") or [])
                     if str(x).strip()][:12]
            if not steps:
                return False, "update_plan 需要至少一个步骤。", "空计划"
            self.plan = steps
            return True, "计划已更新：" + " → ".join(steps), f"{len(steps)} 步"
        if self.call_tool is None:
            return False, "当前没有可用的工具执行器。", "没有执行器"
        try:
            ok, text = self.call_tool(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - 工具失败也要让模型知道
            return False, f"工具调用失败：{type(exc).__name__}: {exc}", "异常"
        return bool(ok), self._clip(text), self._clip(text)[:60]

    # ------------------------------------------------------------------- 循环
    def run(
        self,
        user_text: str,
        *,
        extra_messages: list[dict] | None = None,
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> AgentResult:
        t0 = time.perf_counter()
        result = AgentResult()
        messages = self.llm.build_messages(user_text, extra_messages=extra_messages)
        result.messages = messages
        calls_all: list[dict] = []
        final_text = ""
        ok_all = True
        tool_rounds = 0

        for _ in range(self.max_rounds):
            if self.should_stop():
                result.interrupted = True
                break
            result.model_calls += 1
            text_parts: list[str] = []
            calls: list[dict] = []
            for ev in self.llm.chat_events(
                messages=messages, tools=self.tools, model=model, num_ctx=num_ctx
            ):
                if self.should_stop():
                    result.interrupted = True
                    break
                if "tool_calls" in ev:
                    calls.extend(ev["tool_calls"])
                    continue
                piece = ev.get("delta", "")
                if piece:
                    if result.first_token == 0.0:
                        result.first_token = time.perf_counter() - t0
                    text_parts.append(piece)
            if result.interrupted:
                break
            text = "".join(text_parts).strip()
            if not calls:
                final_text = text
                break

            # ---- 执行工具并把结果喂回模型 ----
            tool_rounds += 1
            normalized = [self._to_call(c, tool_rounds, i) for i, c in enumerate(calls)]
            messages.append({
                "role": "assistant",
                "content": text,
                "tool_calls": [c.message for c in normalized],
            })
            for call in normalized:
                ok, content, note = self._execute(call)
                ok_all = ok_all and ok
                calls_all.append(call.message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })
                self.log.info(f"[agent] {call.name}({call.arguments}) -> "
                              f"{'√' if ok else '×'} {note}")
        else:
            # 轮数到顶：再问一次，不给工具，逼它用已有资料收口
            result.model_calls += 1
            text_parts = []
            for ev in self.llm.chat_events(
                messages=messages, tools=None, model=model, num_ctx=num_ctx
            ):
                if self.should_stop():
                    result.interrupted = True
                    break
                if "delta" in ev:
                    text_parts.append(ev["delta"])
            final_text = "".join(text_parts).strip()

        result.text = final_text
        result.rounds = tool_rounds
        result.calls = calls_all
        result.ok = ok_all
        result.plan = list(self.plan)
        result.seconds = time.perf_counter() - t0
        return result
