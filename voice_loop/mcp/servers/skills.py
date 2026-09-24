"""技能服务器：把现成的 ToolRegistry（日程 / 备忘 / 提醒 / 时间）包成 MCP 服务器。

这是「自己的 MCP 架构」的第一个服务器，也是最典型的例子：
**它没有重写任何业务逻辑**，只是把已经有守卫、有测试的 ToolRegistry 挂到协议后面。
好处是加能力域时不用动 pipeline，而且同一份 handler 既能给本地模型用，
也能通过 stdio 交给 Copilot / Claude Code 用。

工具名沿用原样（list_schedule / add_memo …），不带 mcp__ 前缀——
它们是助手自己的核心能力，模型已经认得这 8 个名字（实测 4b 在这 8 个上 8/8）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ...settings import Settings, load_settings
from ...skills import Skills
from ...tools import ToolRegistry
from ..server import MCPServer

NAME = "skills"

# 让独立进程（stdio 模式）能把数据目录挪走：
# ★为什么需要它★：stdio 服务器是个**新进程**，它读的是真实 config.toml。
# 测试、或想同时跑两个实例时，一句环境变量就能把 data/*.json 换到临时目录，
# 否则一次自测就会往用户真实的备忘/日程里写东西（这个坑真踩过）。
DATA_DIR_ENV = "VOICE_LOOP_DATA_DIR"


def settings_for_standalone() -> Settings:
    """stdio 模式下的配置：config.toml + ``VOICE_LOOP_DATA_DIR`` 覆盖。"""
    settings = load_settings()
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        base = Path(override)
        settings.skills.data_dir = str(base)
        settings.skills.event_file = str(base / "events.json")
        settings.skills.memo_file = str(base / "memos.json")
        settings.skills.alarm_file = str(base / "alarms.json")      # 迁移数据源
        settings.skills.schedule_file = str(base / "schedule.json")  # 迁移数据源
    return settings

INSTRUCTIONS = (
    "这是本地语音助手的生活技能：查日程/课表、下一项安排、提醒（闹钟）、备忘，"
    "以及新增日程/备忘/提醒。时间说法（「下周三下午三点半」「明天早上七点」）"
    "原样放进 text 参数即可，解析由确定性的中文时间解析器负责。"
)


def build_server(
    settings: Settings | None = None,
    skills: Skills | None = None,
    logger: logging.Logger | None = None,
    **_ignored: Any,
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    settings = settings or settings_for_standalone()
    skills = skills or Skills(settings, log)
    registry = ToolRegistry(settings, skills, log)

    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)
    for spec in registry.specs():
        fn = spec.get("function") or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        server.add_tool(
            name,
            str(fn.get("description") or ""),
            fn.get("parameters") or {"type": "object", "properties": {}},
            _handler(registry, name, log),
        )
    return server


def _handler(registry: ToolRegistry, name: str, log: logging.Logger):
    """把工具调用转给 ToolRegistry（守卫、时间解析、去重都在它后面）。"""

    def run(args: dict) -> dict:
        result = registry.call({"function": {"name": name, "arguments": args or {}}})
        text = result.reply or result.error or "（没有结果）"
        if not result.ok:
            log.info(f"[mcp:skills.{name}] 没做成：{text[:60]}")
        return {"text": text, "is_error": not result.ok}

    return run
