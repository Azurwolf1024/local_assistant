"""API 角色的工具预设。

本地 4B 模型能稳定驾驭的工具就那十个（见 README 第 1.2 节），所以默认这十个
对所有角色都一样。**云端模型更强**，可以多背几个工具 —— 但具体加哪些还在讨论，
先把「怎么加」这件事定下来：

    角色卡（人格文件）里的 ``api_tools`` 写预设名或具体工具名即可
        "api_tools": ["memory", "web_search"]
    → ``memory`` 展开成预设里的那组工具名；
    → ``web_search`` 不是预设，就当成一个具体工具名，去 MCP 宿主里找。

工具到底存不存在由 MCP 宿主兜底：找不到的名字只打警告，不会让一轮对话失败。
新增一个预设只需要在 :data:`PRESETS` 里加一行，**不用改流水线**。
"""

from __future__ import annotations

# 预设名 → {说明, 工具名列表}
#
# 注意：工具名就是模型看到的名字。自家能力域没有前缀（list_events…），
# 外来的服务器带 ``mcp__<服务器>__`` 前缀（见 voice_loop/mcp/host.py）。
PRESETS: dict[str, dict] = {
    "memory": {
        "title": "跨角色记忆",
        "tools": ["mcp__memory__recall", "mcp__memory__remember",
                  "mcp__memory__request_dream"],
    },
    "skills": {
        "title": "日程 / 备忘 / 提醒",
        "tools": [
            "list_events", "next_event", "list_memos", "add_event",
            "add_memo", "change_event", "now", "fix_last",
        ],
    },
    "web": {
        "title": "联网搜索 / 网页读取（DeepSeek Search / Fetch）",
        "tools": ["web_search", "web_fetch"],
    },
    "weather": {
        "title": "天气（Open-Meteo，免 key）",
        "tools": ["weather"],
    },
    "voice": {
        "title": "语音控制（结束聆听；API 角色用）",
        "tools": ["end_listening"],
    },
    "mail": {
        "title": "邮件（查 / 读 / 摘要 / 起草 / 提日程；发送与写日程都要人工审批）",
        "tools": ["mail_search", "mail_read", "mail_summary", "mail_draft", "mail_send",
                  "mail_to_event"],
    },
    "files": {
        "title": "工作区文件（读 / 列目录 / 找文件 / 搜索）",
        "tools": ["read_file", "list_dir", "glob", "grep"],
    },
    "write": {
        "title": "写/改工作区文件（需要 [agent] allow_write = true）",
        "tools": ["write_file", "edit_file"],
    },
    "shell": {
        "title": "执行命令（需要 [agent] allow_shell = true；高风险）",
        "tools": ["run_command"],
    },
    "harness": {
        "title": "完整 harness 工具集（不可用的会自动跳过）",
        "tools": ["web_search", "web_fetch", "weather", "mail_search", "mail_read",
                  "mail_summary", "mail_draft", "mail_to_event", "end_listening",
                  "read_file", "list_dir",
                  "glob", "grep", "write_file", "edit_file", "run_command"],
    },
    # ★下一步讨论好的额外工具加在这里★，例如：
    #   "web": {"title": "联网", "tools": ["web_search", "web_fetch"]},
}


def expand(entries) -> set[str]:
    """把「预设名 + 具体工具名」展开成一组工具名（保持去重）。"""
    out: set[str] = set()
    for entry in entries or []:
        name = str(entry or "").strip()
        if not name:
            continue
        preset = PRESETS.get(name)
        if preset is not None:
            out.update(str(t) for t in preset.get("tools") or [])
        else:
            out.add(name)
    return out


def describe() -> list[dict]:
    """给控制台 / 文档用：现在有哪些预设、各展开成什么。"""
    return [
        {"name": name, "title": info.get("title", ""),
         "tools": list(info.get("tools") or [])}
        for name, info in PRESETS.items()
    ]
