"""MCP 宿主：把若干个 MCP 服务器聚合成「模型能看的一份工具清单」，再把调用路由回去。

它在架构里的位置就是 Claude Code / Copilot 里那个「MCP host」：
    模型只能看见一份工具列表 → 列表里的每个名字都能被路由回某个服务器。

命名规则（很重要，是为了模型好选工具）：
    - **自家能力域（`namespace = false`）不带前缀**：名字保持 list_events / add_memo……
      这样模型已经认得的 8 个名字不变（实测 4b 在这 8 个上 8/8），
      TOOL_HINT、评估脚本、老测试也全部照旧。
    - **外来的服务器带 `mcp__<服务器>__<工具>` 前缀**（跟 Claude Code 一致），
      防止两个服务器撞名。

守卫（这是语音助手的底线，写在宿主里而不是各服务器里）：
    1. `tools` 白名单：没写进白名单的工具**根本不出现在模型面前**，
       模型看不见就不可能调（比「调用时再拒绝」安全得多）。
    2. 单个服务器崩了/超时：只让这一路失败，其余继续，并且自动重启进程。
"""

from __future__ import annotations

import logging
import time

from .client import MCPClient, make_transport
from .protocol import MCPError, ToolSpec

SEP = "__"


class MCPHost:
    """管理多个 MCP 服务器连接。"""

    def __init__(
        self,
        settings,
        logger: logging.Logger | None = None,
        deps: dict | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop")
        self.deps = deps or {}
        self._clients: dict[str, MCPClient] = {}
        self._routes: dict[str, tuple[str, str]] = {}   # 模型看到的名字 -> (服务器, 服务器内的名字)
        self._specs: list[ToolSpec] = []
        # ★白名单之外的工具也留一份★：API 角色可以按 `api_tools` 点名多开几个，
        # 本地角色照旧只看白名单（见 host.specs(include=...)）。
        self._all_routes: dict[str, tuple[str, str]] = {}
        self._all_specs: dict[str, ToolSpec] = {}
        self._stats: dict[str, dict] = {}
        self.calls = 0
        self.errors = 0

    # ------------------------------------------------------------------ 起停
    @property
    def enabled(self) -> bool:
        if not bool(getattr(self.settings, "enabled", True)):
            return False
        return bool(getattr(self.settings, "servers", None))

    def start(self) -> None:
        """启动所有启用的服务器（第一次要工具清单时懒调用）。"""
        if not self.enabled or self._clients:
            return
        for spec in self.settings.servers:
            if not getattr(spec, "enabled", True):
                self.log.info(f"[mcp] 跳过已禁用的服务器：{spec.name}")
                continue
            try:
                client = MCPClient(
                    name=spec.name,
                    transport=make_transport(spec, self.log, self.deps),
                    timeout=float(getattr(spec, "timeout", 0) or 15.0),
                    log=self.log,
                )
                client.start()
                tools = client.list_tools()
            except Exception as exc:  # noqa: BLE001 - 一个服务器起不来不能拖垮助手
                self.log.warning(f"[mcp] 服务器 {spec.name} 起不来，跳过：{exc}")
                continue
            self._clients[spec.name] = client
            allow = {str(t) for t in (getattr(spec, "tools", None) or [])}
            hidden_server = bool(getattr(spec, "hidden", False))
            prefix = "" if getattr(spec, "namespace", True) is False else f"mcp{SEP}{spec.name}{SEP}"
            kept = 0
            for tool in tools:
                exposed = f"{prefix}{tool.name}"
                spec_obj = ToolSpec(
                    name=exposed, description=tool.description,
                    input_schema=tool.input_schema,
                )
                self._all_routes[exposed] = (spec.name, tool.name)
                self._all_specs[exposed] = spec_obj
                if hidden_server:
                    continue          # 默认不露出：等 api_tools / extra_tools 点名
                if allow and tool.name not in allow:
                    continue
                self._routes[exposed] = (spec.name, tool.name)
                self._specs.append(spec_obj)
                kept += 1
            self._stats[spec.name] = {
                "tools": kept, "available": len(tools),
                "transport": getattr(spec, "transport", "inproc"),
                "allowlist": bool(allow),
                "hidden": hidden_server,
            }
            hidden = len(tools) - kept
            self.log.info(
                f"[mcp] {spec.name}：露出 {kept}/{len(tools)} 个工具"
                + ("（hidden：等 API 角色点名）" if hidden_server
                   else (f"（白名单挡掉 {hidden} 个）" if hidden else ""))
            )

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"[mcp] 关闭 {client.name} 出错：{exc}")
        self._clients.clear()
        self._routes.clear()
        self._specs.clear()
        self._all_routes.clear()
        self._all_specs.clear()

    # ------------------------------------------------------------- 给模型看
    def specs(self, include=()) -> list[dict]:
        """Ollama / OpenAI 形状的工具定义（白名单 + 点名多开的那些）。

        ``include`` 是 API 角色额外点名的工具（预设已展开成工具名）。
        名字不在任何服务器里就跳过 —— 上层会按「点名了但没找到」打日志。
        """
        self.start()
        out = [s.to_openai() for s in self._specs]
        have = {s.name for s in self._specs}
        for name in include or ():
            name = str(name)
            if name in have:
                continue
            extra = self._all_specs.get(name)
            if extra is not None:
                out.append(extra.to_openai())
                have.add(name)
        return out

    def names(self, include=()) -> set[str]:
        self.start()
        out = set(self._routes)
        out.update(str(n) for n in (include or ()) if str(n) in self._all_specs)
        return out

    def missing(self, include=()) -> list[str]:
        """点名了、但没有任何服务器提供这些工具（给上层打警告用）。"""
        self.start()
        return [str(n) for n in (include or ()) if str(n) not in self._all_specs]

    def handleable(self, name: str) -> bool:
        self.start()
        return str(name) in self._routes or str(name) in self._all_routes

    # --------------------------------------------------------------- 调用
    def call(self, name: str, arguments: dict | None = None) -> tuple[bool, str]:
        """调一个工具，返回 ``(ok, 文本)``。名字必须是 :meth:`specs` 里露出的。"""
        self.start()
        route = self._routes.get(str(name)) or self._all_routes.get(str(name))
        if route is None:
            self.errors += 1
            return False, f"没有这个工具：{name}"
        server_name, tool = route
        client = self._clients.get(server_name)
        if client is None:
            self.errors += 1
            return False, f"服务器 {server_name} 没连上"
        self.calls += 1
        t0 = time.perf_counter()
        ok, text = client.call(tool, arguments or {})
        if not ok:
            self.errors += 1
            self.log.warning(f"[mcp] {server_name}.{tool} 失败：{text[:120]}")
            # 子进程挂了就顺手拉起来，别让后面每一句都失败
            if not getattr(client.transport, "alive", lambda: True)():
                self.log.warning(f"[mcp] {server_name} 的子进程已退出，重启")
                try:
                    client.close()
                    client.transport = make_transport(
                        next(s for s in self.settings.servers if s.name == server_name),
                        self.log,
                        self.deps,
                    )
                    client.start()
                except Exception as exc:  # noqa: BLE001
                    self.log.error(f"[mcp] 重启 {server_name} 失败：{exc}")
        self.log.info(f"[mcp:{server_name}.{tool}] {time.perf_counter() - t0:.2f}s "
                      f"{'√' if ok else '×'} {text[:60]}")
        return ok, text

    def call_dict(self, call: dict) -> tuple[bool, str]:
        """直接吃模型给的那种 tool_call 结构。"""
        fn = call.get("function") or call
        name = str(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            import json

            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        return self.call(name, dict(args or {}))

    # --------------------------------------------------------------- 状态
    def describe(self) -> list[dict]:
        """给 CLI / 自检用：每个工具的「名字 → 来自哪台服务器」清单。"""
        self.start()
        out = []
        for spec in self._specs:
            server, tool = self._routes.get(spec.name, ("", ""))
            out.append(
                {
                    "name": spec.name,
                    "server": server,
                    "tool": tool,
                    "description": spec.description,
                    "namespaced": server in self._stats
                    and spec.name != tool,
                }
            )
        return out

    def status(self) -> str:
        """一行话说明现在的工具来自哪儿（给 selftest / 启动横幅用）。"""
        self.start()
        if not self._clients:
            return "未配置 MCP 服务器"
        parts = []
        for name, info in self._stats.items():
            flag = "" if info["tools"] == info["available"] else f"/{info['available']}"
            parts.append(f"{name}({info['transport']},{info['tools']}{flag})")
        return f"{len(self._specs)} 个工具 ← " + " ".join(parts)
