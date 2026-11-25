"""角色思考与行动的全量本地追踪（JSONL）。

一条线 = 一轮对话；一行 = 一个事件。落在
``sessions/agent_trace/<角色>/YYYY-MM-DD.jsonl``（``sessions/`` 不进 git）。

★记什么★：用户输入、角色/模型、每轮模型实际输出的文本、工具调用及参数、
闸门判定、工具返回的完整内容、最终答复、usage、错误与打断。
★不记什么★：模型内部不可观测的隐藏思维链——API 只返还文本/工具调用，
我们只能忠实记录它"说出来/做出来"的部分，不编造思考过程。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class AgentTrace:
    def __init__(
        self,
        settings: Any,
        character: Any,
        *,
        mode: str,
        prompt: str = "",
        turn_prompt: str = "",
        persona: str = "",
        tools: list[dict] | None = None,
        model: str = "",
    ) -> None:
        root = Path(str(getattr(settings, "root", ".") or "."))
        app = getattr(settings, "app", None)
        configured = str(getattr(app, "sessions_dir", "") or "").strip()
        sessions = Path(configured) if configured else (root / "sessions")
        if not sessions.is_absolute():
            sessions = root / sessions
        cid = str(getattr(character, "id", "") or "default")
        day = datetime.now().strftime("%Y-%m-%d")
        self.path = sessions / "agent_trace" / cid / f"{day}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.turn_id = uuid.uuid4().hex[:12]
        self.character_id = cid
        self.character_name = str(getattr(character, "name", "") or "")
        self.base = {
            "turn_id": self.turn_id,
            "character": cid,
            "character_name": self.character_name,
            "mode": mode,
            "model": model,
        }
        names = []
        for tool in tools or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            names.append(str((fn or {}).get("name") or ""))
        self.write("turn/start", prompt=prompt, turn_prompt=turn_prompt,
                   persona=persona, tools=names)

    def write(self, kind: str, **payload: Any) -> None:
        row = {"ts": _now(), "kind": kind, **self.base, **payload}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass                      # 追踪失败绝不能影响这一轮回答

    def event(self, ev: dict) -> None:
        """把 harness 事件原样落盘（perf 时间换成可读时间）。"""
        row = dict(ev or {})
        if "at" in row:
            row["perf_at"] = row.pop("at")
        self.write("harness/" + str(row.pop("kind", "event")), **row)

    def finish(self, *, answer: str, ok: bool = True, interrupted: bool = False,
               rounds: int = 0, model_calls: int = 0, usages: list[dict] | None = None,
               stats: dict | None = None, calls: list[dict] | None = None) -> None:
        self.write("turn/end", answer=answer, ok=ok, interrupted=interrupted,
                   rounds=rounds, model_calls=model_calls,
                   usages=list(usages or []), stats=dict(stats or {}),
                   calls=list(calls or []))


__all__ = ["AgentTrace"]
