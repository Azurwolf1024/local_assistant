"""MCP 服务器侧：注册工具 + 跑 stdio 循环。

一个服务器 = 一个能力域（日程/备忘/提醒是一个，看图是一个，……）。
每个服务器都是普通的 Python 模块，可以：
    1. 被自己的助手进程内调用（transport = "inproc"，零进程开销）
    2. 被 `python -m voice_loop.mcp.serve <服务器名>` 挂在 stdio 上，
       给别的 agent（Copilot / Claude Code / Codex）当 MCP 服务器用

两条路走的是**同一份 handler 和同一套协议消息**，所以「自己用」和「给别人用」
不会有第二份实现（这是这个架构最值钱的地方）。
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from typing import Any, BinaryIO

from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    M_INITIALIZE,
    M_PING,
    M_TOOLS_CALL,
    M_TOOLS_LIST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Decoder,
    MCPError,
    ToolSpec,
    encode,
    error_response,
    is_notification,
    is_request,
    response,
    text_content,
    tool_result,
)

# handler 可以返回 str（一句答复）或 dict（完整的 MCP 结果，例如带图片）
ToolHandler = Callable[[dict], Any]


class MCPServer:
    """极小的 MCP 服务器。

    只实现 tools 能力（resources / prompts 暂时不需要）；`initialize` 里
    如实声明 capabilities，客户端问什么答什么。
    """

    def __init__(self, name: str, version: str = "1.0.0", instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    # ------------------------------------------------------------------ 注册
    def add_tool(
        self,
        name: str,
        description: str,
        schema: dict | None = None,
        handler: ToolHandler | None = None,
    ) -> None:
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"工具名只能用字母/数字/下划线/中划线：{name!r}")
        self._specs[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=schema or {"type": "object", "properties": {}},
        )
        if handler is not None:
            self._handlers[name] = handler

    def tool(self, name: str, description: str, schema: dict | None = None):
        """装饰器写法：@server.tool("now", "现在几点")  def now(args): ..."""

        def wrap(fn: ToolHandler) -> ToolHandler:
            self.add_tool(name, description, schema, fn)
            return fn

        return wrap

    def tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return f"<MCPServer {self.name} tools={len(self._specs)}>"

    # ------------------------------------------------------------- 消息处理
    def handle(self, msg: dict) -> dict | None:
        """一条消息进，一条响应出；通知返回 None（协议规定不回复）。"""
        if is_notification(msg):
            # initialized / cancelled 之类：我们不依赖它们，收到就当没事
            return None
        if not is_request(msg):
            return None

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            if method == M_INITIALIZE:
                return response(msg_id, self._on_initialize(params))
            if method == M_TOOLS_LIST:
                return response(msg_id, {"tools": [s.to_mcp() for s in self._specs.values()]})
            if method == M_TOOLS_CALL:
                return response(msg_id, self._on_call(params))
            if method == M_PING:
                return response(msg_id, {})
            return error_response(msg_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")
        except MCPError as exc:
            return error_response(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - 服务器绝不能因为一个工具崩掉
            return error_response(
                msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc(limit=6)},
            )

    def _on_initialize(self, params: dict) -> dict:
        want = str(params.get("protocolVersion") or "")
        # 对端要的版本我们支持就用它，否则回我们自己的（规范允许服务器决定）
        version = want if want in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": self.instructions,
        }

    def _on_call(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if name not in self._handlers:
            raise MCPError(INVALID_PARAMS, f"没有这个工具：{name}")
        out = self._handlers[name](args)
        if isinstance(out, dict):
            if "content" in out:
                return out                              # handler 自己给了完整结果
            if "text" in out:                           # 简写：{"text": ..., "is_error": bool}
                return tool_result(
                    [text_content(str(out.get("text") or ""))],
                    is_error=bool(out.get("is_error")),
                )
        return tool_result([text_content(str(out))])

    # ------------------------------------------------------------ stdio 循环
    def serve_stdio(self, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
        """按 MCP 的 stdio 传输跑循环：一行一条 JSON，日志一律走 stderr。

        ★stdout 只能出现协议消息★——多打一个 print 就会把对端的解析搞坏，
        所以这里和所有 handler 都不许往 stdout 写东西（用 logger / stderr）。
        """
        inp = stdin or getattr(sys.stdin, "buffer", sys.stdin)
        out = stdout or getattr(sys.stdout, "buffer", sys.stdout)
        dec = Decoder()
        while True:
            chunk = inp.read1(65536) if hasattr(inp, "read1") else inp.read(65536)  # type: ignore[union-attr]
            if not chunk:
                return 0                                # 对端关了管子，正常收工
            for msg in dec.feed(chunk):
                reply = self.handle(msg)
                if reply is not None:
                    out.write(encode(reply))
                    out.flush()


def log_stderr(text: str) -> None:
    """服务器里的日志（stdout 被协议占用了，只能用 stderr）。"""
    print(text, file=sys.stderr, flush=True)


def serve_module(module_path: str, name: str) -> int:
    """`python -m voice_loop.mcp.serve <模块>` 的实现体。

    ``module_path`` 形如 ``voice_loop.mcp.servers.skills``，模块里要有
    ``build_server() -> MCPServer``。
    """
    import importlib

    mod = importlib.import_module(module_path)
    server = mod.build_server()
    log_stderr(f"[mcp] {name or server.name} 就绪（{len(server.tools())} 个工具），走 stdio")
    return server.serve_stdio()


__all__ = ["MCPServer", "ToolHandler", "log_stderr", "serve_module"]
