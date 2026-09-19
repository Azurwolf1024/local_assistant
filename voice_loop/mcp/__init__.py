"""自己搭的 MCP 架构（协议 → 服务器 → 宿主）。

一眼看懂它在这个项目里的位置：

    ┌─────────────────────────────────────────────────────────┐
    │ pipeline（会话编排）                                    │
    │   └─ MCPHost  ← 只认「工具名 → 谁提供」这一件事          │
    │        ├─ MCPClient ─ InProcessTransport ─┐             │
    │        ├─ MCPClient ─ StdioTransport ─┐   │             │
    │        └─ ……                          │   │             │
    │                                       ↓   ↓             │
    │                              MCPServer（自家能力域）     │
    │                                skills / vision / system  │
    └─────────────────────────────────────────────────────────┘

- ``protocol``：JSON-RPC 2.0 + MCP 的消息形状（自己实现，零额外依赖）
- ``server``：一个能力域 = 一个 MCPServer，可 inproc 也可 stdio
- ``client``：两种传输走**同一套消息**（inproc 不起进程，stdio 起子进程）
- ``host``：聚合 + 白名单 + 路由 + 崩了自动重启

加了新能力域（比如「读文件」「控制音量」）只要写一个 ``servers/xxx.py`` 并在
``config.toml`` 的 ``[[mcp.servers]]`` 里登记，pipeline 一行都不用改。
"""

from .client import InProcessTransport, MCPClient, StdioTransport, make_transport
from .host import SEP, MCPHost
from .protocol import PROTOCOL_VERSION, MCPError, ToolSpec
from .server import MCPServer

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPHost",
    "MCPServer",
    "PROTOCOL_VERSION",
    "SEP",
    "InProcessTransport",
    "StdioTransport",
    "ToolSpec",
    "make_transport",
]
