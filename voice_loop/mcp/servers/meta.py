"""渐进式发现用的元工具服务器（仅 API 角色点名时可见）。

模型每轮固定看到 core 工具 + 这 4 个工具；需要长尾能力时：
    tool_groups → tool_search → tool_describe → tool_invoke
工具定义本身不进 tools 数组，只在工具结果里追加，缓存前缀保持稳定。

★发现 ≠ 授权★：tool_invoke 走 MCPHost.call(allowed=...)，和原生工具调用同一道闸门。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from ..server import MCPServer

NAME = "meta"

INSTRUCTIONS = (
    "工具发现：找不到合适工具时先 tool_search，必要时 tool_describe，"
    "再用 tool_invoke。能搜到不代表能调用，权限不足时会明确拒绝。"
)


def build_server(settings=None, logger=None, *, host: Any = None, **_ignored) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

    def allowed() -> set[str] | None:
        got = getattr(host, "meta_allowed", None)
        return set(got) if got is not None else None

    def render_cards(cards: list[dict], *, with_allowed: bool = True) -> str:
        lines = []
        for c in cards:
            flag = "" if not with_allowed else ("  [可用]" if c.get("allowed") else "  [未授权]")
            lines.append(f"{c['name']}  ({c['group']}/{c['risk']}){flag}\n  "
                         f"{c['summary'][:120]}")
        return "\n".join(lines) or "没有匹配的工具。"

    def tool_groups(_args: dict) -> dict:
        rows = host.catalog(allowed=allowed()) if host is not None else []
        counts = Counter(r["group"] for r in rows)
        lines = [f"{g}: {n}" for g, n in sorted(counts.items())]
        return {"text": "\n".join(lines) or "工具目录是空的。", "is_error": False}

    def tool_search(args: dict) -> dict:
        if host is None:
            return {"text": "Error: 工具目录没接上", "is_error": True}
        query = str((args or {}).get("query") or "").strip().lower()
        group = str((args or {}).get("group") or "").strip().lower()
        try:
            limit = max(1, min(20, int((args or {}).get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        rows = host.catalog(allowed=allowed())
        if group:
            rows = [r for r in rows if r["group"] == group]
        if query:
            words = [w for w in query.split() if w]

            def score(row: dict) -> int:
                text = " ".join([row["name"], row["summary"], " ".join(row["tags"])]).lower()
                return sum(3 if w in row["name"].lower() else (1 if w in text else 0)
                           for w in words)

            rows = [r for r in rows if score(r) > 0]
            rows.sort(key=lambda r: -score(r))
        return {"text": render_cards(rows[:limit]), "is_error": False}

    def tool_describe(args: dict) -> dict:
        if host is None:
            return {"text": "Error: 工具目录没接上", "is_error": True}
        name = str((args or {}).get("name") or "").strip()
        card = host.tool_describe(name)
        if card is None:
            return {"text": f"没有这个工具：{name}", "is_error": True}
        return {"text": json.dumps(card, ensure_ascii=False, indent=2), "is_error": False}

    def tool_invoke(args: dict) -> dict:
        if host is None:
            return {"text": "Error: 工具目录没接上", "is_error": True}
        name = str((args or {}).get("name") or "").strip()
        arguments = (args or {}).get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"text": "Error: arguments 必须是对象", "is_error": True}
        log.info(f"[mcp:meta.invoke] {name}({arguments})")
        ok, text = host.invoke(name, arguments, allowed=allowed())
        return {"text": text, "is_error": not ok}

    server.add_tool("tool_groups", "列出可用工具分组与数量（渐进式发现第 1 步）。",
                    {"type": "object", "properties": {}}, tool_groups)
    server.add_tool("tool_search", "按名字/描述搜工具，返回 name/group/risk/一句话说明。",
                    {"type": "object", "properties": {
                        "query": {"type": "string", "description": "要找什么能力"},
                        "group": {"type": "string", "description": "限定分组，如 mail/shell/browser"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                                  "description": "最多几条（默认 5）"},
                    }}, tool_search)
    server.add_tool("tool_describe", "查看一个工具的完整参数 schema、副作用与示例。",
                    {"type": "object", "properties": {
                        "name": {"type": "string", "description": "tool_search 找到的工具名"},
                    }, "required": ["name"]}, tool_describe)
    server.add_tool("tool_invoke", "调用一个当前角色可用的工具（参数和原生工具一致）。",
                    {"type": "object", "properties": {
                        "name": {"type": "string", "description": "要调用的工具名"},
                        "arguments": {"type": "object", "description": "按 tool_describe 的 schema 填"},
                    }, "required": ["name"]}, tool_invoke)
    return server
