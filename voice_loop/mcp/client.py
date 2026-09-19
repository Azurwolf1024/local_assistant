"""MCP 客户端侧：握手 + 列工具 + 调工具。

两种传输，走**同一套协议消息**：

``InProcessTransport``
    自家服务器的默认选择：不起进程，直接把消息交给 :class:`MCPServer.handle`。
    零启动开销（语音助手最在意这个），但行为与走进程时完全一致——
    因为它照样经过 initialize / tools/list / tools/call 这些消息。

``StdioTransport``
    起子进程按 MCP stdio 跑（`python -m voice_loop.mcp.serve <服务器名>`），
    用于：① 别人写的 MCP 服务器；② 我们自己想把某个能力域隔离出去。

为什么要线程读 stdout：Windows 的 select 不认管道，而语音链路是同步代码，
不能为了等一条响应把事件循环塞进来。所以起一个读线程喂队列，主线程 ``get(timeout=)``。
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from .protocol import (
    MCPError,
    M_INITIALIZE,
    M_INITIALIZED,
    M_PING,
    M_TOOLS_CALL,
    M_TOOLS_LIST,
    PROTOCOL_VERSION,
    Decoder,
    ToolSpec,
    encode,
    notification,
    request,
    result_text,
)
from .server import MCPServer

DEFAULT_TIMEOUT = 15.0


class Transport(Protocol):
    label: str

    def send(self, msg: dict) -> None: ...
    def recv(self, timeout: float) -> dict | None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
class InProcessTransport:
    """进程内传输：消息不过 JSON，但形状与顺序完全一样。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self.label = f"inproc:{server.name}"
        self._inbox: queue.Queue[dict] = queue.Queue()

    def send(self, msg: dict) -> None:
        reply = self.server.handle(msg)
        if reply is not None:
            self._inbox.put(reply)

    def recv(self, timeout: float) -> dict | None:
        try:
            return self._inbox.get(timeout=max(0.001, timeout))
        except queue.Empty:
            return None

    def close(self) -> None:
        while not self._inbox.empty():
            self._inbox.get_nowait()


# --------------------------------------------------------------------------- #
class StdioTransport:
    """子进程 stdio 传输（一行一条 JSON）。"""

    def __init__(
        self,
        command: list[str],
        cwd: str | Path | None = None,
        env: dict | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.command = [str(c) for c in command]
        self.cwd = str(cwd) if cwd else None
        # 子进程的环境：默认继承，配置里的 env 覆盖上去
        # （Python 写的服务器建议带 PYTHONIOENCODING=utf-8，否则中文可能走 cp936）
        env_all = dict(os.environ)
        env_all.update({str(k): str(v) for k, v in (env or {}).items()})
        self.env = env_all
        self.log = log or logging.getLogger("voice_loop")
        self.label = f"stdio:{' '.join(self.command[:2])}"
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue[dict] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._dec = Decoder()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 起停
    def start(self) -> None:
        self.log.info(f"[mcp] 启动子进程：{' '.join(self.command)}")
        creation = 0
        if sys.platform == "win32":                       # 别弹出黑窗口
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,                    # 服务器日志不要污染我们的控制台
            cwd=self.cwd,
            env=self.env,
            bufsize=0,
            creationflags=creation,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True, name="mcp-read")
        self._reader.start()

    def _pump(self) -> None:
        """把子进程 stdout 切成的消息塞进队列（唯一读 stdout 的地方）。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                if not chunk:
                    break
                for msg in self._dec.feed(chunk):
                    self._inbox.put(msg)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[mcp] 读子进程输出失败：{exc}")
        finally:
            self._inbox.put({"__eof__": True})

    def send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None:
            # 懒启动：第一次发消息才把子进程拉起来（谁都不用记得先调 start()）
            self.start()
            proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError(-32603, "子进程没起来")
        if proc.poll() is not None:
            raise MCPError(-32603, f"子进程已退出（code={proc.returncode}）")
        with self._lock:
            proc.stdin.write(encode(msg))
            proc.stdin.flush()

    def recv(self, timeout: float) -> dict | None:
        try:
            msg = self._inbox.get(timeout=max(0.001, timeout))
        except queue.Empty:
            return None
        if msg.get("__eof__"):
            raise MCPError(-32603, "子进程关闭了输出（可能已崩溃）")
        return msg

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.log.warning(f"[mcp] 子进程不退出，强杀：{' '.join(self.command)}")
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass


# --------------------------------------------------------------------------- #
class MCPClient:
    """一个服务器的连接：握手 → 列工具 → 调工具。"""

    def __init__(
        self,
        name: str,
        transport: Transport,
        timeout: float = DEFAULT_TIMEOUT,
        log: logging.Logger | None = None,
    ) -> None:
        self.name = name
        self.transport = transport
        self.timeout = float(timeout)
        self.log = log or logging.getLogger("voice_loop")
        self.protocol_version = ""
        self.server_info: dict = {}
        self.instructions = ""
        self._tools: list[ToolSpec] = []
        self._next_id = 0
        self._started = False

    # ------------------------------------------------------------------ 握手
    def start(self) -> None:
        if self._started:
            return
        t0 = time.perf_counter()
        result = self._call(
            M_INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "voice_loop", "version": "1.0.0"},
            },
        )
        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = result.get("serverInfo") or {}
        self.instructions = str(result.get("instructions") or "")
        # 规范要求客户端随后发这个通知（服务器不回复）
        self.transport.send(notification(M_INITIALIZED))
        self._started = True
        self.log.info(
            f"[mcp] {self.name} 已连接（{self.server_info.get('name', '?')} "
            f"proto={self.protocol_version}，握手 {time.perf_counter() - t0:.2f}s）"
        )

    def ping(self) -> bool:
        try:
            self._call(M_PING, {})
            return True
        except MCPError:
            return False

    def list_tools(self, refresh: bool = False) -> list[ToolSpec]:
        if not self._started:
            self.start()
        if not self._tools or refresh:
            result = self._call(M_TOOLS_LIST, {})
            self._tools = [ToolSpec.from_mcp(t) for t in (result.get("tools") or [])]
        return list(self._tools)

    def call(self, tool: str, arguments: dict | None = None) -> tuple[bool, str]:
        """调一个工具，返回 ``(ok, 文本)``；协议层错误会转成 ok=False。"""
        try:
            result = self._call(M_TOOLS_CALL, {"name": tool, "arguments": arguments or {}})
        except MCPError as exc:
            return False, f"工具调用失败：{exc.message}"
        ok = not bool(result.get("isError"))
        return ok, result_text(result)

    def close(self) -> None:
        self.transport.close()
        self._started = False

    # ---------------------------------------------------------------- 内部
    def _call(self, method: str, params: dict) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self.transport.send(request(msg_id, method, params))
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MCPError(-32603, f"{self.name} 在 {self.timeout:.0f}s 内没回 {method}")
            msg = self.transport.recv(left)
            if msg is None:
                continue
            if msg.get("id") != msg_id:
                # 别的请求的响应 / 服务器主动通知：这里只有一个在飞的请求，记一下就跳过
                self.log.debug(f"[mcp] {self.name} 收到无关消息：{str(msg)[:120]}")
                continue
            if "error" in msg:
                err = msg["error"] or {}
                raise MCPError(int(err.get("code") or -32603), str(err.get("message") or "未知错误"))
            return msg.get("result") or {}


def make_transport(spec, log: logging.Logger | None = None, deps: dict | None = None) -> Transport:
    """按配置造一个传输（配置结构见 settings.McpServerConfig）。

    ``deps``：进程内服务器要用的依赖（settings / skills / logger）。
    ★必须把助手自己那份传进去★：否则 inproc 服务器会另建一套 Skills，
    两边各认一份缓存，日程就可能读到旧数据。
    """
    kind = (getattr(spec, "transport", "inproc") or "inproc").lower()
    if kind == "stdio":
        return StdioTransport(
            command=list(spec.command),
            cwd=spec.cwd or None,
            env=dict(getattr(spec, "env", None) or {}),
            log=log,
        )
    if kind == "inproc":
        import importlib

        module = str(getattr(spec, "module", "") or "")
        if not module:
            raise MCPError(-32602, f"服务器 {spec.name} 是 inproc，但没配 module")
        mod = importlib.import_module(module)
        build = getattr(mod, "build_server", None)
        if not callable(build):
            raise MCPError(-32602, f"{module} 里没有 build_server()")
        return InProcessTransport(build(**(deps or {})))
    raise MCPError(-32602, f"不认识的 transport：{kind}（只有 inproc / stdio）")
