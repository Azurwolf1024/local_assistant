"""MCP 协议层：JSON-RPC 2.0 + MCP 的消息形状。

为什么要自己写这一层（而不是 `pip install mcp`）
    - 本项目是**纯离线、零额外依赖**的风格，官方 SDK 会带进 pydantic / starlette /
      uvicorn / httpx 一大串（我们只用得上 stdio 那一条路径）。
    - 协议本身很小：一行一个 JSON（newline-delimited JSON-RPC 2.0）+ 四个方法
      （initialize / tools/list / tools/call / ping）+ 一个通知（initialized）。
      写清楚比引进来更好维护，而且能脱离模型单测。
    - 但**消息形状严格照 MCP 规范**，所以我们的服务器能被 Copilot / Claude Code
      直接当 MCP 服务器用，反过来我们也能接别人的服务器（见 client.StdioTransport）。

这一层只做两件事：**把消息编成字节 / 从字节里切出消息**，以及**校验形状**。
真正干活在 server.py（服务器侧）和 client.py / host.py（客户端侧）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

JSONRPC = "2.0"

# 我们实现/期望的 MCP 规范版本（客户端与服务器要在 initialize 里对齐）
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 错误码（MCP 直接沿用）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP 定义的方法名
M_INITIALIZE = "initialize"
M_INITIALIZED = "notifications/initialized"
M_TOOLS_LIST = "tools/list"
M_TOOLS_CALL = "tools/call"
M_PING = "ping"
M_CANCELLED = "notifications/cancelled"
M_RESOURCES_LIST = "resources/list"
M_PROMPTS_LIST = "prompts/list"


class MCPError(Exception):
    """协议层错误（带 JSON-RPC 错误码）。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# --------------------------------------------------------------------------- #
# 消息构造
# --------------------------------------------------------------------------- #
def request(msg_id: int | str, method: str, params: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": JSONRPC, "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def response(msg_id: int | str | None, result: Any) -> dict:
    return {"jsonrpc": JSONRPC, "id": msg_id, "result": result}


def error_response(msg_id: int | str | None, code: int, message: str, data: Any = None) -> dict:
    msg: dict = {"jsonrpc": JSONRPC, "id": msg_id, "error": {"code": code, "message": message}}
    if data is not None:
        msg["error"]["data"] = data
    return msg


def is_request(msg: dict) -> bool:
    """有 id 的是请求（要回），没 id 的是通知（不回）。"""
    return "method" in msg and "id" in msg and msg.get("id") is not None


def is_notification(msg: dict) -> bool:
    return "method" in msg and msg.get("id") is None


def is_response(msg: dict) -> bool:
    return "method" not in msg and ("result" in msg or "error" in msg)


def validate(msg: Any) -> dict:
    """校验一条入站消息，形状不对就抛 :class:`MCPError`。"""
    if not isinstance(msg, dict):
        raise MCPError(INVALID_REQUEST, "消息必须是 JSON 对象")
    if msg.get("jsonrpc") != JSONRPC:
        raise MCPError(INVALID_REQUEST, f"jsonrpc 必须是 {JSONRPC!r}")
    if is_request(msg) or is_notification(msg):
        if not isinstance(msg.get("method"), str) or not msg["method"]:
            raise MCPError(INVALID_REQUEST, "缺少 method")
        if "params" in msg and not isinstance(msg["params"], dict):
            raise MCPError(INVALID_PARAMS, "params 必须是对象")
    elif is_response(msg):
        if "result" in msg and "error" in msg:
            raise MCPError(INVALID_REQUEST, "result 与 error 不能同时出现")
    else:
        raise MCPError(INVALID_REQUEST, "既不是请求/通知也不是响应")
    return msg


# --------------------------------------------------------------------------- #
# 编解码（stdio 用换行分帧：一行一条完整 JSON）
# --------------------------------------------------------------------------- #
def encode(msg: dict) -> bytes:
    """编成一行 UTF-8 字节（中文不转义，便于人肉 debug）。"""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Decoder:
    """把字节流切成消息。

    允许**不带换行**的裸 JSON（有些实现会一次写一整段），做法是：先按行切，
    切不出就攒着，直到能 json.loads 成功为止。
    """

    def __init__(self, max_buffer: int = 16 * 1024 * 1024) -> None:
        self._buf = ""
        self.max_buffer = max_buffer

    def feed(self, chunk: bytes) -> Iterator[dict]:
        self._buf += chunk.decode("utf-8", errors="replace")
        while True:
            line, sep, rest = self._buf.partition("\n")
            if not sep:
                # 没有换行：试着把整个缓冲区当一条 JSON（单行裸消息）
                text = self._buf.strip()
                if text:
                    try:
                        yield validate(json.loads(text))
                    except json.JSONDecodeError:
                        pass                      # 还没收全，等下一块
                    except MCPError:
                        raise
                if len(self._buf) > self.max_buffer:
                    raise MCPError(PARSE_ERROR, "缓冲超过上限，疑似对端没有按行分帧")
                return
            self._buf = rest
            line = line.strip()
            if not line:
                continue
            try:
                yield validate(json.loads(line))
            except json.JSONDecodeError as exc:
                raise MCPError(PARSE_ERROR, f"不是合法 JSON：{exc}") from exc


# --------------------------------------------------------------------------- #
# 工具描述 / 调用结果
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    """一个工具（服务器侧声明，客户端侧缓存）。"""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_mcp(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}

    @staticmethod
    def from_mcp(raw: dict) -> "ToolSpec":
        return ToolSpec(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            input_schema=raw.get("inputSchema") or {"type": "object", "properties": {}},
        )

    def to_openai(self) -> dict:
        """转成 Ollama / OpenAI 的 tools 形状（我们喂给本地模型的就是这个）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def text_content(text: str) -> dict:
    """MCP 的文本内容块。"""
    return {"type": "text", "text": text}


def tool_result(content: list[dict], is_error: bool = False) -> dict:
    out: dict = {"content": content}
    if is_error:
        out["isError"] = True
    return out


def result_text(result: Any) -> str:
    """把 tools/call 的结果拍平成一个字符串（本地模型只吃文本）。"""
    if not isinstance(result, dict):
        return str(result or "")
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append("[图片]")
            else:
                parts.append(str(block.get("type") or block))
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p).strip()
