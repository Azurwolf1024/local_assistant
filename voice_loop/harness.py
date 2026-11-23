"""Harness agent 运行时（API 角色专用，形状对齐 DeepSeek Harness）。

一个 **Turn** = 本轮用户任务；里面若干 **Step**，每个 Step = 一次模型请求 +
它发起的所有工具调用。循环本身只是驱动器，权限、可观测、结果加工都挂在事件点上：

    turn/start → step/start → llm/stream → tool/pre-execute → tool/call
              → tool/result → step/end → … → turn/end

与 DSH 对应：
    - ``guard`` = tools/pre-execute 的权限闸门（默认放行，拒绝必须给理由）；
    - ``on_event`` = 可插拔观察者（日志 / 控制台轨迹 / 自测都订阅它）；
    - 事件留在 :attr:`AgentResult.events`，模型看到什么就记录什么。

这一层只做「模型 ↔ 工具」的循环，不碰麦克风 / TTS：工具执行、提问回调、
停止判断都由外面注入。``ask_user`` 与 ``update_plan`` 是运行时自带的两个工具。
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
    messages: list[dict] = field(default_factory=list)  # 完整轨迹（含 system / user / tool）
    initial_messages: int = 0   # build_messages 造出的前缀长度；后面全是这一轮新增的
    events: list[dict] = field(default_factory=list)  # Turn/Step/工具事件轨迹


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
        plan_tool: bool = True,
        guard: Callable[[str, dict], tuple[bool, str]] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop")
        self.call_tool = call_tool
        self.ask_user = ask_user
        self.guard = guard                    # tools/pre-execute 闸门
        self.on_event = on_event              # 可插拔观察者
        self.should_stop = should_stop or (lambda: False)
        cfg = getattr(settings, "agent", None)
        self.max_rounds = max(1, int(getattr(cfg, "max_rounds", 6) or 6))
        self.max_tool_chars = max(500, int(getattr(cfg, "max_tool_chars", 6000) or 6000))
        self.max_history_chars = max(2000, int(
            getattr(cfg, "max_history_chars", 24000) or 24000))
        self.tools = list(tool_specs or [])
        if ask_user is not None:
            self.tools.append(ASK_USER_SPEC)
        # ★「做梦」不许规划★：它只要一份结构化的整理提议，多给一个 update_plan 只会
        # 让它把步骤写进计划里然后忘了交提议（见 memory/dream.py）。
        if plan_tool:
            self.tools.append(UPDATE_PLAN_SPEC)
        self.plan: list[str] = []

    # ------------------------------------------------------------------ 事件
    def _emit(self, result: AgentResult, kind: str, **payload) -> None:
        """模型可见的一切都留痕；观察者拿到的就是同一份事件（DSH 的做法）。"""
        ev = {"kind": kind, "at": round(time.perf_counter(), 3), **payload}
        result.events.append(ev)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception as exc:  # noqa: BLE001 - 观察者不该拖垮循环
                self.log.debug(f"[agent] on_event 出错：{exc}")

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

    def _trim_history(self, messages: list[dict]) -> None:
        """只压早期的 tool 原文，不动最近一轮和 system/user（DSH 的上下文预算）。"""
        total = sum(len(str(m.get("content") or "")) for m in messages
                    if m.get("role") == "tool")
        if total <= self.max_history_chars:
            return
        Budget = self.max_history_chars
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content") or "")
            if len(content) <= 200:
                continue
            keep = max(200, Budget // 4)
            if len(content) > keep:
                msg["content"] = content[:keep].rstrip() + "…（早期结果已压缩）"
            Budget -= len(str(msg["content"]))
            if Budget <= 0:
                break

    def _execute(self, call: _Call) -> tuple[bool, str, str]:
        """返回 ``(ok, 喂回模型的文本, 给日志的一句话)``。"""
        # tools/pre-execute：闸门只拒不放，拒绝理由原样喂回模型
        if self.guard is not None:
            try:
                allowed, why = self.guard(call.name, call.arguments)
                if not allowed:
                    return False, f"工具被权限闸门拒绝：{why}", "被拒绝"
            except Exception as exc:  # noqa: BLE001
                return False, f"权限闸门出错：{type(exc).__name__}: {exc}", "闸门异常"
        if call.name == "ask_user":
            question = str(call.arguments.get("question") or "").strip()
            options = [str(x).strip() for x in (call.arguments.get("options") or [])
                       if str(x).strip()][:6]
            if not question:
                return False, "ask_user 缺少 question。", "缺少问题"
            try:
                answer = self.ask_user(question, options) if self.ask_user else "（当前不能提问）"
            except Exception as exc:  # noqa: BLE001 - 问用户失败也要让模型知道
                return False, f"提问失败：{type(exc).__name__}: {exc}", "提问异常"
            answer = str(answer or "").strip() or "（用户没有回答）"
            # ★取消 / 没听清 / 没回调 ≠ 用户回答★（以前一律 ok=True 误导模型）
            negative = ("（用户取消了）", "（没有听清）", "（当前不能提问）",
                        "（用户没有回答）")
            if answer.startswith(negative):
                return False, f"用户没有回答：{answer}", answer[:40]
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
        clipped = self._clip(text)
        return bool(ok), clipped, clipped[:60]

    # ------------------------------------------------------------------- 循环
    def run(
        self,
        user_text: str,
        *,
        extra_messages: list[dict] | None = None,
        model: str | None = None,
        num_ctx: int | None = None,
        system_override: str | None = None,
    ) -> AgentResult:
        t0 = time.perf_counter()
        result = AgentResult()
        messages = self.llm.build_messages(user_text, extra_messages=extra_messages,
                                           system_override=system_override)
        result.messages = messages
        result.initial_messages = len(messages)
        self._emit(result, "turn/start", user_text=user_text[:200])
        calls_all: list[dict] = []
        final_text = ""
        ok_all = True
        tool_rounds = 0
        seen: set[tuple[str, str]] = set()

        for round_no in range(1, self.max_rounds + 1):
            if self.should_stop():
                result.interrupted = True
                break
            result.model_calls += 1
            self._emit(result, "step/start", step=round_no)
            text_parts: list[str] = []
            calls: list[dict] = []
            try:
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
            except Exception as exc:  # noqa: BLE001 - 云端断线不得把整轮对话带崩
                ok_all = False
                self._emit(result, "llm/error", step=round_no,
                           error=f"{type(exc).__name__}: {exc}")
                self.log.warning(f"[agent] 模型调用失败（第 {round_no} 步）：{exc}")
                break
            if result.interrupted:
                break
            text = "".join(text_parts).strip()
            self._emit(result, "step/end", step=round_no,
                       text_chars=len(text), tool_calls=len(calls))
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
                self._emit(result, "tool/pre-execute", tool=call.name,
                           arguments=call.arguments)
                key = (call.name, json.dumps(call.arguments, sort_keys=True,
                                             ensure_ascii=False))
                if key in seen:
                    ok, content, note = False, "（相同调用已执行过，跳过）", "重复调用"
                    self._emit(result, "tool/skipped", tool=call.name)
                else:
                    seen.add(key)
                    ok, content, note = self._execute(call)
                    self._emit(result, "tool/call", tool=call.name,
                               arguments=call.arguments, ok=ok)
                ok_all = ok_all and ok
                calls_all.append(call.message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })
                self._emit(result, "tool/result", tool=call.name, ok=ok,
                           chars=len(content))
                self.log.info(f"[agent] {call.name}({call.arguments}) -> "
                              f"{'√' if ok else '×'} {note}")
            self._trim_history(messages)
        else:
            # 轮数到顶：再问一次，不给工具，逼它用已有资料收口
            result.model_calls += 1
            self._emit(result, "step/start", step=self.max_rounds + 1, closeout=True)
            text_parts = []
            try:
                for ev in self.llm.chat_events(
                    messages=messages, tools=None, model=model, num_ctx=num_ctx
                ):
                    if self.should_stop():
                        result.interrupted = True
                        break
                    if "delta" in ev:
                        text_parts.append(ev["delta"])
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                self._emit(result, "llm/error", step=self.max_rounds + 1,
                           error=f"{type(exc).__name__}: {exc}")
                self.log.warning(f"[agent] 收口调用失败：{exc}")
            final_text = "".join(text_parts).strip()
            self._emit(result, "step/end", step=self.max_rounds + 1,
                       closeout=True, text_chars=len(final_text))

        # ★打断后不拼残留文本★（以前靠 pipeline 再清一次，太脆弱）
        if result.interrupted:
            final_text = ""
        self._emit(result, "turn/end", rounds=tool_rounds,
                   text_chars=len(final_text), ok=ok_all)
        result.text = final_text
        result.rounds = tool_rounds
        result.calls = calls_all
        result.ok = ok_all
        result.plan = list(self.plan)
        result.seconds = time.perf_counter() - t0
        return result
