"""API 角色的工具预设。

本地 4B 模型能稳定驾驭的工具就那十个（见 README 第 1.2 节），所以默认这十个
对所有角色都一样。**harness 预设虽然列在这里，但只对 `engine = "api"` 的角色展开**
（`pipeline._api_extra_tools` 在本地角色直接返回空集），本地小模型永远不会看到 31 项。
云端模型更强，可以多背几个工具 —— 具体加哪些见下面的预设。

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
        "title": "完整 harness 工具集（对齐 DSH：agent + 邮件 + 记忆 + 技能，缺失的自动跳过）",
        "tools": [
            # agent 服务器（写/命令仍受 [agent] allow_write / allow_shell 闸门）
            "web_search", "web_fetch", "weather", "mail_search", "mail_read",
            "mail_summary", "mail_draft", "mail_send", "mail_to_event",
            "end_listening", "read_file", "list_dir", "glob", "grep",
            "write_file", "edit_file", "run_command",
            "job_output", "job_list", "job_kill",
            # 记忆（request_dream 默认 hidden，点名才露）
            "mcp__memory__recall", "mcp__memory__remember",
            "mcp__memory__request_dream",
            # 技能层（日程 / 备忘 / 报时 / 纠正上一条）
            "list_events", "next_event", "add_event", "change_event",
            "list_memos", "add_memo", "now", "fix_last",
        ],
    },
    "harness-progressive": {
        "title": "渐进式 harness（核心工具 + 工具发现，为模块化扩展准备）",
        "mode": "progressive",
        # 只把高频工具放进模型每轮固定的 tools 数组；长尾工具按需经
        # tool_search / tool_describe / tool_invoke 找到并调用。
        # ask_user / update_plan 是 harness 内置，不在这里点名。
        "tools": [
            "now", "list_events", "next_event", "add_event",
            "web_search", "read_file", "end_listening",
            "tool_groups", "tool_search", "tool_describe", "tool_invoke",
        ],
        # ★固定暴露 ≠ 授权范围★：长尾工具不进固定数组是为了护 KV cache，
        # 但 tool_invoke 的闸门必须放行它们，否则「搜得到、调不动」。
        # 授权全集 = 完整 harness（仍受各 server 的 [agent] 闸门约束 ——
        # allow_shell / allow_write 没开时那些工具根本不注册，自然也调不到）。
        "authorizes": "harness",
    },
    # ★下一步讨论好的额外工具加在这里★，例如：
    #   "web": {"title": "联网", "tools": ["web_search", "web_fetch"]},
}


def expand(entries) -> set[str]:
    """把「预设名 + 具体工具名」展开成一组工具名（保持去重）。

    这是**固定暴露**给模型的工具名；渐进式预设只展开它的核心工具。
    """
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


def authorized(entries) -> set[str]:
    """这一轮角色**有权通过 tool_invoke 调用**的工具名（渐进式含长尾）。

    ★为什么要跟 expand 分开★：渐进式角色为了护 KV cache，只把高频工具放进
    模型每轮固定的 tools 数组；长尾工具（shell / 写文件 / 邮件…）靠
    tool_search 发现后经 tool_invoke 调用。如果授权集合也用那份固定数组，
    长尾工具就「搜得到、看得到参数、一调用就被拒」——等于渐进式白做了。

    这里把带 ``authorizes`` 的预设展开成它指向的完整预设（harness-progressive
    → harness），其余预设仍按自身 tools 展开。注意：**这不是绕过闸门** ——
    write_file / run_command 等是否注册由各 server 按 ``[agent] allow_*`` 决定，
    没开时工具根本不在宿主里，进不了这份集合，也调不到。
    """
    out = expand(entries)
    for entry in entries or []:
        name = str(entry or "").strip()
        preset = PRESETS.get(name)
        if preset is None:
            continue
        target = preset.get("authorizes")
        if target and str(target) in PRESETS:
            out.update(str(t) for t in PRESETS[str(target)].get("tools") or [])
    return out


def describe() -> list[dict]:
    """给控制台 / 文档用：现在有哪些预设、各展开成什么。"""
    return [
        {"name": name, "title": info.get("title", ""),
         "mode": str(info.get("mode") or "full"),
         "tools": list(info.get("tools") or []),
         "authorizes": str(info.get("authorizes") or "")}
        for name, info in PRESETS.items()
    ]
