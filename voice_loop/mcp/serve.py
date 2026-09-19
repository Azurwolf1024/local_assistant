"""把一个自家服务器挂到 stdio 上（给别人当 MCP 服务器用）。

    python -m voice_loop.mcp.serve skills
    python -m voice_loop.mcp.serve vision --list      # 只打印工具清单，方便调试

配到别的 agent 里（例）：

    # Claude Code
    claude mcp add voice-assistant -- python -m voice_loop.mcp.serve skills

    # VS Code / Copilot（.vscode/mcp.json）
    {"servers":{"voice-assistant":{"type":"stdio","command":"python",
      "args":["-m","voice_loop.mcp.serve","skills"],"cwd":"D:/local_AI"}}}

★stdout 是协议通道★：服务器里任何 print 都必须走 stderr，否则对端解析会崩
（server.MCPServer.serve_stdio 已经保证了这一点）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_loop.mcp.server import log_stderr  # noqa: E402

DEFAULT_MODULES = {
    "skills": "voice_loop.mcp.servers.skills",
}


def resolve(name: str) -> str:
    """把 ``skills`` 这样的短名翻成模块路径；也可以直接给模块路径。"""
    if name in DEFAULT_MODULES:
        return DEFAULT_MODULES[name]
    if "." in name:
        return name
    return f"voice_loop.mcp.servers.{name}"


def main() -> int:
    ap = argparse.ArgumentParser(description="把自家能力域挂成 MCP 服务器（stdio）")
    ap.add_argument("server", nargs="?", default="skills",
                    help="服务器名（skills / vision / system…）或完整模块路径")
    ap.add_argument("--list", action="store_true", help="只打印工具清单，不起协议循环")
    args = ap.parse_args()

    module = resolve(args.server)
    try:
        import importlib

        mod = importlib.import_module(module)
    except ImportError as exc:
        log_stderr(f"[mcp] 找不到服务器模块 {module}：{exc}")
        return 2
    build = getattr(mod, "build_server", None)
    if not callable(build):
        log_stderr(f"[mcp] {module} 里没有 build_server()")
        return 2

    server = build()
    if args.list:
        print(f"{server.name}（{len(server.tools())} 个工具）：")
        for tool in server.tools():
            first = (tool.description or "").splitlines()[0] if tool.description else ""
            print(f"  - {tool.name}：{first[:70]}")
        return 0

    log_stderr(f"[mcp] {server.name} 就绪（{len(server.tools())} 个工具），stdio 等待宿主…")
    return server.serve_stdio()


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
