"""工具目录元数据（渐进式发现用）。

目标：让新增工具模块只声明一张卡片，而不用改一个巨大的 harness 预设。

发现只是"看得见"，不是"允许调用"。真正的权限仍然由 ``MCPHost.call(allowed=...)``
和 harness 的 guard 判定；这里只负责把工具按组、风险、副作用整理成紧凑卡片。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .protocol import ToolSpec

# 模块可以调用 register_card() 往这里加自己的元数据；没登记的工具也有兜底卡片。
_CARDS: dict[str, dict[str, Any]] = {}


def register_card(name: str, **meta: Any) -> None:
    """声明式注册工具卡片（新模块用这个，别改预设清单）。"""
    _CARDS[str(name)] = dict(meta)


@dataclass
class ToolCard:
    name: str
    group: str = "other"
    summary: str = ""
    risk: str = "low"
    side_effects: str = "read"
    requires: str = ""
    tags: list[str] = field(default_factory=list)
    allowed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "group": self.group, "summary": self.summary,
            "risk": self.risk, "side_effects": self.side_effects,
            "requires": self.requires, "tags": list(self.tags),
            "allowed": self.allowed,
        }


_GROUP_RULES = [
    (re.compile(r"^(list_events|next_event|add_event|change_event|fix_last|now|list_memos|add_memo)$"), "schedule"),
    (re.compile(r"^(read_file|list_dir|glob|grep|write_file|edit_file)$"), "files"),
    (re.compile(r"^(web_search|web_fetch|weather)$"), "web"),
    (re.compile(r"^mail_"), "mail"),
    (re.compile(r"^mcp__memory__"), "memory"),
    (re.compile(r"^(run_command|job_output|job_list|job_kill)$"), "shell"),
    (re.compile(r"^(end_listening|ask_user|update_plan)$"), "control"),
    (re.compile(r"^(tool_groups|tool_search|tool_describe|tool_invoke)$"), "meta"),
]

_HIGH = re.compile(r"(send|delete|remove|kill|run_command|shell|eval_js)", re.I)
_MEDIUM = re.compile(r"(write|edit|change|add_|update_plan)", re.I)


def card_for(spec: ToolSpec, *, allowed: bool = True) -> ToolCard:
    name = spec.name
    meta = dict(_CARDS.get(name) or {})
    group = str(meta.pop("group", "") or "")
    if not group:
        group = next((g for pat, g in _GROUP_RULES if pat.search(name)), "other")
    summary = str(meta.pop("summary", "") or "").strip()
    if not summary:
        summary = re.sub(r"\s+", " ", str(spec.description or "")).strip()[:120]
    risk = str(meta.pop("risk", "") or "")
    if not risk:
        risk = "high" if _HIGH.search(name) else ("medium" if _MEDIUM.search(name) else "low")
    effects = str(meta.pop("side_effects", "") or "write" if risk != "low" else "read")
    tags = list(meta.pop("tags", []) or [])
    return ToolCard(
        name=name, group=group, summary=summary, risk=risk,
        side_effects=effects, requires=str(meta.pop("requires", "") or ""),
        tags=tags, allowed=bool(allowed),
    )


def validate_arguments(schema: dict | None, args: Any) -> str:
    """轻量 JSON-schema 校验：不给未声明工具留参数幻觉的口子。"""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "arguments 必须是对象"
    schema = schema if isinstance(schema, dict) else {}
    for key in schema.get("required") or []:
        if key not in args:
            return f"缺少必填参数：{key}"
    props = schema.get("properties") or {}
    for key, value in args.items():
        rule = props.get(key)
        if not isinstance(rule, dict):
            return f"未声明的参数：{key}"
        kind = rule.get("type")
        ok = True
        if kind == "string":
            ok = isinstance(value, str)
        elif kind == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif kind == "boolean":
            ok = isinstance(value, bool)
        elif kind == "array":
            ok = isinstance(value, list)
        elif kind == "object":
            ok = isinstance(value, dict)
        if not ok:
            return f"参数 {key} 类型不对，期望 {kind}"
        if "minimum" in rule and isinstance(value, (int, float)) and value < rule["minimum"]:
            return f"参数 {key} 小于最小值 {rule['minimum']}"
        if "maximum" in rule and isinstance(value, (int, float)) and value > rule["maximum"]:
            return f"参数 {key} 大于最大值 {rule['maximum']}"
    return ""


__all__ = ["ToolCard", "card_for", "register_card", "validate_arguments"]
