"""面板：★工具★（模型能 call 哪些、这些工具从哪来）。

一个页面回答三个问题：

1. **现在模型能 call 什么** —— 这一份必须问服务要（`tools` 命令）。
   只有服务知道当前角色是不是 API 引擎、`api_tools` 展开了什么、MCP 宿主里
   哪个服务器真的连上了、白名单挡掉了哪些、路由是不是 `chat`（那样一个工具都不给）。
   控制台照配置推算只会算出「应该是什么」。
2. **预设怎么换算** —— 角色卡里的 `api_tools` 写的是**预设名**（见
   `voice_loop/tool_presets.py`），这里把每个预设展开成真实工具名，
   写角色卡的时候不用去翻源码。
3. **哪些东西在拦着** —— `[[mcp.servers]]` 的白名单 / `hidden`，
   以及 `[agent] allow_write` / `allow_shell` 这两个默认关着的开关。

★本面板不加载任何服务器★：`build_server()` 里有的会去连网络（邮件要读身份），
控制台进程不该为了「列个清单」就把这些东西拉起来。所以静态部分只读配置，
真实清单按需问服务。
"""

from __future__ import annotations

from ..registry import Panel

PANEL = Panel(
    id="tools",
    title="工具",
    order=48,
    hint="模型能 call 哪些工具、来自哪个服务器、角色卡多开了什么（真实清单要服务在跑）",
)


def register(app, ctx) -> None:
    from fastapi import Body  # noqa: PLC0415

    from ... import tool_presets  # noqa: PLC0415

    @app.get("/api/tools")
    def api_tools():
        """静态部分：预设表、服务器配了什么、每个角色会多开哪些、安全开关。"""
        settings = ctx.settings
        mcp = getattr(settings, "mcp", None)
        agent = getattr(settings, "agent", None)
        api = getattr(settings, "api", None)
        agent_on = bool(getattr(agent, "enabled", True))

        servers = []
        for spec in getattr(mcp, "servers", None) or []:
            allow = [str(t) for t in (getattr(spec, "tools", None) or [])]
            servers.append({
                "name": str(getattr(spec, "name", "") or ""),
                "transport": str(getattr(spec, "transport", "inproc") or "inproc"),
                "enabled": bool(getattr(spec, "enabled", True)),
                "hidden": bool(getattr(spec, "hidden", False)),
                "namespace": bool(getattr(spec, "namespace", True)),
                "allowlist": allow,
                "timeout": float(getattr(spec, "timeout", 0.0) or 0.0),
                "module": str(getattr(spec, "module", "") or ""),
                "command": list(getattr(spec, "command", None) or []),
            })

        presets = [
            {"name": p["name"], "title": p["title"], "tools": p["tools"],
             "count": len(p["tools"])}
            for p in tool_presets.describe()
        ]

        global_extra = [str(x) for x in (getattr(api, "extra_tools", None) or [])]
        characters = []
        for char in _characters(ctx):
            engine = str(getattr(char, "engine", "") or getattr(api, "engine", "local"))
            own = [str(x) for x in (getattr(char, "api_tools", None) or [])]
            # ★跟流水线 _api_extra_tools 同一套规则★：角色自己没写就退回 [api] extra_tools
            entries = own or global_extra
            active = engine == "api" and agent_on
            characters.append({
                "id": getattr(char, "id", ""),
                "name": getattr(char, "name", "") or getattr(char, "id", ""),
                "engine": engine,
                "api_tools": own,
                "from_global": not own and bool(global_extra),
                "entries": [
                    {"name": name,
                     "kind": "preset" if name in {p["name"] for p in presets} else "tool",
                     "title": next((p["title"] for p in presets if p["name"] == name), "")}
                    for name in entries
                ],
                "expanded": sorted(tool_presets.expand(entries)) if active else [],
                "active": active,
                "why": "" if active else (
                    "本地引擎（Ollama）：只有默认那几个工具" if engine != "api"
                    else "[agent] enabled = false：没开 harness"),
            })

        return {
            "mcp": {
                "enabled": bool(getattr(mcp, "enabled", True)),
                "servers": servers,
            },
            "agent": {
                "enabled": agent_on,
                "max_rounds": int(getattr(agent, "max_rounds", 6) or 6),
                "allow_write": bool(getattr(agent, "allow_write", False)),
                "allow_shell": bool(getattr(agent, "allow_shell", False)),
                "workspace": str(getattr(agent, "workspace", ".") or "."),
                "max_tool_chars": int(getattr(agent, "max_tool_chars", 6000) or 6000),
            },
            "api": {
                "enabled": bool(getattr(api, "enabled", True)),
                "engine": str(getattr(api, "engine", "local") or "local"),
                "extra_tools": global_extra,
            },
            "presets": presets,
            "characters": characters,
            "runtime_tools": [
                {"name": "ask_user", "desc": "缺关键信息时向用户提问（harness 内置）"},
                {"name": "update_plan", "desc": "多步任务先写步骤（harness 内置）"},
            ],
        }

    @app.post("/api/tools/live")
    def api_tools_live(payload: dict = Body(default={})):
        """★真的问服务要一份★（服务没跑就明说，不拿静态推算冒充）。"""
        timeout = float((payload or {}).get("timeout") or 10.0)
        reply = ctx.call_service("tools", timeout=timeout)
        return {
            "ok": reply.ok,
            "error": reply.error,
            "seconds": round(reply.seconds, 2),
            "data": reply.data or {},
        }


def _characters(ctx):
    try:
        return list(ctx.characters().all())
    except Exception:  # noqa: BLE001 - 角色文件坏了不该让工具页白屏
        return []
