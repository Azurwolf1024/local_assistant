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
from .discovery import card_for, validate_arguments
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
        self.deps["host"] = self      # 给 inproc 的 meta 服务器用（发现/统一调用）
        self.meta_allowed: set[str] | None = None   # 本轮角色可见工具，由 pipeline 设置
        self._clients: dict[str, MCPClient] = {}
        self._routes: dict[str, tuple[str, str]] = {}   # 模型看到的名字 -> (服务器, 服务器内的名字)
        self._specs: list[ToolSpec] = []
        # ★白名单之外的工具也留一份★：API 角色可以按 `api_tools` 点名多开几个，
        # 本地角色照旧只看白名单（见 host.specs(include=...)）。
        self._all_routes: dict[str, tuple[str, str]] = {}
        self._all_specs: dict[str, ToolSpec] = {}
        self._stats: dict[str, dict] = {}
        self._fails: dict[str, int] = {}
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
            self._register(spec.name, spec, tools)

    def _register(self, server_name: str, spec, tools) -> None:
        """把一台服务器的工具写进路由表（start 和重启共用同一段逻辑）。"""
        allow = {str(t) for t in (getattr(spec, "tools", None) or [])}
        hidden_server = bool(getattr(spec, "hidden", False))
        # ★bool() 而不是 is False★：TOML 写 0 / "" 也该关掉前缀
        prefix = "" if not bool(getattr(spec, "namespace", True)) else f"mcp{SEP}{server_name}{SEP}"
        # 先摘掉旧路由，重启后工具集变化才不会留下幽灵条目。
        doomed = {n for n, (owner, _t) in self._all_routes.items() if owner == server_name}
        self._all_specs = {n: s for n, s in self._all_specs.items() if n not in doomed}
        self._specs = [s for s in self._specs if s.name not in doomed]
        for bucket in (self._routes, self._all_routes):
            for exposed in doomed:
                bucket.pop(exposed, None)
        kept = 0
        for tool in tools:
            exposed = f"{prefix}{tool.name}"
            spec_obj = ToolSpec(
                name=exposed, description=tool.description,
                input_schema=tool.input_schema,
            )
            self._all_routes[exposed] = (server_name, tool.name)
            self._all_specs[exposed] = spec_obj
            if hidden_server:
                continue          # 默认不露出：等 api_tools / extra_tools 点名
            if allow and tool.name not in allow:
                continue
            self._routes[exposed] = (server_name, tool.name)
            self._specs.append(spec_obj)
            kept += 1
        prev = self._stats.get(server_name, {})
        self._stats[server_name] = {
            "tools": kept, "available": len(tools),
            "transport": getattr(spec, "transport", "inproc"),
            "allowlist": bool(allow), "hidden": hidden_server,
            "restarts": int(prev.get("restarts", 0)),
        }
        hidden = len(tools) - kept
        self.log.info(
            f"[mcp] {server_name}：露出 {kept}/{len(tools)} 个工具"
            + ("（hidden：等 API 角色点名）" if hidden_server
               else (f"（白名单挡掉 {hidden} 个）" if hidden else ""))
        )

    def _restart(self, server_name: str, client: MCPClient, *, reason: str) -> bool:
        """重建 transport + 路由表 + 状态；只让这一路短暂中断。"""
        try:
            spec = next(s for s in self.settings.servers if s.name == server_name)
            client.close()
            client.transport = make_transport(spec, self.log, self.deps)
            client.start()
            tools = client.list_tools()
            self._register(server_name, spec, tools)
            self._stats[server_name]["restarts"] += 1
            self._fails[server_name] = 0
            self.log.warning(f"[mcp] {server_name} 已重启（{reason}），工具清单已重建")
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[mcp] 重启 {server_name} 失败：{exc}")
            return False

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
        self._fails.clear()

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

    # ----------------------------------------------------------- 渐进式发现
    def catalog(self, *, allowed=None, include=()) -> list[dict]:
        """所有工具的紧凑卡片（含 hidden），按组/名字排序。

        ``allowed`` 只决定卡片上的 ``allowed`` 标记：能搜到 ≠ 能调用。
        """
        self.start()
        names = set(self._all_specs)
        names.update(str(n) for n in (include or ()) if str(n) in self._all_specs)
        visible = {str(n) for n in allowed} if allowed is not None else None
        cards = [card_for(self._all_specs[name], allowed=(
            visible is None or name in visible)) for name in names]
        cards = [c for c in cards if c.group != "meta"]
        cards.sort(key=lambda c: (c.group, c.name))
        return [c.to_dict() for c in cards]

    def tool_describe(self, name: str) -> dict | None:
        """一个工具的完整卡片 + JSON schema（只查，不执行）。"""
        self.start()
        spec = self._all_specs.get(str(name or ""))
        if spec is None:
            return None
        card = card_for(spec)
        return {**card.to_dict(), "input_schema": spec.input_schema,
                "description": spec.description}

    def invoke(self, name: str, arguments: dict | None = None, *, allowed=None) -> tuple[bool, str]:
        """元工具统一调用入口：先校验参数，再走原来的 call()/权限闸门。"""
        self.start()
        name = str(name or "")
        spec = self._all_specs.get(name)
        if spec is None:
            return False, f"没有这个工具：{name}"
        why = validate_arguments(spec.input_schema, arguments)
        if why:
            return False, f"参数不合法：{why}"
        return self.call(name, arguments, allowed=allowed)

    def handleable(self, name: str, *, allowed=None) -> bool:
        self.start()
        name = str(name)
        # ★allowed = 当前角色这一轮真正看得见的工具名★
        # 不传时保持老行为（CLI / 自测）；传了就绝不越界。
        if allowed is not None and name not in {str(n) for n in allowed}:
            return False
        return name in self._routes or name in self._all_routes

    # --------------------------------------------------------------- 调用
    def call(
        self, name: str, arguments: dict | None = None, *, allowed=None
    ) -> tuple[bool, str]:
        """调一个工具，返回 ``(ok, 文本)``。

        ``allowed`` 是本轮可见工具名集合（来自 :meth:`names`）。本地角色不传 hidden 的
        名字，即使模型幻觉出一个也不放行；API 角色点名过的 hidden 工具才会进来。
        """
        self.start()
        name = str(name)
        if not self.handleable(name, allowed=allowed):
            self.errors += 1
            return False, (f"这个角色不能调用：{name}" if allowed is not None
                           else f"没有这个工具：{name}")
        route = self._routes.get(name) or self._all_routes.get(name)
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
        if ok:
            self._fails[server_name] = 0
        else:
            self.errors += 1
            self._fails[server_name] = self._fails.get(server_name, 0) + 1
            self.log.warning(f"[mcp] {server_name}.{tool} 失败：{text[:120]}")
            alive = getattr(client.transport, "alive", None)
            dead = alive is not None and not alive()
            # 卡住但没退出的进程：连续两次失败也拉起来，别让后面每句都耗满 timeout
            stuck = alive is not None and self._fails[server_name] >= 2
            if dead or stuck:
                self._restart(server_name, client,
                              reason="子进程退出" if dead else "连续超时")
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
            restarts = f"*restart:{info['restarts']}" if info.get("restarts") else ""
            parts.append(f"{name}({info['transport']},{info['tools']}{flag}{restarts})")
        return f"{len(self._specs)} 个工具 ← " + " ".join(parts)
