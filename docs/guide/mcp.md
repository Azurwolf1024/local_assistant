# MCP：自己搭的工具协议层

> 从 [`README.md`](../../README.md) 拆出：第 13 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。

---
## 13. MCP：自己搭的工具协议层

这个项目的工具不再是一坨写死在 pipeline 里的函数，而是**按 MCP 协议**拆成服务器、
由宿主聚合（`voice_loop/mcp/`）。目标是拿那套 agent（Claude Code / Copilot）的架构，
但**不引任何新依赖**：协议层自己实现，只有 stdio 的 JSON-RPC。

```
pipeline（会话编排）
  └─ MCPHost         ← 只认「工具名 → 谁提供」这一件事
       ├─ MCPClient ─ InProcessTransport ─┐
       ├─ MCPClient ─ StdioTransport ─┐   │
       └─ …                           │   │
                                      ↓   ↓
                            MCPServer（自家能力域）
                              skills / memory / vision / system…
```

| 文件 | 干什么 |
| --- | --- |
| `mcp/protocol.py` | JSON-RPC 2.0 + MCP 消息形状、编解码（换行分帧）、形状校验 |
| `mcp/server.py` | 一个能力域 = 一个服务器；`initialize` / `tools/list` / `tools/call` / `ping` |
| `mcp/client.py` | 两种传输走**同一套消息**：inproc（不起进程，5 ms 握手）/ stdio（起子进程） |
| `mcp/host.py` | 聚合工具清单、白名单、路由、服务器崩了自动重启 |
| `mcp/servers/skills.py` | 第一个服务器：把现成的 `ToolRegistry`（事件/提醒/备忘）挂到协议后面 |
| `mcp/servers/memory.py` | ★ 记忆服务器（第 15 节）：`recall` / `remember` / `memory_stats`，默认只露前两个 |
| `mcp/servers/agent.py` | ★ harness agent：`web_search` / `web_fetch` / `read_file` / `list_dir` / `glob` / `grep`（+ 可选 `write_file` / `edit_file` / `run_command`），默认 `hidden` |
| `harness.py` | ★ harness 运行时：多轮工具循环 + 内置 `ask_user` / `update_plan`（进程内，不是 MCP 工具） |

第一条命令：

```powershell
python main.py mcp                                     # 现在有哪些工具、来自哪个服务器
python main.py mcp --call list_events --args "{\"text\": \"下周\"}"
python main.py mcp --serve skills                       # 挂到 stdio，给别人当 MCP 服务器用
```

### 接进别的 agent（把助手当 MCP 服务器）

```powershell
# Claude Code
claude mcp add voice-assistant -- python -m voice_loop.mcp.serve skills
```

```jsonc
// VS Code / Copilot：.vscode/mcp.json
{
  "servers": {
    "voice-assistant": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "voice_loop.mcp.serve", "skills"],
      "cwd": "D:/local_AI",
      "env": { "PYTHONIOENCODING": "utf-8", "VOICE_LOOP_DATA_DIR": "D:/local_AI/data" }
    }
  }
}
```

配好之后，在 Copilot 里说「查一下我的日程」它就会调这个服务器的 `list_events`，
拿到的和语音助手是**同一份数据**。

> `VOICE_LOOP_DATA_DIR` 只在 stdio 模式下生效（独立进程读的是真实 `config.toml`），
> 用来把数据目录指走（测试、或想同时跑两个实例）。

### 两条必须守的规矩

1. **★工具清单要短★**。实测本地 4b 在 8 个工具上 8/8，9b 反而 5/8；接一堆服务器
   把几十个工具摊在模型面前，它就开始乱选甚至不选。所以 `[[mcp.servers]]` 支持
   `tools = [...]` 白名单：**没进白名单的工具根本不出现在模型面前**（比调用时再拒绝安全得多）。
   接外来服务器时请默认只放只读工具。
   —— 现在自家共 **10 个**（技能 8 + 记忆 2），记忆那边也只露 `recall` / `remember`
   （`memory_stats` 留给命令行和外部 agent，不占模型的注意力）。
2. **★stdout 是协议通道★**。服务器里任何 `print()` 都会把对端的解析搞坏，
   日志一律走 `stderr`（`server.log_stderr()`），Python 子进程建议带
   `PYTHONIOENCODING=utf-8`（否则中文可能走 cp936）。

### 加一个新的能力域（三步）

```python
# voice_loop/mcp/servers/louder.py
from ..server import MCPServer
from ...system_ops import set_volume          # 假设有这么一个函数

def build_server(settings=None, skills=None, logger=None, **_) -> MCPServer:
    srv = MCPServer("louder", instructions="音量控制")
    srv.add_tool("get_volume", "当前音量",
                 {"type": "object", "properties": {}},
                 lambda a: f"当前音量 {get_volume()}%")
    srv.add_tool("set_volume", "调整音量（0~100）",
                 {"type": "object",
                  "properties": {"percent": {"type": "integer"}}, "required": ["percent"]},
                 lambda a: f"音量已调到 {set_volume(int(a['percent']))}%")
    return srv
```

```toml
# config.toml
[[mcp.servers]]
name = "louder"
transport = "inproc"                    # 自家代码：进程内，零启动开销
module = "voice_loop.mcp.servers.louder"
tools = ["get_volume", "set_volume"]    # 白名单（想全开就留空，但不推荐）
```

想隔离到子进程就把 `transport` 换成 `stdio` + `command = ["python", "-m", "voice_loop.mcp.serve", "louder"]`，
**handler 一行都不用改**。

### 命名规则

- 自家的能力域 `namespace = false`：名字保持 `list_events` / `add_memo` 原样
  （模型已经认得这 8 个，TOOL_HINT 和评估脚本也照旧）。
- 其它服务器自动加前缀 `mcp__<服务器>__<工具>`（跟 Claude Code 一致），防止撞名 ——
  所以记忆那两个工具在模型眼里叫 `mcp__memory__recall` / `mcp__memory__remember`。

---
