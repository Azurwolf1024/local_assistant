# MCP：自己搭的工具协议层

> 从 [`README.md`](../../README.md) 拆出：第 13 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。
> ★实现细节（协议字段、两种传输、白名单拦在哪、harness 循环、故障对照）在
> [`voice_loop/mcp/README.md`](../../voice_loop/mcp/README.md)★ —— 这里是「怎么用」。

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
                              skills / memory / agent / mail…
```

| 文件 | 干什么 |
| --- | --- |
| `mcp/protocol.py` | JSON-RPC 2.0 + MCP 消息形状、编解码（换行分帧）、形状校验 |
| `mcp/server.py` | 一个能力域 = 一个服务器；`initialize` / `tools/list` / `tools/call` / `ping` |
| `mcp/client.py` | 两种传输走**同一套消息**：inproc（不起进程，5 ms 握手）/ stdio（起子进程） |
| `mcp/host.py` | 聚合工具清单、白名单、路由、服务器崩了自动重启 |
| `mcp/servers/skills.py` | 第一个服务器：把现成的 `ToolRegistry`（事件/提醒/备忘）挂到协议后面 |
| `mcp/servers/memory.py` | ★ 记忆服务器（第 15 节）：`recall` / `remember` / `memory_stats`，默认只露前两个 |
| `mcp/servers/agent.py` | ★ harness agent：`web_search` / `web_fetch` / `weather` / `read_file` / `list_dir` / `glob` / `grep`（+ 可选 `write_file` / `edit_file` / `run_command`），默认 `hidden` |
| `harness.py` | ★ harness 运行时：多轮工具循环 + 内置 `ask_user` / `update_plan`（进程内，不是 MCP 工具） |
| `tool_presets.py` | ★ API 角色的工具预设：人格文件 `api_tools` 里写预设名，展开成真实工具名 |

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
   —— 现在自家技能服务器的白名单是空的（全露 9 个），记忆只露 `recall` / `remember`，
   所以本地角色看到 **11 个**；`agent` / `mail` 两台是 `hidden`，默认一个都不露
   （`memory_stats` 留给命令行和外部 agent，不占模型的注意力）。
2. **★stdout 是协议通道★**。服务器里任何 `print()` 都会把对端的解析搞坏，
   日志一律走 `stderr`（`server.log_stderr()`），Python 子进程建议带
   `PYTHONIOENCODING=utf-8`（否则中文可能走 cp936）。

### 加一个新的能力域（三步）

```python
# voice_loop/mcp/servers/louder.py
from ..server import MCPServer
from ...my_new_module import set_volume          # 假设有这么一个函数

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
  （模型已经认得这些名字，TOOL_HINT 和评估脚本也照旧）。
- 其它服务器自动加前缀 `mcp__<服务器>__<工具>`（跟 Claude Code 一致），防止撞名 ——
  所以记忆那两个工具在模型眼里叫 `mcp__memory__recall` / `mcp__memory__remember`。

### API 角色能多开工具（预设 / `api_tools`）

本地 4B 能稳定驾驭的工具就那十个，**云端模型更强**，所以"多背几个工具"这件事
做成了**数据**，不是代码分支：

```jsonc
// data/personas/<id>.json —— 「蓝色大肥鱼」这种 engine=api 的角色
{ "engine": "api", "api_tools": ["mail", "web", "weather", "voice"] }
//                                  ↑ 预设名（展开见下表），也可以直接写具体工具名
```

| 预设 | 展开成 | 几个 |
| --- | --- | --- |
| `memory` | `mcp__memory__recall` / `remember` / `request_dream` | 3 |
| `skills` | `list_events` … `fix_last`（日程/备忘/提醒/时间） | 8 |
| `web` | `web_search` / `web_fetch`（DeepSeek Search / Fetch） | 2 |
| `weather` | `weather`（Open-Meteo，免 key） | 1 |
| `voice` | `end_listening`（让模型能主动结束这次聆听） | 1 |
| `mail` | `mail_search` / `read` / `summary` / `draft` / `send` / `to_event` | 6 |
| `files` | `read_file` / `list_dir` / `glob` / `grep` | 4 |
| `write` | `write_file` / `edit_file`（要 `[agent] allow_write = true`） | 2 |
| `shell` | `run_command` + `job_output` / `job_list` / `job_kill`（要 `[agent] allow_shell = true`，高风险） | 4 |
| `harness` | agent 20（含邮件 6 + 后台作业 3）+ 记忆 3 + 技能 8 = **31 项**，对齐 DSH；写 / 命令 / 发送各有闸门；★仅 API 角色★ | 31 |
| `harness-progressive` | 核心 7 + 元工具 4 = **11 项**；长工具靠 `tool_search` / `tool_describe` / `tool_invoke` 按需发现，tools 数组固定更省 token、护缓存；★仅 API 角色★ | 11 |

三条要记住的：

1. **只对 `engine = api` 生效**，本地角色永远只有默认那几个（`_api_extra_tools()` 直接返回空集）；
2. 人格文件没写 `api_tools` 就退回 `[api] extra_tools`；
3. 名字写错只打警告（"这些额外工具没有服务器提供"），**不会**让一轮对话失败。

`hidden = true` 的服务器（`agent` / `mail`）就是靠这套点名的：默认一个都不露，
本地 4B 看不见也就不会误调。

### harness：多轮工具循环（只有 API 角色走）

本地那套是「模型选一个工具 → 执行 → 把结果念出来」。API 角色走的是成熟 agent 的做法：

```
模型 → 调工具 → 结果喂回模型 → 再决定要不要继续 → … → 最终回答
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `[agent] enabled` | `true` | 关掉 = API 角色也只做单轮 |
| `[agent] max_rounds` | `6` | 到顶会**再问一次、不给工具**，逼它用已有资料收口 |
| `[agent] max_tool_chars` | `6000` | 单个工具结果喂回模型的上限（超了截断） |
| `[agent] allow_write` | `false` | 开了才**注册** `write_file` / `edit_file` |
| `[agent] allow_shell` | `false` | 开了才注册 `run_command`（等于把本机 shell 交给模型） |

harness 还自带两个工具：`ask_user`（缺信息就真的追问一句）和
`update_plan`（多步任务先写步骤，计划不会自动执行、也不念给用户听）。

### 在控制台看「模型能 call 什么」

控制台有专门的 **「工具」页**：

- **上半页是真实的**：点一下「问服务要真实清单」，服务会回当前角色此刻能看到的
  工具（按服务器分组、显示 `11 个工具 ← skills(inproc,9) memory(inproc,2/4) …`、
  有没有点名了却没找到的、工具数是不是偏多）。**服务没跑就如实说没问到**，不拿推算冒充。
- **下半页是说明**：预设各展开成什么、每个角色多开了几个、每台服务器的白名单 /
  `hidden` / 前缀 / 超时、`[agent]` 的写盘与命令开关。

改完人格文件的 `api_tools` 之后回这一页点一下，就能看出"多开了几个、到底叫什么"。

### 工具数量是有上限的（实测）

| 工具数 | 现象 |
| --- | --- |
| 8 | 4b 选得 8/8（甜点） |
| 11 | 本地角色现在的常态，稳 |
| 18 | 以上开始不保险 |
| 22 | ★`deepseek-chat` 把「看邮件」错选成了 `list_memos`★ |

所以接外来服务器时**默认只放只读工具**，这也是 `tools = [...]` 白名单存在的理由。

---
