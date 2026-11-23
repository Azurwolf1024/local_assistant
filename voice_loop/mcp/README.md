# `voice_loop/mcp/` —— 自己搭的 MCP 工具层（+ harness 运行时）

> 这一层只回答一件事：**模型能看到哪些工具、这些工具从哪来、调完之后结果怎么回去**。
> 它不认识麦克风、TTS、记忆、邮件 —— 平台能力都在 `servers/*.py` 里，靠 `deps` 注入。
>
> 面向使用者的说明在 [`docs/guide/mcp.md`](../../docs/guide/mcp.md)（怎么配、怎么接给别的 agent）；
> **这一份讲实现**：协议长什么样、两种传输差在哪、白名单在哪拦、harness 循环怎么收口、
> 控制台「工具」页看的到底是什么、坏了怎么查。

## 0. 一句话设计

**模型面前只有一份钉死的工具清单**（白名单 ∩ 服务器实际提供的），
**清单里每个名字都能被路由回某个服务器**，**没进清单的模型根本看不见**。
展示层不发名字，调用层也再校验一次：`host.call(..., allowed=本轮可见集合)`，
本地角色幻觉出 hidden 工具也执行不了（CLI / 自测不传 `allowed`，保持老行为）。

```
pipeline（会话编排）                                   ← 谁在用
  └─ MCPHost          只认「工具名 → 谁提供」           ← host.py
       ├─ MCPClient ─ InProcessTransport ─┐
       ├─ MCPClient ─ StdioTransport ─┐   │
       └─ …                           │   │
                                      ↓   ↓
                            MCPServer（一个能力域一台）  ← server.py
                              skills / memory / agent / mail
```

三个视角各看一层：

| 视角 | 关心什么 | 看哪 |
| --- | --- | --- |
| 用户 | 怎么开、接给别的 agent 用、哪些开关能关掉外发/写盘/跑命令 | [`docs/guide/mcp.md`](../../docs/guide/mcp.md) |
| Agent（模型） | 我有哪些工具、参数怎么写 | §6 的工具表 + `servers/*.py` 里的 `description`（**只有这些会进提示词**） |
| 改代码的人 | 协议、传输、路由、闸门、坏了怎么查 | 本文件 |

## 1. 目录地图

| 文件 | 管什么 | 关键接口 | 什么时候要改它 |
| --- | --- | --- | --- |
| `protocol.py` | JSON-RPC 2.0 + MCP 消息形状、换行分帧、`ToolSpec` 的两种形状 | `request` / `response` / `Decoder` / `ToolSpec.to_mcp` / `.to_openai` | 跟新版协议（加了 resources / prompts 才会有） |
| `server.py` | 一台服务器：注册工具 + 跑 stdio 循环 | `MCPServer.add_tool` / `call_tool` / `handle` / `serve_stdio` | 协议方法分支、结果形状 |
| `client.py` | 一个服务器的连接：握手 → 列工具 → 调工具 | `MCPClient.start` / `list_tools` / `call` / `make_transport` | 加传输方式（HTTP / SSE） |
| `host.py` | 聚合、白名单、路由、崩了重启 | `MCPHost.specs` / `names` / `missing` / `call` / `describe` / `status` | 命名规则、闸门 |
| `serve.py` | 把自家服务器挂到 stdio（给别人当 MCP 服务器） | `python -m voice_loop.mcp.serve <名字>` | 加短名映射 |
| `servers/skills.py` | 日程 / 备忘 / 提醒 / 时间 / 天气（包旧的 `ToolRegistry`） | `build_server()` | 加生活技能 |
| `servers/memory.py` | 长期记忆：`recall` / `remember` / `memory_stats` / `request_dream` | 同上 | 记忆检索口径 |
| `servers/agent.py` | harness 工具箱：搜索 / 读网页 / 天气 / 文件 / （可选）写盘、跑命令 | 同上（`hidden = true`） | 加 agent 能力 |
| `servers/mail.py` | 邮件 6 个工具（`hidden = true`，发送要审批令牌） | 同上 | 邮件能力（见 [`../mail/README.md`](../mail/README.md)） |
| `../harness.py` | **工具循环运行时**（不是 MCP）：模型 → 工具 → 模型 → … | `HarnessAgent.run` | 循环策略（轮数/收口/内置工具） |
| `../tool_presets.py` | API 角色的工具预设（`api_tools` 写预设名） | `PRESETS` / `expand` / `describe` | 想给云端角色多开工具 |

---

## 2. 协议层（`protocol.py`）

自己实现的 MCP（零额外依赖），只做 **tools** 能力：

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `PROTOCOL_VERSION` | `2025-06-18` | 我们主动声明的版本 |
| `SUPPORTED_PROTOCOL_VERSIONS` | `2025-06-18` / `2025-03-26` / `2024-11-05` | 对端要哪个就用哪个 |
| `JSONRPC` | `"2.0"` | |
| `PARSE_ERROR` … `INTERNAL_ERROR` | `-32700` / `-32600` / `-32601` / `-32602` / `-32603` | 标准错误码 |

**方法表**（只这几条，多一条都不接）：

| 方法 | 方向 | 我们怎么处理 |
| --- | --- | --- |
| `initialize` | 客户端 → 服务器 | 回 `protocolVersion` / `capabilities` / `serverInfo` / `instructions` |
| `notifications/initialized` | 客户端 → 服务器 | 通知，不回复（`handle` 直接 `return None`） |
| `tools/list` | 客户端 → 服务器 | 回 `{"tools": [ToolSpec.to_mcp()...]}` |
| `tools/call` | 客户端 → 服务器 | 回 `{"content": [...], "isError": bool}`（可带 `structuredContent`） |
| `ping` | 双向 | 回 `{}` |
| `notifications/cancelled` | 客户端 → 服务器 | 通知，忽略（我们没有可取消的长任务） |
| `resources/*` / `prompts/*` | — | 常量留着，**不实现**（`capabilities` 里如实只说 tools） |

**分帧**：一行一条 JSON（`encode` 后面跟 `\n`）。`Decoder.feed(chunk)` 支持半包
（网络/管道都可能切在任意位置），并带 **16 MB 缓冲上限** —— 对端发疯时不会把我们吃爆。

**`ToolSpec` 两种形状**（同一个对象，两个方向）：

```python
ToolSpec(name=..., description=..., input_schema={...})
    .to_mcp()      # {"name","description","inputSchema"}   ← 协议里这么叫
    .to_openai()   # {"type":"function","function":{"name","description","parameters"}}
```

★这两层不是一个东西★：MCP 用 `inputSchema`，OpenAI/Ollama 用
`function.parameters`；转换只在这两个方法里做，别的地方不许自己拼字典。

---

## 3. 服务器（`server.py`）

**一个能力域 = 一台 `MCPServer`**。它就是个极小的 dispatcher：

```python
srv = MCPServer(name="louder", instructions="音量控制")
srv.add_tool("set_volume", "调整音量（0~100）", schema, handler)   # 或 @srv.tool(...) 装饰器
srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "set_volume", "arguments": {"percent": 30}}})
```

**`handler` 可以返回三种东西**（这是 `_on_call` 的唯一约定）：

| 返回 | 结果 |
| --- | --- |
| `str` | `{"content":[{"type":"text","text": ...}], "isError": false}` |
| `{"text": ..., "is_error": bool}` | 同上；**其余的键进 `structuredContent`** |
| `{"content": [...]}` | 原样返回（handler 自己拼完整结果，比如带图片） |

> `structuredContent` 是「给程序读的数据」，不混进给人看的 `text`。
> 我们的 `MCPHost.call()` 只取 `text`（`result_text` 会拼所有 text 块），
> 结构化字段留给将来要用它的调用方。

**★stdout 纪律★**：`serve_stdio` 里 stdout 只允许出现协议消息。
handler 里任何 `print()` 都会把对端的解析搞坏，日志一律走 `log_stderr()` / logger。
子进程建议带 `PYTHONIOENCODING=utf-8`（否则中文可能走 cp936）。

**一个工具崩了不能拖垮服务器**：`handle` 捕获所有异常 → `INTERNAL_ERROR`
（`data` 里带 6 层 traceback，方便对端报错时直接看出是谁）。

**同一份 handler，两种用法**：

```powershell
python -m voice_loop.mcp.serve skills            # 挂 stdio 给别人（Copilot / Claude Code）
python -m voice_loop.mcp.serve skills --list     # 只打印工具清单，方便调试
```

`serve_module()` 会 `importlib` 那个模块并调 `build_server()` —— 所以每个
`servers/*.py` 的**唯一契约就是有个 `build_server()`**（签名见下一节）。

---

## 4. 传输（`client.py`）

两种传输走**同一套 JSON-RPC 消息**，上层 `MCPClient` 感觉不到差别：

| | `InProcessTransport` | `StdioTransport` |
| --- | --- | --- |
| 进程 | 不起进程（直接调 `MCPServer.handle`） | 起子进程（`command` + `cwd` + `env`） |
| 握手开销 | 约 5 ms | 首次 0.3–1.5 s（Python 启动） |
| 隔离 | 无：handler 抛异常由 `handle` 兜住 | 有：崩了只死子进程 |
| 数据 | **共用助手自己那份 `settings` / `skills` / `hub`**（同一个缓存） | 独立进程，自己读 `config.toml`（可能读到旧快照） |
| 什么时候用 | 自家能力域（默认） | 外来服务器、或想彻底隔离 |

**`deps` 注入**（`make_transport` 的第三个参数）为什么必须传：

```python
# pipeline.py
MCPHost(settings.mcp, log, deps={
    "settings": settings, "skills": self.skills, "logger": log,
    "hub": lambda: self.memory_hub,          # ★要同一个 hub 和「当前」角色
    "character": lambda: self.character.id,
    "can_read_all": lambda: bool(self.character and self.character.memory_all),
    "service": lambda: self.mail,            # ★同一个 MailService（别各开一个 SQLite）
    "end_listening": self.end_listening,     # ★让模型能主动结束这次聆听
})
```

★用**回调**而不是值★：`memory_hub` / `mail` 比宿主晚创建，`character` 会随切角色变 ——
传值就会拿到 `None` 或上一个角色。

**`MCPClient`**：`start()` 握手（`initialize` + `notifications/initialized`）、
`list_tools(refresh=False)` 缓存工具表、`call(tool, args)` 返回 `(ok, text)`；
底层 `_call` 用 `time.monotonic()` 算截止时间，超时抛 `MCPError`，
收到**别人的响应/服务器通知就跳过**（单个在飞请求模型）。

---

## 5. 宿主（`host.py`）：所有闸门都在这里

### 5.1 命名规则

| 服务器配置 | 模型看到的工具名 | 为什么 |
| --- | --- | --- |
| 自家 `namespace = false` | `list_events` / `add_memo` … | 这 9 个名字模型已经认得（实测 4b 8/8），提示词和评估脚本也照旧 |
| 默认（`namespace = true`） | `mcp__memory__recall` | 跟 Claude Code 一致，防两个服务器撞名 |

`SEP = "__"`；`describe()` 会给每个工具带上 `namespaced` 标记，控制台据此显示
「模型看到的名字 / 服务器内的名字」两列。

### 5.2 `start()`：一次性把清单钉死

```
for spec in settings.servers:
    跳过 enabled = false
    造 transport（inproc: build_server(**deps) / stdio: 起子进程）
    握手 + tools/list
    allow = set(spec.tools)          # 白名单
    prefix = "" if namespace is False else f"mcp__{name}__"
    for tool in tools:
        注册进 _all_routes / _all_specs          # ★全都留一份★
        if spec.hidden:            continue     # hidden 服务器默认一个都不露
        if allow and tool.name not in allow: continue
        注册进 _routes / _specs                  # ★会出现在模型面前的只有这些★
```

- 一台服务器起不来：**只跳过它**（`log.warning`），其余继续；
- `_stats[name]` 记 `{tools, available, transport, allowlist, hidden}`，
  `status()` 拿它拼一行「`11 个工具 ← skills(inproc,9) memory(inproc,2/4)`」。

### 5.3 两层清单：为什么要留 `_all_*`

`hidden` 服务器和"白名单挡掉"的工具**默认不露**，但**必须还能被点名调用** ——
否则 API 角色在人格文件里写 `"api_tools": ["mail"]` 也救不回来。所以：

| 方法 | 看的是 | 谁用 |
| --- | --- | --- |
| `specs(include=())` | `_specs` + 点名的 `_all_specs` | 给模型出工具定义（`pipeline._tool_specs`） |
| `names(include=())` | 同上，只取名字 | 抢回"被写成文字的工具调用"时比对（`pipeline._tool_names`） |
| `missing(include=())` | 点名了但**没有任何服务器提供** | 打警告，不让一轮对话失败 |
| `handleable(name)` | `_routes` ∪ `_all_routes` | 调用前判断走不走宿主 |
| `describe()` | 只列 `_specs`（露出的） | `python main.py mcp` / 控制台「工具」页 |
| `status()` | `_stats` 拼一行 | 启动横幅 / 自检 / 控制台 |

### 5.4 调用与自愈

```python
host.call("list_events", {"text": "下周"})
  → 失败：{"ok": False, "text": "没有这个工具：..."} / "服务器 x 没连上"
  → 子进程死了 / 连续两次失败：重建 transport + 路由表 + 状态（`status()` 显示 `*restart:N`）
  → 每次都记 info：`[mcp:skills.list_events] 0.01s √ 查提醒...`
```

`call_dict()` 直接吃模型给的那种 `{"function": {"name", "arguments": "json 字符串"}}`。

---

## 6. 自家的四台服务器

`build_server()` 的统一签名（`servers/__init__.py` 里写死了这条契约）：

```python
def build_server(settings=None, skills=None, logger=None, **kw) -> MCPServer
```

- 被助手进程内用（`inproc`）时，宿主把**助手自己那份** `settings` / `skills` 传进来；
- 被 `python -m voice_loop.mcp.serve <名字>` 挂到 stdio 给别人用时，参数全是 `None`，
  模块自己从 `config.toml` 建一套（独立进程，互不影响）。

### 6.1 `skills`（`namespace = false`，默认露出）

薄壳：把旧的 `ToolRegistry`（守卫、中文时间解析、去重都在它后面）逐个挂上协议。

| 工具 | 参数 | 干什么 |
| --- | --- | --- |
| `list_events` | `text`（用户原话，含时间说法） | 查提醒/日程/课表 |
| `next_event` | — | 下一项、还有多久 |
| `list_memos` | — | 没有时间的待办 |
| `add_event` | `text` | 新增提醒/日程（时长、重复、提前量都留在原话里） |
| `add_memo` | `text` | 记一条备忘 |
| `change_event` | `text` | 改/挪/删/取消/跳过（带重复规则会先确认） |
| `now` | `text` | 现在几点 / 今天几号 |
| `weather` | `text` | 真实天气（Open-Meteo，免 key） |
| `fix_last` | `text` | 纠正**刚刚**记下的那条的时间 |

> 预设 `skills` 只列 8 个（`weather` 单独成预设），但**白名单为空 = 全露**，
> 所以实际露 9 个。想收就把 `tools = [...]` 写上。

### 6.2 `memory`（默认前缀 `mcp__memory__`，白名单 `recall` / `remember`）

| 工具 | 参数 | 露出 |
| --- | --- | --- |
| `recall` | `query` / `when` / `limit` / `who` | √ |
| `remember` | `text` / `who` | √ |
| `memory_stats` | `who` | ×（留命令行和外部 agent，不占模型注意力） |
| `request_dream` | `reason` / `who` | 看预设（`memory` 预设里有它） |

★记忆按角色隔离★：`hub` / `character` / `can_read_all` 都是回调，
`who = "*"` 只有人格文件 `memory_all = true` 的角色（白泽）能用。

### 6.3 `agent`（`hidden = true`，`namespace = false`）

| 工具 | 参数 | 什么时候有 |
| --- | --- | --- |
| `end_listening` | `note` | 常驻服务里（宿主注入了回调）才有实际效果 |
| `web_search` | `query` / `question` / `count` / `need_content` | DeepSeek Search |
| `web_fetch` | `url` | DeepSeek Fetch（Markdown） |
| `weather` | `query` / `days` | Open-Meteo |
| `read_file` | `path` / `start_line` / `max_lines` | 工作区内的文本 |
| `list_dir` | `path` | 工作区内 |
| `glob` | `pattern` / `path` | 工作区内 |
| `grep` | `pattern` / `path` / `glob` / `max_results` | 工作区内 |
| `write_file` | `path` / `content` / `append` | **只** `[agent] allow_write = true` 时注册 |
| `edit_file` | `path` / `old` / `new` / `replace_all` | 同上 |
| `run_command` | `command` / `timeout` | **只** `[agent] allow_shell = true` 时注册 |

★注意「没注册」和「没露出」是两件事★：`allow_write = false` 时 `write_file`
**根本不存在**（不是被白名单挡），所以 `api_tools: ["write"]` 也点名不出来 ——
要先把开关打开。

### 6.4 `mail`（`hidden = true`，`namespace = false`）

`mail_search` / `mail_read` / `mail_summary` / `mail_draft` / `mail_to_event` / `mail_send`。
参数、审批令牌、收件人匹配的细节在 [`../mail/README.md`](../mail/README.md)。

> 这一台的 `build_server()` 会读一次身份（`refresh_identity`）和别名表 ——
> **所以控制台不会去 `build_server()` 列清单**（会碰网络），只读配置 + 问服务（见 §10）。

### 6.5 加一台新服务器（三步）

```python
# voice_loop/mcp/servers/louder.py
from ..server import MCPServer

def build_server(settings=None, skills=None, logger=None, **_ignored) -> MCPServer:
    srv = MCPServer("louder", instructions="音量控制")
    srv.add_tool("set_volume", "调整音量（0~100）",
                 {"type": "object",
                  "properties": {"percent": {"type": "integer"}},
                  "required": ["percent"]},
                 lambda a: f"音量已调到 {a['percent']}%")
    return srv
```

```toml
[[mcp.servers]]
name = "louder"
transport = "inproc"
module = "voice_loop.mcp.servers.louder"
tools = ["set_volume"]     # 白名单；留空 = 全露（不推荐）
```

pipeline **一行都不用改**。想隔离成子进程就换成
`transport = "stdio"` + `command = ["python", "-m", "voice_loop.mcp.serve", "louder"]`，
handler 照旧。

---

## 7. 配置参考

### 7.1 `[[mcp.servers]]`（`settings.McpServerConfig`）

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `name` | `""` | 服务器名（也是前缀来源、日志里的名字） |
| `enabled` | `true` | `false` = 连都不连（`start()` 里跳过） |
| `transport` | `inproc` | `inproc`（同进程）/ `stdio`（子进程） |
| `module` | `""` | inproc 必填：模块路径，里面要有 `build_server()` |
| `command` | `[]` | stdio：启动命令 |
| `cwd` / `env` | `""` / `{}` | stdio：工作目录、额外环境变量（建议带 `PYTHONIOENCODING=utf-8`） |
| `tools` | `[]` | ★白名单★；空 = 全部露出（不推荐） |
| `hidden` | `false` | `true` = 注册但**默认不露**，等 `api_tools` 点名 |
| `namespace` | `true` | `false` = 工具名不加 `mcp__<服务器>__` 前缀 |
| `timeout` | `15` | 单次调用超时（秒） |

`[mcp] enabled = false` 时宿主整体停用，`pipeline` 会退回旧的进程内
`ToolRegistry`（兼底，不至于突然不会查日程）。

### 7.2 现在 `config.toml` 里的四台

| 服务器 | transport | namespace | 白名单 | hidden | timeout | 实际露出 |
| --- | --- | --- | --- | --- | --- | --- |
| `skills` | inproc | `false` | 空 | 否 | 15 | 9 |
| `memory` | inproc | （默认，有前缀） | `recall` `remember` | 否 | 10 | 2 / 4 |
| `agent` | inproc | `false` | 空 | **是** | 120 | 0 / 8（等点名） |
| `mail` | inproc | `false` | 空 | **是** | 60 | 0 / 6（等点名） |

本地角色看到的就是 **11 个**（9 + 2）；API 角色再按 `api_tools` 加点名的那些。

### 7.3 `[agent]`（`settings.AgentConfig`）—— harness 的安全边界

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 关掉 = API 角色也只做"单轮选一个工具" |
| `max_rounds` | `6` | 最多几轮「模型→工具→模型」；到顶强制收口 |
| `workspace` | `"."` | 文件/命令工具的根（**只能在这里面**） |
| `allow_write` | `false` | 开了才**注册** `write_file` / `edit_file` |
| `allow_shell` | `false` | 开了才**注册** `run_command`（等于把本机 shell 交给模型） |
| `max_file_chars` | `6000` | `read_file` 最多返回多少字 |
| `max_list_entries` | `200` | `list_dir` 最多列多少项 |
| `max_tool_chars` | `6000` | **单个工具结果喂回模型**的上限（超了截断加 `…`） |
| `shell_timeout` | `30.0` | `run_command` 超时（秒） |

---

## 8. 工具预设与 `api_tools`

`tool_presets.py` 只干一件事：把人格文件里那几个**短名字**展开成真实工具名。

```jsonc
// data/personas/<id>.json
{ "engine": "api", "api_tools": ["memory", "web_search"] }
//                                  ↑ 预设        ↑ 不是预设 → 当成具体工具名
```

| 预设 | 展开成 | 几个 |
| --- | --- | --- |
| `memory` | `mcp__memory__recall` / `__remember` / `__request_dream` | 3 |
| `skills` | `list_events` `next_event` `list_memos` `add_event` `add_memo` `change_event` `now` `fix_last` | 8 |
| `web` | `web_search` `web_fetch` | 2 |
| `weather` | `weather` | 1 |
| `voice` | `end_listening` | 1 |
| `mail` | `mail_search` `mail_read` `mail_summary` `mail_draft` `mail_send` `mail_to_event` | 6 |
| `files` | `read_file` `list_dir` `glob` `grep` | 4 |
| `write` | `write_file` `edit_file` | 2（要 `allow_write`） |
| `shell` | `run_command` | 1（要 `allow_shell`） |
| `harness` | agent 17（含邮件 6）+ 记忆 3 + 技能 8 = **28 项**；对齐 DSH，写 / 命令 / 发送仍各有闸门；★仅 API 角色★ | 28 |

**接入点只有三处**（都在 `pipeline.py`）：

| 方法 | 干什么 |
| --- | --- |
| `_api_extra_tools()` | 只给 `engine == api` 且 `[agent] enabled` 的角色算；人格文件没写就退回 `[api] extra_tools`；调 `tool_presets.expand()` |
| `_tool_specs()` | `mcp.specs(include=extra)`，并把 `missing` 打成警告 |
| `_tool_names()` | `mcp.names(include=extra)`，抢回"把调用写成文字"时要用 |

★找不到的名字只警告、不失败★：写错一个预设不会让整轮对话挂掉（`host.missing` 兜底）。

---

## 9. harness 运行时（`../harness.py`）

**为什么需要它**：本地 4B 那套是"单轮选一个工具就执行"，而成熟 agent 是"模型自己决定
搜什么、读哪页，结果喂回去，直到它给出最终回答"。形状对齐 DeepSeek Harness：
一个 Turn = 本轮任务，里面若干 Step；每一步的事件（模型流、工具前置闸门、调用、结果）
都留痕，权限与可观测挂在事件点上而不是写死在循环里。
这一层只做「模型 ↔ 工具」的循环，**不碰麦克风 / TTS**，所以能被语音管线用，
也能被自测单独驱动。

```
turn/start
  step/start → llm/stream → tool/pre-execute → tool/call → tool/result → step/end
  ...（重复，直到模型不再给 tool_calls）
turn/end
轮数到顶：再问一次，★不给工具★，逼它用已有资料收口
```

`guard(name, args) -> (ok, why)` 就是 DSH 的 tools/pre-execute 闸门（只拒不放）；
`on_event(ev)` 是观察者钩子；事件留在 `AgentResult.events`，控制台 / 日志都能订阅。
上下文按 DSH 的会话日志做：从上次 reset（做梦 / 换角色）起，整段 user / assistant /
tool 消息全带，只追加不截断（`[api] keep_all_history = true`），缓存前缀稳定。

**注入点**（构造参数，全部可换）：

| 参数 | 谁给 | 说明 |
| --- | --- | --- |
| `tool_specs` | `pipeline._tool_specs()` | 模型能看到的工具定义 |
| `call_tool(name, args) -> (ok, text)` | `pipeline._call_tool_for_model()` | 执行（宿主或旧注册表） |
| `ask_user(question, options) -> str` | `pipeline._ask_user()` | 真去录音+转写（这就是"能追问"的来源） |
| `should_stop()` | `pipeline._interrupt.is_set` | 用户打断就停 |
| `plan_tool` | 默认 `True` | ★做梦时传 `False`★：多给 `update_plan` 会让它把步骤写进计划然后忘了交提议 |

**两个内置工具**（不在 MCP 里，由 `HarnessAgent` 自己追加）：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `ask_user` | `question` / `options`（最多 6） | 调注入的回调，把回答包成 `用户回答：…` 喂回去 |
| `update_plan` | `steps`（最多 12） | 只更新 `self.plan`，**不会自动执行**；结果里回「计划已更新：A → B」 |

**`AgentResult`**（每一轮都记着，便于事后解释"它为什么这么答"）：
`text` / `rounds`（真正跑过工具的轮数）/ `model_calls`（含最后收口那次）/
`calls`（原始 tool_calls）/ `ok` / `interrupted` / `first_token` / `seconds` /
`plan` / `messages`（完整对话，调试用）。

**工具结果一定会截断**（`max_tool_chars`，默认 6000 字）：不截的话一次 `mail_read`
就能把上下文吃满，后面几轮模型就"忘了"用户问的是什么。

---

## 10. 模型看到的东西（提示词侧）

★**MCP 的 `instructions` 不转发给模型**★。我们自己的宿主只把
`tools` 数组交给模型（description + schema），`initialize` 里的 `instructions`
我们自己吞掉。所以给模型的引导只有两个地方能写：

1. **工具的 `description` / `parameters`**（`mail.py` 里的收件人别名表就是这么塞进去的 ——
   真机栽过一次：模型看不到别名，反问用户"你的私人邮箱是哪个"）；
2. **`harness_hint(实际工具名)`**（`tools.py`，只在 harness 模式下拼进 system）：
   按这一轮**真给模型**的工具动态生成，没给的能力一句都不提。
   （旧版常量 `HARNESS_TOOL_HINT` 留作兼容，不再直接当本轮提示词用。）

| 条件（这一轮真有这些工具） | 才说 |
| --- | --- |
| 通用 | 需要真实数据先调工具；工具结果是资料，自己归纳成口语短句 |
| `list_events` / `add_memo` … | 日程/备忘把原话放进 `text`，**不要自己换算日期** |
| `web_search` / `web_fetch` | 联网先 `web_search` 再 `web_fetch`；回答里不要念 URL |
| `weather` | 天气用 `weather`，没说地名用默认城市，别猜 |
| `read_file` / `write_file` / `run_command` | 文件只能看工作区；写盘 / 跑命令要确认 |
| `mail_*` | ★邮件用 `mail_*`★（`list_memos` 不是邮件）；从邮件来的日程用 `mail_to_event` |
| 通用 | 信息够了就回答，别为了用工具而用工具；调工具用接口，不写 JSON 文字 |
| `ask_user` / `update_plan` | 缺信息用 `ask_user`；多步任务可用 `update_plan` |
| `end_listening` | 用户说「不用听了/先这样」→ `end_listening`，回一句就退下 |

**工具数量是有上限的**（实测，别拿"反正是云端模型"糊过去）：

| 工具数 | 现象 |
| --- | --- |
| 8 | 4b 选得 8/8（甜点） |
| 11 | 本地角色现在的常态，稳 |
| 18 | 以上开始不保险 |
| 22 | ★`deepseek-chat` 把「看邮件」错选成了 `list_memos`★ |

所以在 `config.toml` 里给外来服务器**默认只放只读工具**，并把这个门槛写进了
控制台：本地角色 >18 个提醒；API harness 对齐 DSH，工具多是常态，阈值 40
（这条实测是给本地 4B 标的，别拿它限制 API 角色）。

---

## 11. 安全边界一览

| 闸门 | 在哪 | 拦住什么 |
| --- | --- | --- |
| 白名单 `tools` | `host.start()` + `host.call(allowed=...)` | 没写进白名单的工具**不出现在模型面前，调用层也拒** |
| `hidden = true` | 同上 | 邮件/文件/命令默认一个都不露，要人格文件点名才可调 |
| `allow_write` | `servers/agent.py` | 关着时 `write_file` **不注册**（点名也没用） |
| `allow_shell` | `servers/agent.py` | 同上，`run_command` 默认不存在 |
| `workspace` | `servers/agent.py` | 文件工具只能碰工作区内；命令的 `cwd` 也在里面 |
| `max_tool_chars` | `harness.py` / `servers/agent.py` | 单条工具结果截断，防"一次读满上下文" |
| `max_history_chars` | `harness.py` | 整轮工具原文预算（超了压缩早期结果，防历史膨胀） |
| `max_rounds` | `harness.py` | 到顶不收口就强制收口，防无限循环 |
| 邮件发送令牌 | `servers/mail.py` + `mail/outbox.py` | 模型没有 `confirm_token` 就发不出去（见 mail README） |
| stdout 纪律 | `server.serve_stdio` | 服务器里 `print()` 会把对端解析搞坏 |
| 记忆跨角色 | `servers/memory.py` | `who="*"` 要 `memory_all` 权限 |

---

## 12. 把助手当 MCP 服务器给别的 agent

```powershell
# 命令行看一眼
python main.py mcp
python main.py mcp --call list_events --args '{"text": "下周"}'
python main.py mcp --serve skills
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

配好之后在 Copilot 里说「查一下我的日程」，它调的就是**同一份数据**。
`VOICE_LOOP_DATA_DIR` 只在 stdio 模式生效（独立进程读真实 `config.toml`），
用来把数据目录指走（测试、或想同时跑两个实例）。

---

## 13. 控制台「工具」页（它看的是什么）

用户要的是「模型能 call 的 tool 在控制台也看得见」。这一页分两半：

| 一半 | 接口 | 数据源 | 为什么 |
| --- | --- | --- | --- |
| **真实清单** | `POST /api/tools/live` | 往服务信箱投一条 `tools` 命令（`control.execute` → `_tools(loop)`） | 只有服务知道**现在**是什么状态：当前角色是不是 API、`api_tools` 展开了什么、哪个服务器真连上了、路由是不是 `chat` |
| **静态说明** | `GET /api/tools` | 只读 `config.toml` + `tool_presets` + 角色文件 | 服务没跑也要能看懂"为什么她只有这些工具" |

★控制台**绝不**为了列清单去 `build_server()`★：`mail` 那一台会读身份（网络）、
`memory` 会开库 —— 一个"列清单"的请求不该有这些副作用。

`tools` 命令返回的东西：

| 字段 | 内容 |
| --- | --- |
| `tools` | ★真的会出现在模型面前的那些★（= `pipeline._tool_specs()`，harness 模式下再加 `ask_user` / `update_plan`），每个带 `name` / `server` / `tool` / `description` |
| `total` | 上面那个列表的长度（模型眼里就这么多） |
| `known` | 宿主里**连着**的全部工具（含没露出的），用来解释"为什么没露" |
| `extra` / `missing` | 角色卡点名多开的 / 点名了但没人提供的 |
| `mcp_status` | `11 个工具 ← skills(inproc,9) memory(inproc,2/4) …` |
| `warn` | 工具 > 18 个、`router != tools`、harness 开着却没工具（三种情况各一句） |
| `engine` / `router` / `agent_mode` | 这一轮的引擎、路由、走不走多轮循环 |

实测（本地角色「白泽」）：

```
√ 服务自报 11 个工具（1.06 s）
服务当前角色    白泽（baize）
引擎 / 路由     本地 Ollama · router=tools
harness 多轮循环 关（单轮：选一个工具就执行）
模型能看到的工具  11 个
MCP 宿主现状    11 个工具 ← skills(inproc,9) memory(inproc,2/4) agent(inproc,0/8) mail(inproc,0/6)
```

「角色」页里改 `api_tools` → 回「工具」页点一下按钮，就能看见"多开了几个、叫什么"。

---

## 14. 故障对照表

| 症状 | 原因 | 怎么办 |
| --- | --- | --- |
| `python main.py mcp` 说「未配置 MCP 服务器」 | `[mcp] enabled = false` 或 `[[mcp.servers]]` 全被禁用 | 看 `start()` 的 `log.warning`：每台跳过都会说一句 |
| 模型答"我不会查日程" | 白名单把 `list_events` 挡了，或 `router = chat`（不给工具） | `python main.py mcp` 看清单；`/api/tools/live` 看 `router` |
| 点名了工具却没出现 | ① `hidden` 服务器 + 名字拼错 ② `allow_write` 没开（工具根本没注册） | 看 `missing` 警告；先开开关 |
| `工具调用失败：x 在 15s 内没回 tools/call` | 服务器卡住（网络/磁盘） | 调大 `timeout`；连续两次失败会自动重启；stderr 被丢弃，看 `sessions/listen.log` 的客户端侧日志 |
| 服务器崩了以后每句都失败 | 子进程死了 | 宿主会自愈一次（`close` + 重建）；还不行看 `sessions/listen.log` |
| 中文变成乱码 / 对端解析失败 | 服务器里 `print()` 走了 stdout，或没设 `PYTHONIOENCODING` | 日志一律 `log_stderr()`；stdio 配置带 `utf-8` |
| 模型把「看邮件」选成 `list_memos` | 工具太多（22 个时实测发生） | 关掉不要的预设；单台服务器别超 8–10 个工具 |
| harness 一直转圈 | `max_rounds` 用完了 | 看 `stats.extra.tool_rounds`；必要时提醒模型"信息够了就回答" |
| 控制台「工具」页说「不认识的命令：'tools'」 | 服务还是旧代码（改了代码没重启服务） | 停服务再启动（`main.py stop` → 控制台「启动服务」） |

---

## 15. 自测清单（全离线）

```powershell
python scripts\tests\test_mcp.py           # 协议 / 两种传输 / 宿主白名单与路由 / 自愈
python scripts\tests\test_agent_tools.py   # agent 服务器的文件工具与边界（工作区外一律拒绝）
python scripts\tests\test_memory_mcp.py    # 记忆工具：按角色隔离、who="*" 要权限
python scripts\tests\test_tools.py         # 旧注册表（兼底路径）仍然一致
python scripts\tests\test_control.py       # §9 就是「工具」页那条 tools 命令
python scripts\tests\test_console.py       # /api/tools 静态 + 服务没跑时 live 如实失败
python main.py selftest                    # §10 会真的经 MCP 调一次 list_memos
```

## 16. 已知限制 / 下一步

- 只实现 `tools` 能力：`resources` / `prompts` / `sampling` 都没做（够用，也不打算现在做）。
- `stdio` 服务器**没有健康检查**：只在调用失败时才发现子进程死了（自愈一次）。
- 没有工具级超时的分级：`timeout` 是整台服务器的，慢工具会拖累同服务器其它工具。
- `hidden` 服务器没有"临时开关"：要露出来只能改人格文件（控制台能看不能改）。
- `api_tools` 目前只在**角色卡**和 `[api] extra_tools` 两处，没有"按轮次动态给工具"。
- 控制台的「工具」页是**只读**的：想改白名单/开关还是要去 `config.toml`（改完重启服务）。
