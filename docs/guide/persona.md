# 角色设定（多角色 + 声线 + 唤醒词）

> 从 [`README.md`](../../README.md) 拆出：第 14 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。

---
## 14. 角色设定（多角色 + 不同唤醒词）

人设不再是 `config.toml` 里那一大段 `system_prompt`，而是**索引 + 独立人格文件**两层：

```
data/characters.json          ← 索引：只写「加载哪些人格」+ 默认是谁（= 谁可被唤醒）
data/personas/baize.json      ← ★默认角色「白泽」：原创助手★（名字/风格/台词都是自己写的，
                                声线用公开的出厂 Piper —— 见下面「默认角色是原创的」）
data/personas/kaltsit.json    ← 一个人格一个文件（名字/背景/称呼/风格/示例台词/唤醒词…）
data/personas/amiya.json
data/personas/别人.json        ← 放着不挂：不会被加载，也不会被唤醒
```

**为什么要拆**：以前所有角色挤在一个文件里，想试第三个角色的设定就得先写进去 ——
而写进去就等于「挂上了」（会被唤醒词命中、会切人设）。现在**设一个角色 ≠ 挂上她**：
人格文件随手写，索引里加一行才上线；临时停用也只改索引一行，人格文件不动。
系统提示词仍由程序按字段拼（`voice_loop/persona.py`）——**你填字段，不写提示词工程**。

```powershell
python main.py persona                      # 现在挂着谁（含唤醒词、人格文件路径）
python main.py persona --show 阿米娅          # 看这一个角色的所有字段
python main.py persona --show 阿米娅 --prompt # 看她拼出来的 system prompt（想调措辞时看这个）
python main.py persona --voices             # 每个角色的声线装没装
python main.py persona --split              # 旧版（角色全写在一个文件里）→ 一键拆成独立文件
python main.py --character amiya text       # 临时用某个角色（chat / listen / text / ask 都支持）
python main.py memory --who amiya            # 她记得什么、知识库读到几段（第 15 节）
```

### 索引长什么样

```jsonc
// data/characters.json
{
  "default": "baize",                  // 没指定角色时用谁（默认是原创助手白泽）
  "characters": [
    { "id": "baize", "file": "personas/baize.json", "default": true },  // ★默认角色★
    { "id": "kaltsit", "file": "personas/kaltsit.json" },   // 挂上 = 可以被「凯尔希」唤醒
    { "id": "amiya",   "file": "personas/amiya.json", "enabled": false },  // 临时停用
    "personas/lucy.json"                // 简写：只给路径也行
  ]
}
```

`file` 相对索引所在目录（也可以写绝对路径）；`id` 可省略（默认用文件名）。
`personas/` 里**没被索引引用**的文件会出现在 `python main.py persona` 的
「库里另有 … 没挂上」那一行——想让她上线，加一行就行。

### 一个人格文件长什么样

```jsonc
// data/personas/kaltsit.json
{
  "id": "kaltsit",                 // 可省略（用文件名）
  "name": "凯尔希",                 // ★名字★
  "title": "罗德岛医疗主管",         // 身份，一句话
  "background": "罗德岛的医疗主管……", // ★背景★：世界观 + 性格由来 + 和「我」的关系
  "user_title": "博士",             // ★对「我」的称呼★（提示词里统一用它）
  "wake_words": ["凯尔希"],         // ★唤醒词★：喊谁切谁
  "aliases": { "凯尔希": ["老猫", "开尔希"] },   // 常被听错的写法
  "ack": "我在，博士。",             // 被唤醒时的应答（留空则不出声）
  "style": ["冷静、克制", "事务性内容直接给结论"],  // ★说话风格★，每条一句
  "rules": ["不知道就直说不知道"],                  // 硬性要求
  "avoid": ["不要用「作为一个AI」"],                // 明确不要出现什么
  "lines": [                        // ★示例台词★（场景 → 台词）
    { "scene": "被唤醒", "text": "我在，博士。" }
  ],
  "voice": "",                      // 可选：这个角色用自己的 piper 声线
  "voice_ref": "",                 // 可选：克隆音色的参考音频（backend = zipvoice 时用）
  "voice_ref_text": "",             // 可选：那条参考的逐字文本（空 = 同名 .txt / 清单 / 自动转写）
  "voice_refs": {},                 // 可选：★分风格的多条参考★（当前模型下唯一的「情绪控制」手段）
                                    //   {"calm": "data/personas/<id>/a.wav",
                                    //    "催促": {"ref": "...b.wav", "text": "那句话"}}
                                    //   取不到那一档就退回 voice_ref；素材不会挑→ pick_voice_ref.py
  "backend": "",                    // 可选：这个角色用哪个 TTS 后端（piper / zipvoice / api；
                                    //   空 = 跟 config.toml 的 [tts] backend）
  "tts_voice": "",                  // 可选：backend = api 时的官方音色（stella / tide）
  // ↓↓↓ 可选：这个角色的「脑子」跑在哪（默认跟全局，全离线）↓↓↓
  "engine": "",                     // "" = 跟 [api] engine；local = 本机 Ollama；api = 云端接口
  "api_model": "",                  // 可选：API 模型名（空 = 跟 [api] model，如 deepseek-chat）
  "api_base": "",                   // 可选：API 地址（空 = 跟 [api] base_url）
  "api_key": "",                    // 可选：★别把真 key 写进要提交的文件★（留空 = 读环境变量）
  "api_tools": [],                  // 可选：API 角色额外开放的工具（预设名或工具名）
  // ↓↓↓ 下面这几个是「它知道什么」（第 15 节）↓↓↓
  "knowledge": ["data/knowledge/x/设定.md"],  // 可选：专属知识库文件/目录
  "worlds": ["明日方舟"],           // 可选：★挂在哪个世界观上★（同一个 IP 的角色共享一套）
  "world": "",                     // 可选：把它的世界观直接写在这
  "knowledge_title": "",           // 可选：专属世界观的标题
  "knowledge_shared": true,         // 可选：false = 连共享知识库也不看（全隔离）
  "memory_all": false,              // 可选：能不能查所有人的记忆（只有白泽开）
  "knowledge_all": null,            // 可选：能不能看全部知识库（默认跟随 memory_all）
  "temperature": 0.0,               // 可选：覆盖全局温度（0 = 用全局）
  "enabled": true,                  // 可选：false 则不参与唤醒（索引里写也一样）
  "notes": "给自己看的备注，不进提示词"
}
```

### 每个角色的 AI 引擎：本地 / API（可选）

默认仍然是**全离线**：`[llm]` 指向本机 Ollama。但有些角色想要云端模型的能力，
所以在人格文件里给每个角色单独选「脑子」跑在哪 —— **喊谁就用谁的模型**：

```jsonc
// data/personas/bluefish.json（原生 API 角色「蓝色大肥鱼」）
{
  "id": "bluefish",
  "name": "蓝色大肥鱼",
  "engine": "api",              // ★这个角色走云端★
  "api_model": "deepseek-chat", // 空 = 跟 [api] model
  "api_tools": ["harness"]       // 蓝色大肥鱼的真实配置（展开成 28 项 DSH 工具）
}
```

```toml
[api]
enabled = true
engine = "local"                          # 角色卡上没选的角色用哪个
base_url = "https://api.deepseek.com"     # 任何 OpenAI 兼容接口都行
api_key_env = "DEEPSEEK_API_KEY"          # ★密钥放环境变量，别写进 git★
api_key_file = ""                         # 或者：指向仓库外的一行 key
model = "deepseek-chat"
extra_tools = []                          # API 角色额外开放的工具（预设名或工具名）
```

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."          # PowerShell（当前窗口有效）
python main.py text --character bluefish   # 用 API 角色聊一句
```

**密钥 / 个人路径放在 `config.local.toml`**（被 `.gitignore` 忽略，永远不会提交）：

```toml
# config.local.toml（本地才有；主 config.toml 里 api_key_file 保持空）
[api]
api_key_file = "D:/你的路径/assistant.txt"
```

优先级：`api_key` → `api_key_file` → `api_key_env`。主配置里这几项都是空的，
所以仓库里不会出现你的本地路径或密钥；启动时会打印一行「已合并本地覆盖」。

几条行为约定：

| 情况 | 会发生什么 |
| --- | --- |
| 角色卡写 `"engine": "api"`，key/地址没配好 | **自动退回本地**并在日志里说清楚 —— 喊醒了却一个字说不出，比退回本地糟 |
| 本地角色 | 完全不碰 `[api]`，还是全离线 |
| 调用中途 API 断了 | 这一轮如果还没往外念字，就**改用本地模型重来**；已经念出去半句则如实报错 |
| 记忆总结 / 看图 | **永远走本地**（不花云端 token，断网也不丢记忆） |
| 改了角色卡上的引擎 | 服务会热加载人格文件，几秒内生效；不用重启 |
| 控制台「角色」页 | 第 ③ 组「AI 引擎」就是这几栏，填完点保存即可 |

**额外工具**：本地 4B 能稳定驾驭的工具就那十个，所以默认所有角色一样。
API 角色可以在 `api_tools`（或 `[api] extra_tools`）里按 **预设名** 或 **具体工具名**
点名多开。现在有的预设是 `memory`（recall / remember / request_dream）、`skills`（八个生活技能）、
`web`、`weather`、`files`、`mail`（含 `mail_send`，但发送要审批卡签名）、`harness`（对齐
DeepSeek Harness 的 28 项：agent + 邮件 + 记忆 + 技能）；
也可以直接点名一个被白名单挡住的工具，例如
`"api_tools": ["mcp__memory__memory_stats"]`。预设表在
[`voice_loop/tool_presets.py`](../../voice_loop/tool_presets.py)：加一个预设 = 加一行，
流水线不用改；名字找不到只打警告，不会让这一轮失败。

### 声音（TTS）：换成任何模型 / 任何 API 都不用改代码

TTS 这一层现在是**数据驱动**的：一个「后端（provider）」= 一个声明文件
（`voice_loop/tts/providers/*.py`），它自己说清能力、参数、怎么造引擎。
加一个新模型/服务**只加一个文件 + 一段 config**，pipeline / settings / 控制台一行不改。

内置的五个：

| provider | 在哪 | 要什么 | 能力 |
| --- | --- | --- | --- |
| `piper` | 本地 | 一个 onnx（`models/tts/piper/`） | 低延迟；★所有后端的保底退路★ |
| `zipvoice` | 本地 | 克隆模型 + 参考音频 | 零样本克隆、分风格参考 |
| `deepseek` | 云端 | `[api]` 的 key | 官方音色 `stella`/`tide`；按字符计费 |
| `edge` | 云端 | **什么都不用**（免费） | 音色多、可调语速；`pip install edge-tts` |
| `http` | 云端 | 一段 config（url/headers/body） | ★任意云端 TTS★：不写 Python |

```toml
[tts]
provider = "zipvoice"          # 默认用哪个；角色卡可以覆盖
fallback = ["piper"]           # 缺模型/缺 key 时依次往下试（★末尾永远还有 piper★）
max_loaded = 1                 # 同时留几个引擎（克隆模型几百 MB）

[tts.providers.piper]          # 各后端的私有参数：加后端只往这里加，不动 dataclass
voice = "zh_CN-huayan-medium"
[tts.providers.deepseek]
voice_id = "stella"
fallback_voice = "zh_CN-huayan-medium"
[tts.providers.edge]
voice_id = "zh-CN-XiaoxiaoNeural"
rate = "+0%"
[tts.providers.http]           # 「给个 API 就能接」：取消注释、填好就能用
# url = "https://api.example.com/v1/audio/speech"
# headers = { Authorization = "Bearer {key}" }
# body = '{"input": "{text}", "voice": "{voice}", "response_format": "mp3"}'
# audio_format = "mp3"; key_env = "EXAMPLE_TTS_KEY"
```

角色卡上只写**一份**「嗓音描述」（不再有 `backend` / `voice_ref` / `tts_voice`
那样一堆平铺字段）：

```jsonc
// data/personas/bluefish.json
"tts": {
  "provider": "deepseek",                  // 空 = 跟 [tts] provider
  "voice_id": "stella",                    // 官方音色 / piper 声线名 / edge 音色
  "fallback": ["piper:zh_CN-huayan-medium"],   // 云端挂了用这个嗓子
  "params": {"tts_block_seconds": 0.2}     // 这个后端的私有参数（原样透传）
}
```

| 设计点 | 为什么 |
| --- | --- |
| **能力协商，不认名字** | pipeline 只问 `clone` / `voice_id` / `cloud` 这些能力，永远不写 `if backend == "piper"` |
| **每个 spec 一份配置副本** | 以前切角色 = 改全局配置 + unload（两个角色没法共存）；现在全局只表示「默认值」，切换互不影响 |
| **降级链收在注册表** | 缺模型/缺 key 只是「换下一个」，配置问题只提示一次；云端中途失败 → 这一句回本地，不哑 |
| **`cloud` 是能力，不是注释** | 自测里有一条「纯本地角色解析出来不是云端」，隐私不变量靠它守着 |
| **控制台表单从注册表生成** | 声音后端下拉、可选音色都是现算的 —— 再不会出现「下拉里没有新值，浏览器把她的音色悄悄清空」 |

**从旧格式升级**：旧人格文件里的 `backend` / `voice` / `voice_ref` / `voice_refs` /
`voice_model` / `tts_voice` 不再生效，用一次迁移脚本搬过去（留 `.bak-tts`）：

```powershell
python scripts/tools/migrate_tts_format.py --dry-run   # 先看会改成什么
python scripts/tools/migrate_tts_format.py             # 真写
```

**加一个后端**（全程不改别处）：

```python
# voice_loop/tts/providers/my_tts.py
from ..spec import CAP_CLOUD, CAP_VOICE_ID, FieldSpec, ProviderSpec

PROVIDER = ProviderSpec(
    id="my-tts", label="我的 TTS",
    capabilities=frozenset({CAP_VOICE_ID, CAP_CLOUD}),
    fields=(FieldSpec("voice_id", "音色", "select", options=("a", "b")),),
    ready=lambda settings: (True, ""),
    create=lambda settings, spec: MyEngine(settings, spec),
    hint="…",
)
```

### 官方 TTS：让 API 角色用云端嗓子（可选）

「蓝色大肥鱼」不只是脑子走 DeepSeek，**嗓子也可以**。官方接口是
`POST https://api.deepseek.com/v1/tools/tts`，返回 24 kHz / 16-bit / 单声道 PCM：

```toml
[api]
tts_enabled = true
tts_voice = "stella"       # stella（女，默认）/ tide（男）；角色卡可以覆盖
tts_format = "pcm"         # 直接可播的裸 PCM（也支持 wav）
tts_block_seconds = 0.2    # 边收边播：收够 0.2 秒就先交给扬声器
```

```jsonc
// data/personas/bluefish.json
"tts": {
  "provider": "deepseek",                       // ★声音走官方 TTS★
  "voice_id": "stella",                         // 空 = 跟 [tts.providers.deepseek] voice_id
  "fallback": ["piper:zh_CN-huayan-medium"]     // ★退路★：API 不可用时用这个 Piper 声线
}
```

| 情况 | 会发生什么 |
| --- | --- |
| `[api]` 没配 key / `tts_enabled=false` | 降级链退到 `piper`（用 `fallback` 里那把嗓子，或 `[tts.providers.piper]`） |
| API 400 / 429 / 503 / 超时 | 还没出声 → 这一段改用本地；已经播了半句 → 保留已播部分，不重复 |
| **`provider` 那一栏被清空** | 她就去跟 `[tts] provider`（本项目默认是**克隆**）——听起来就是「换了个人」。控制台保存时会明确告警，概览页也会写出她实际用的那一路 |
| 音色 | 官方只有 `stella` / `tide`，**没有克隆、没有语速参数** |
| 计费 | 每 10000 字符 $0.45（限时 $0.225）；中文一个字按 2 字符算 |
| 控制台 | 角色卡第 ⑤ 组（后端下拉由注册表生成）+ 概览页「TTS：deepseek · stella」这种真话 |

### API 角色的 harness agent（多轮工具 + 联网 / 文件 / 命令）

本地 4B 仍是「选一个工具 → 直接念工具的话」；**API 角色则按成熟 harness agent 的做法**：
模型 → 工具 → 模型，最多 `[agent] max_rounds` 轮，工具结果喂回模型，
最后由模型自己收口成一句简短的中文回答（而不是把工具原文直接念出来）。

```toml
[agent]
enabled = true
max_rounds = 6
workspace = "."           # 文件/命令工具的根目录
allow_write = false       # write_file 默认关
allow_shell = false       # run_command 默认关（★高风险★）
max_tool_chars = 6000     # 单个工具结果喂回模型的上限
```

工具清单（`namespace = false`，名字就是 harness 风格）：

| 工具 | 来源 | 什么时候有 |
| --- | --- | --- |
| `web_search` | DeepSeek `POST /v1/tools/search` | `api_tools` 含 `web` |
| `web_fetch` | DeepSeek `POST /v1/tools/fetch` | `api_tools` 含 `web` |
| `weather` | Open-Meteo（免 key；解析在 `voice_loop/weather.py`） | `api_tools` 含 `weather` |
| `read_file` | 工作区文本文件（带行号） | `api_tools` 含 `files` |
| `list_dir` | 工作区目录 | `api_tools` 含 `files` |
| `glob` | 按通配符找文件（`**/*.py`） | `api_tools` 含 `files` |
| `grep` | 在文件内容里搜正则 | `api_tools` 含 `files` |
| `mail_search` / `mail_read` / `mail_summary` | 邮件查 / 读 / 摘要 | `api_tools` 含 `mail` |
| `mail_draft` | 起草邮件（不发送） | `api_tools` 含 `mail` |
| `mail_send` | 真发送（必须带审批卡签发的 `confirm_token`） | `api_tools` 含 `mail` |
| `mail_to_event` | 把邮件提成日程 | `api_tools` 含 `mail` |
| `mcp__memory__recall` / `remember` / `request_dream` | 记忆查 / 记 / 主动整理 | `api_tools` 含 `memory` |
| `list_events` / `add_event` / … | 技能层日程 / 备忘 / 报时 | `api_tools` 含 `skills`，或 `harness` |
| `write_file` | 写工作区文件 | `[agent] allow_write = true` 且点名 |
| `edit_file` | 精确替换文件里的一处文本 | `[agent] allow_write = true` 且点名 |
| `run_command` | 工作区 shell | `[agent] allow_shell = true` 且点名 |
| `ask_user` | ★内置★：向用户提问（可带选项） | 对话型 API 角色默认有（dream 等专用 harness 不注入） |
| `update_plan` | ★内置★：把步骤计划写进这一轮的状态 | 对话型 API 角色默认有（同上） |

`ask_user` 的两种模式：

- **语音**：先念出问题（`speak_text`），再用麦克风听、转写；期间会清掉静音状态，
  所以即使刚才按了静音也能听见回答。
- **纯文本**（`enable_listening=false` / 没有麦克风）：直接 `input()` 读一行。

`update_plan` 不念给用户听，只把步骤记到 `TurnStats.extra["plan"]`（日志 / session 可见，
控制台「工具」页显示的是可调用能力，不是本轮计划），
模型想怎么规划都行，不会因为「计划」多占一轮回答。

`api_tools` 里写预设名即可：

```jsonc
// data/personas/bluefish.json
{ "api_tools": ["web", "files", "weather"] }
```

- `web` → `web_search` + `web_fetch`
- `weather` → `weather`（真实天气，免 API key）
- `files` → `read_file` + `list_dir` + `glob` + `grep`
- `write` → `write_file` + `edit_file`（需要先在 `[agent]` 里显式开启）
- `shell` → `run_command`（需要先在 `[agent]` 里显式开启）
- `harness` → 对齐 DSH 的 **28 项**：agent 17（已含邮件 6）+ 记忆 3 + 技能 8；
  ★只对 `engine = "api"` 的角色生效★，本地小模型即使写了也不会展开；
  写 / 命令 / 发送各有闸门，不可用的自动跳过并告警

| 设计点 | 原因 |
| --- | --- |
| **默认隐藏**（`hidden = true`） | 本地 4B 看不到这些工具，不会误调；只有 API 角色点名才露出 |
| **多轮工具循环** | 搜到结果后模型可以继续 `web_fetch` 读正文，或直接回答；不再把搜索摘要当最终答复 |
| **运行时独立** | 循环 / `ask_user` / `update_plan` 都在 [`voice_loop/harness.py`](../../voice_loop/harness.py)，流水线只提供「念出来」「问用户」两个回调，方便单测 |
| **URL 只给模型看** | 工具结果里有 URL（模型可读），最终回答由模型组织，不会念网址 |
| **文件边界** | 所有路径都限制在 `[agent] workspace` 里；`..` / 越界绝对路径会被拒 |
| **隐私路径** | 即使 workspace 指到项目根，`sessions/`、`data/memory/`、`data/knowledge/`、`data/vision/`、`data/backup/`、`config.local.toml` 也永远不给模型看/写 |
| **写/命令默认关** | `write_file` / `edit_file` / `run_command` 只有显式开启才出现在工具列表里；命令等于把本机 shell 交给模型，风险自负 |
| **会问也会等** | `ask_user` 会把问题念出来并真的等回答（语音或键盘），答案作为工具结果喂回模型；★取消 / 没听清 / 无回调记为 `ok=False`，不会被当成"用户说了 yes"★ |
| **计划不占回答** | `update_plan` 只记录步骤，不念出来也不额外花一轮；写在日志 / session 里（控制台工具页显示的是可调用能力，不是本轮计划） |
| **DSH 形状的事件轨迹** | 一个 Turn 分若干 Step；`turn/start` → `step/start` → `tool/pre-execute` → `tool/call` → `tool/result` → `step/end` → `turn/end` 都留在 `AgentResult.events`，观察者可通过 `on_event` 订阅；权限闸门只拒不放 |
| **整段会话上下文** | API harness 从上次 reset（做梦 / 换角色）起保留**全部** user / assistant / tool 消息，只在尾部追加；`keep_all_history = true`，不再按轮数截断，云端缓存前缀才不会掉 |
| **动态信息不切前缀** | 时间 / 工具提示 / 记忆摘要不再当 system 插在历史与当前用户之间，而是并入**当前用户消息**并随该轮一起写进历史；有完整历史后不再重复注入 L1 尾巴，usage 事件也记进缓存账 |
| **计费** | Search / Fetch 各 $0.75 / 1000 次成功请求；`count` 不影响单价；空结果/失败/超时不计费 |
| **隐私** | 查询内容会发给 DeepSeek；本地角色完全不会触发联网工具 |

控制台不用额外配置：角色卡第 ③ 组「额外工具」里写 `web` / `files` 即可
（一行一个，也可以写具体工具名）。


### 身份卡怎么写才「不出戏」（★4B 小模型的硬限制★）

提示词是程序按字段拼的，但**字段怎么写**直接决定她像不像正常人。实测结论（细节见工程日志 §58）：

| 你写的 | 会出什么事 | 改成 |
| --- | --- | --- |
| `style` 里带**完整例句**（比如「博士，你又熬夜了」） | 4B 会把这句**原样搬**到不相干的回合里 —— 白天也说你熬夜 | 写成**描述**：`关心人的方式很克制：只在对方说到累或忙的时候接一句` |
| 大段 `background`（几百字）+ 几十条 `lines` | 提示词到 2000+ tokens，越过 `num_ctx` 会被**从头截断**，人设整段丢 → 「作为一个人工智能助手……」 | 背景留 2~3 句；台词靠导入，提示词里只放 6 条 |
| 带 `★` `**` `【】` 的写法 | 模型把提示词当成「一份要分析的文档」，开始交作业：拆解要求、列要点、复述提示词 | 写成平常的话；渲染时会自动洗掉，但别指望它 |
| 写「不要自称 AI」「不要用列表」 | ★否定句会被照念★：它就在回答里冒出「我是白泽（AI 助手）」 | 只说肯定句（`rules`/`avoid` 仅在 `prompt_style = "rich"` 时进提示词） |

```toml
[persona]
prompt_style = "compact"   # 默认：人设卡式，200~300 字（4B 只能吃这个）
                           # "rich"：分节文档式，信息更全 —— 给 20B 以上的模型用
lines_in_prompt = 0        # 提示词里放几条示例台词（0 = 默认 6；越多越容易被复述）
```

体检一下你的人格文件（有告警就退出码 1，已进测试套件）：

```powershell
python scripts/lint_persona.py               # 所有角色
python scripts/lint_persona.py --who kaltsit  # 只看一个
```

另外还有一层**运行时的兜底**：回答的第一句如果是在拒绝扮演、议论提示词、或者复述人设，
这段会被丢掉（一个字都不念），换一句话的极简人设重答一次 —— 见 `persona.reply_problem()`。

### 一次把一个角色导进来（台词 + 档案，可批量）

一个角色一个文件夹，素材按下面三种命名放进去，然后跑一条命令：

```powershell
python scripts/import_persona.py                      # 先看会做什么（★默认试运行，什么都不写★）
python scripts/import_persona.py --apply              # 真导入（写前自动 .bak）
python scripts/import_persona.py --only clementia --apply
python scripts/import_persona.py --create --add-index --apply   # 连新角色的骨架一起生成
```

```
data/personas/clementia/
    任命助理.wav  交谈1.wav  …   ← 语音素材（克隆参考、声线体检都用它）
    clementia.txt              ← 台词清单（一行名字 + 一段正文）→ 追加到她的「示例台词」
    profile.txt                ← 角色档案 / 角色卡 / 设定     → 写成知识库（检索命中才进提示词）
```

| 素材 | 去哪儿 | 什么时候用得上 |
| --- | --- | --- |
| `<角色>.txt` 台词清单 | 人格文件的 `lines`（示例台词） | 每一轮都在提示词里，学语气用；**只追加，已有的不动** |
| `profile.txt` 档案 / 设定 | `data/knowledge/<id>/档案.md`（知识库） | 只在**检索命中**时进提示词（十几 KB 的档案塞进提示词会把首字拖慢，也稀释人设） |
| 音频 | 不动（本来就在她的文件夹里） | 挑参考音：`python scripts/tools/pick_voice_ref.py --who <id>` |

★**不看是哪个游戏、也不要求模板**★：识别与解析只看**结构** ——
`① 短行标题 + 后面有正文` 当章节、`② 【键】值` 或 `键：值` 当元信息、
`③ 「提升信赖至 50% 以查看」这类解锁门槛行`按通用形状丢掉（`--keep-gates` 留着）。
所以换成别的游戏、英文的 Markdown 角色卡、甚至一页**没有任何标题的散文**都能进来；
判断错了可以用 `--as profile` / `--as lines` 强制指定（界面里也有这个开关）。
判断依据写在 `voice_loop/persona_import.py` 的模块说明里，自测用**两套完全不同的排版**各走一遍。

**控制台也能做**：「角色」页最下面那张 **批量导入素材** 卡片 —— 扫描角色文件夹 → 勾选项 →
「试运行」/「导入」，和上面那条命令是**同一份实现**（`voice_loop/persona_import.py`）。
新角色第一次导入时勾上「生成骨架」「加进索引」，然后就**点她的卡片**把风格那几栏补完。

> 档案默认放 `data/knowledge/<角色id>/`：**只有她自己检索得到**（凯尔希查「阿戈尔」查不到她的档案）。
> 想让同一个 IP 的角色共享一套，用 `--world 明日方舟` 放进 `data/knowledge/_worlds/明日方舟/`，
> 同时会往人格文件的 `worlds` 追加那个名字（只加不减）。

### 默认角色是原创的（白泽）

默认角色是一个**自己写的角色「白泽」**（神话里「知万物之名」的瑞兽 —— 刚好贴合
「替你记事、替你念日程」这个定位），目的有两个：**不让这个项目带任何需要授权的素材**，
也让它换个机器、换个人都直接能用。三条规矩（`scripts/tests/test_persona.py` 第 [7] 节守着）：

1. **名字 / 背景 / 风格 / 台词都是自己写的** —— 不用任何作品的角色名、原文台词；
2. **声线固定用公开的出厂模型**（`zh_CN-huayan-medium`）；
3. ★**`voice_ref` / `voice_model` 必须为空**★ —— 这是最容易坏的一条：全局 `[tts] backend`
   是 `zipvoice`（克隆），角色不写 `backend` 就会跟着全局去用某条**参考音频**；
   白泽写了 `"backend": "piper"`，所以它走的是出厂声线，跟克隆素材完全无关。

想换成别的原创角色：新写一个人格文件 + 在索引里把 `default` 改过去就行。
想让某个角色也用出厂声线（不克隆）：在它的人格文件里加 `"backend": "piper"`。

### 多角色怎么工作

1. **喊谁切谁**：唤醒词写在各人格文件的 `wake_words` 里，`WakeWordMatcher` 命中时
   会告诉上层「是哪位」，服务就切人设（顺带换应答语；写了 `voice` 就一起换声线）。
   默认挂着两个：喊「凯尔希」是凯尔希，喊「阿米娅」是阿米娅，喊回去就切回来。
2. **切换会清对话历史**：新角色不继承上一位的口气（否则她会学着凯尔希的简报腔说话）。
3. **热加载**：索引**和人格文件**的改动都会被监听到（几秒内生效，不用重启）；
   改人设、补别名、加角色、临时停用都算。
4. **别名跟着角色走**：`aliases` 挂在各角色的主唤醒词下——不同角色相互独立，
   凯尔希的「老猫」不会把阿米娅叫醒。
   ★**2026-10-01 起别名不再是必需的**★：唤醒词多了一层**拼音近音**匹配
   （「能天史 / 开尔信 / 阿宁 / 白哲」这类听错不用手攒就能认，见下面「唤醒词为什么不用手攒别名」）。
   别名仍然有效（人工攒的最准，而且近音层关掉也还能用）。
5. **容错**：某个人格文件找不到或写坏了，只跳过她一个（日志会说是哪个文件），
   其余角色照常；索引里一个可用角色都没有时才退回 `[llm] system_prompt` 那段
   （`[persona] enabled = false` 也一样）。没有角色时，唤醒词退回 `data/wakewords.json`。

### 唤醒词为什么不用手攒别名

**原来的问题**：唤醒词靠**字符级**相似度匹配（`SequenceMatcher`，阈值 0.75）。
ASR 把「能天使」听成「能天史」时相似度只有 2/3 = **0.667 < 0.75** → 认不出来，
只能一路手工加别名（能天使的资料卡里攒了 5 条，凯尔希那边攒了 58 条）。

**业界怎么做**（都查过）：

| 路线 | 做法 | 代表 |
| --- | --- | --- |
| 声学侧（真 KWS 模型） | 专用小模型直接**从音频**检关键词，关键词以「声母 + 韵母」的 token 序列给出（`n ǐ h ǎo j ūn g ē @你好军哥`），每个关键词还能单独调 `:boosting` 与 `#threshold` | sherpa-onnx open-vocabulary KWS（模型仅 3~4 MB，chunk-8 延迟 160 ms）、Porcupine、openWakeWord |
| 文本侧（ASR 出字之后） | 把文本归一到**拼音/音素**再比（同音词替换也是同一条路） | sherpa-onnx 的 pinyin homophone replacer；商用助手对自定义唤醒词普遍这么做 |

你说天猫精灵那种「忽略辅音比韵母」是文本侧的**粗糙版** —— 真做法是**音素级**匹配，
不是真丢辅音（丢了会把「开尔文」也认成「凯尔希」）。

**我们走文本侧**（待唤醒时本来就在跑 ASR，零新模型、零新依赖）：

```jsonc
// data/wakewords.json
"phonetic": true     // 拼音近音层（默认开）；关掉就回到「只能靠别名」的老行为
```

规则都是**实测出来的**（数据来自真人耳听的听错 + 日常负样本）：

| 规则 | 为什么 |
| --- | --- |
| 相似度只有三档：全同 1.0 / 同声母 0.7 / 同韵母 0.7 / 否则 0 | 留出可解释的余量，不要一个黑箱分数 |
| 「同韵母」要求韵母 ≥2 个字母 | 单韵母 a/i/u 太常见：不加这条会把「阿米**巴**」认成「阿米**娅**」 |
| 声母/韵母的同类混淆组也算相似（z/zh、c/ch、s/sh、n/l；an/ang、en/eng、in/ing…） | 平翘舌与前后鼻音是中文 ASR 的经典错法（「白泽→白**哲**」就靠它） |
| 允许**恰好一个**音节不同 | 「能天**史**」（同音字）、「开尔**信**」、「阿**宁**」 |
| **只在句首附近**认（允许前面一个语气词「呃，能天史」） | 放开到全句会立刻出事：「海尔洗衣机」里的「海尔洗」与「凯尔希」拼音全同 |
| 拼音撞车的常用词走黑名单 | 同上：「海尔洗」「开尔文」 |
| 单音节唤醒词不做近音 | 一个音节里「恰好一个不同」= 全都不同，太容易误触发 |
| 人名那边更严：只许**第一个**音节不同 | 实测人名听错都错在首字（凯/开/海/太/台/泰） |

实测：**17 条真实听错全中**（一个别名都不给），9 条日常负样本 **0 误触发**（`scripts/tests/test_phonetic.py`）。

> 要不要上真正的声学 KWS 模型？它不是必需的：我们待唤醒时本来就在跑 SenseVoice，
> 拼音层是零成本的一层。真 KWS 的优势是**省电**（3~4 MB 的模型替掉 228 MB 的 ASR）和
> **更低延迟**（160 ms vs 一轮 ASR），但要多一个模型 + 关键词文件要在运行时生成，
> 而且它只对设定过的关键词生效 —— 以后要省电再说。

### 想让角色声音也不一样

Piper 的每个声线是一个 onnx 文件。在角色里写 `"voice": "zh_CN-huayan-medium"` 之外的名字，
把那两个文件（`.onnx` + `.onnx.json`）放进 `models/tts/piper/` 即可；
**没装的时候只警告、继续用全局声线**（不会把嘴弄哑）。用 `python main.py persona --voices` 看缺哪个。

### 只用一条语音就能克隆（不训模型）

素材还没多到能训模型，也想要「换个人声」：控制台（第 4 节）的「**角色**」标签页里，
每个角色卡片下有个「★只用一条语音就能克隆★」：

1. 从下拉里挑一条她的素材（素材目录：人格里写了 `voice_dir` 就用它，否则按约定
   `<人格文件所在目录>/<id>/` —— 没设参考音的角色也照列；逐条标了时长/采样率/行不行/有没有文本），
   **或者**直接传一段你自己的录音；
2. 点「设为克隆参考并试听」—— 它写人格文件（`voice_ref`，可选 `voice_ref_text`；
   `backend` 空着就补成 `zipvoice`），改前留 `.bak`，然后让服务**真重建一次 TTS**
   （第一次十几秒），再念一句**她自己的台词**给你听。

| 事实 | 说明 |
| --- | --- |
| 写哪儿 | ★人格文件★（唯一真相）：只动 `voice_ref` / `voice_ref_text` / `backend`，别的键一个不碰 |
| **参考文本** | 按**文件名**能从素材清单（`<id>.txt`）/ 她的台词里对上就**自动填入**（下拉里标「有文本」）；自己传的文件也按文件名查一遍 |
| 换了音频 | 却又对不上新文本时，会把旧的 `voice_ref_text` **清掉** —— 留着就是「参考音频和文本不匹配」，听着会声不对词（工程日志 §21） |
| 要重启吗 | 不要。只换参考音是**当场生效**的（不重建就不会生效 —— 详细原因见工程日志 §48） |
| 上传的文件 | 存到 `data/personas/<id>/clone-ref.<ext>`（跟人格文件放一起，不跟项目根走） |
| 长度限制 | 0.8 ~ 60 秒（太短学不出音色、太长只是白等），单个文件 ≤ 25 MB |
| 命令行等价 | 手工改人格文件的 `voice_ref` 就行；控制台只是替你改 + 备份 + 写坏当场校验 |
| 试运行 | 接口默认 `dry_run`（只报告会改哪几个键、不落盘），控制台点按钮才真写 |

现成的第二个完整例子是 `data/personas/exusiai.json`（能天使：`worlds: ["明日方舟"]`、
38 条台词）。她的语料是用上一节那套导进来的：

```powershell
python scripts/import_lines.py data/personas/exusiai/exusiai.txt --apply   # 只添加，不替换
```

> 素材音频本仓库不带（`data/personas/<id>/*.wav` 没进 git）：想复现就把自己的音频
> 按那个目录名放好，再照上面两步克隆。

### 在控制台里新建 / 修改角色（按格式填 / 导入文件）

不想手写 JSON 就用控制台「**角色**」标签页**底下那张表单**（上面是角色卡片，底下是新建 ——
建完顺手就能试听，听着不对就点卡片上的「编辑资料卡」回去改，不用换标签页）。
它把「格式」变成了**一份规范**：字段名、说明、类型（文本 / 多行 / 一行一条 / JSON / 数字 / 开关）、
必填项，全部来自后端的一份 `SPEC` —— 前端表单是按它**生成**的，所以规范改了、表单自动跟着改。

| 它会做的事 | 说明 |
| --- | --- |
| 只写两个文件 | `data/personas/<id>.json` + 往 `data/characters.json` **追加一行**（只有索引里有的角色才会被唤醒） |
| 先看后写 | 「预览 / 看 JSON」只报告会写到哪、长什么样；点「创建」/「保存修改」才落盘（接口默认 dry-run） |
| ★改 ≠ 覆盖★ | 在角色列表里**点那张卡片**（或卡上的「编辑资料卡」按钮）就进编辑：表单被填成她现在的样子、id 锁住，**只动表单里那几栏**。`enabled` / `default` / `voice_dir` / `world` 这些「表单不暴露」的键、以及你不认识的字段**一个都不会丢** —— 整份覆盖会把它们静默删掉（「我就改了个称呼，怎么喊不醒了」就是这么来的） |
| 新建不覆盖 | 「创建」碰到已有的 id 会**直接拒绝**并指路去「编辑资料卡」；表单里清空的栏会**真的删掉**（那是你的意思），空着一开始就没填的栏不算改动、也不会白写一遍文件 |
| 不会误伤 | 改前留 `.bak`、写完当场读回校验 JSON；改角色**不动索引**，也不碰别人的人格文件 |
| ★不认识的旧值不会被清掉★ | 下拉框里没有她现在的取值时（典型：**控制台进程比人格文件旧**，那份「表单规范」里还没有新加的选项），浏览器不会报错，只会把下拉退回到第一项 ≈ 空。所以填表时会把没见过的值**原样补一条**并在表单上方黄字说明，存回去也还是原值；服务端那边「客户端压根没送这一栏」也不等于「用户清空了」，一律保留 |
| ★换嗓子会明说★ | 保存时如果清掉了 `backend` / `tts_voice` / `voice` / `voice_ref` / `engine` 这类栏，报告里会红字告警（「这等于换嗓子/换脑子」），不再只是一行「清掉了 backend」 |
| 会提醒「写了但没用」的字段 | 人格解析是**白名单式**的（字段名写错只会被静默忽略），所以不认识的字段会当场报出来 |
| 导入 | ① `.json` —— 一张资料卡（本标签页导出的就是，也认数组/索引，取第一个并说明）；② `.txt` —— 素材清单（名字一行 + 正文一行）→ 只填「示例台词」那一栏。★正在编辑另一个角色时导入别的 id，会自动切回「新建」★（不然会拿着别人的字段去覆盖你正在改的那个） |
| 导出 | 每张角色卡片上的「下载 JSON」→ 下载她的人格文件（可分享 / 可进 git） |

> 新建完不用重启：角色索引与人格文件都被监听着（几秒内热加载）。
> `id` 直接当文件名用，所以只允许小写字母/数字/下划线/短横 —— 这条不是洁癖，是安全边界。

### 量「像不像她」：声纹尺子

以前能量音区、停顿、沙沙声，唯独量不了用户最在意的那件事 —— **「这是不是同一个人在说」**。
换参考、换精度、换步数、重训模型，几次调整之间没有共同刻度，就说不清「这次到底好没好」。
现在有了一把尺子（3D-Speaker CAM++ 中文版，27 MB，纯 CPU，走 sherpa-onnx，零新依赖）：

```powershell
python scripts/download_models.py --only speaker                      # 一次性
python scripts/tools/spk_check.py 合成.wav --ref data/personas/kaltsit      # vs 她的素材（质心）
python scripts/tools/spk_check.py a.wav b.wav c.wav                          # 两两矩阵
python scripts/tools/pick_voice_ref.py --who kaltsit                        # 素材体检（多两列 + 风格建议）
```

**阈值是量出来的，不是拍的**：同人素材两两 **0.635~0.977**、跨人 **0.313~0.535**
→ **0.60** 是个干净的分界（两边各留约 0.1 余量）。

**2026-09-30 的基准**：合出来的凯尔希 vs 她的素材质心 = **0.70~0.77**，
而她素材内部两两中位 = **0.82** —— 也就是「像她，但比她自己的素材之间还差一档」。
这句话以前只能凭感觉，现在有数了；以后每次改参考/改后端，都能看它是涨还是跌。

`pick_voice_ref.py` 现在除干净度还给**语气层次**（音区中位 / 摆幅 / 持续偏离）
并直接建议几条风格参考（附一段能粘进人格文件的 JSON）。

★挑参考时先看两列，它们各自对应一种「听着就不对」★：

| 现象 | 看哪个数 | 门槛 |
| --- | --- | --- |
| **沙、杂音多** | **停顿电平**（帧电平 p10）—— 参考的停顿里还有没有东西在响（伴奏/底噪/音效） | 要 < **−55 dB**；带伴奏的参考会让输出一直沙 |
| 说话前干等、卡 | **时长** | 3.5~8 秒（21 秒的参考实测 RTF 3.67、首段 17.9 秒 ✗；4.7 秒的 RTF 0.40 ✓） |

> 参考侧这些数只是**筛子**，最终判据是**输出**：
> `python scripts/tools/ab_clone_model.py --profile <合成的 wav>`（看 10-12k 那一层和停顿电平）。
> 工程日志 §52 有一张完整的实测对照表（一次真实的「声线崩了」是怎么量出来的）。

★但要看它给的跨度★：凯尔希在 3.5~8 秒区间里音区只差约 **0.6 个半音** —— 她的素材本身
语气就单一，换参考只能给出细微差别；想要明显不同的情绪，得另找带情绪的素材
（或者换支持风格条件的模型）。工具会自己把这句话打出来。

### 让语气跟着情绪走（`voice_refs`）

★前提★：ZipVoice 的接口里**没有「情绪」这个输入维度** —— 风格只能从**参考音频**里来
（实测：参考音自己只差 1.4 倍，到输出被放大 2.5 倍）。所以当前模型下能做的「情绪控制」就是：
**准备几条不同语气的参考，按需要换**。

```jsonc
// data/personas/kaltsit.json
"voice_ref": "data/personas/kaltsit/干员报到.wav",        // 默认那一档（老写法，仍然有效）
"voice_refs": {
  "calm":  "data/personas/kaltsit/完成高难行动.wav",
  "催促":  { "ref": "data/personas/kaltsit/精英化晋升1.wav", "text": "文件在这里，自己看。" }
}
```

三条命令就够用：

```powershell
python scripts/tools/pick_voice_ref.py --who kaltsit   # 挑素材：给音区/摆幅/像不像她 + 直接给 voice_refs 建议
python scripts/tools/spk_check.py 合成.wav --ref data/personas/kaltsit   # 量「换档后像不像她」
```

怎么让它真的切档（**默认关**，因为要靠模型听话）：

```toml
# config.toml
[tts]
style_from_llm = true      # 允许回答开头写 <style=calm>；角色没写 voice_refs 时完全无效
style_default  = ""        # 启动时用哪一档（空 = voice_ref 那一档）
```

- 标签会被**边收边剥**（模型是一个 token 一个 token 吐的，标签会被切开），
  **不会念出来**；没写标签、或写了不存在的档 → 按默认语气说，不会把声线弄坏。
- 代码里也可以直接切：`loop.set_style("催促")`（引擎已加载时**当场生效**，
  因为换参考不需要重载模型）。
- ★门槛先说清楚★：绝大多数素材里「不同语气」的差别是**细微**的（凯尔希 3.5~8 秒区间
  音区只跨 0.6 个半音）。想要明显不同的情绪，得先有带情绪的素材 —— 
  `pick_voice_ref.py` 会把这句话自己打出来。

### 给角色训专属声线（标准化流程，Windows 原生）

素材够的角色（有音频 + 每条音频都有对应文本）可以训一份自己的模型，从此不依赖零样本克隆：

```powershell
python scripts/persona_voice.py --list                      # 谁够条件、谁缺什么
python scripts/persona_voice.py --persona kaltsit           # 一条龙：数据→训练→导出→安装→写 voice_model
python scripts/persona_voice.py --persona kaltsit --verify  # 用助手环境加载新模型试一句
python scripts/persona_voice.py --persona kaltsit --dry-run # 只打印会做什么
```

约定（不满足可以显式覆盖）：

| 位置 | 约定 | 覆盖方式 |
| --- | --- | --- |
| 素材音频 | `data/personas/<id>/*.wav` | 人格文件里的 `voice_dir` |
| 素材文本 | `data/personas/<id>/<id>.txt`（名字一行 + 正文一行，见第 14 节） | 同上 |
| 数据集 | `data/finetune/<id>/`（24kHz + TSV，已 gitignore） | `--data-dir` |
| 训好的模型 | `models/tts/zipvoice/personas/<id>/` | `--install-dir` |

**跑完会自动把 `"voice_model"` 写进人格文件**（改前存 `.bak`，只动那一行、改完校验 JSON）：

```json
"voice_model": "models/tts/zipvoice/personas/kaltsit"
```

> ⚠️ **单说话人微调会把音色收敛到一个人**：这份模型只适合该角色，别的角色别配它，
> 否则也会变成她的声音（没配的角色走 `[tts] clone_dir` 的默认模型，不受影响）。
> 这也正是「按角色配模型」的意义——每个角色各用各的 `voice_model`。

> **另一条路（Piper 专属声线）实测结论：5 分钟素材训不出来，别走**（2026-09-26）。
> 60 epoch / lr 1e-4 训完之后，音区确实更接近参考音了（187 Hz vs 出厂 209 Hz，参考 180 Hz），
> **但话说不清了**——两个独立 ASR（SenseVoice / Whisper）回听同一句，相似度只有 0.54~0.94
> （出厂 Piper 是 0.958~1.000，ZipVoice 是 1.000）。数据集格式、底模权重搬运、素材量
> 都已逐项排查过：**不是代码错，是「5 分钟素材」这个量级下 VITS 会崩**。
> 想要专属音色，请用上面那条 ZipVoice 的流程，或者再攒 10~20 分钟素材。
> 完整数据在 [`docs/ENGINEERING_LOG.md`](../ENGINEERING_LOG.md) 第 26 节。
>
> **2026-09-27 补测（第 44 节）：问题不在「训多久」，在「数据配比」**——
> 判据是「**梅尔帧 / 音素 id**」比值（健康区间 ~1.5~2.0，`train_piper.py` 现在会直接打印并警告）：
> A 组长片段（24 条，中位 **1.90**）→ 29 epoch 就能用（ASR 回听 0.79/1.00/1.00）；
> B 组自动拆段（69 条，中位 **1.02**）→ 29 与 49 epoch **都是胡话**（0.04~0.38）。
> 所以两条可执行的规矩：**① 新数据先量比值，中位 <1.5 别训**；
> **② 训完一定要跑 `python scripts/tools/piper_ab.py`，并且看 ASR 回听实际听到的字**
> （只看相似度数字会漏掉「音色对但说的是胡话」）。
>
> 顺带一个**值得知道的选项**：**出厂 Piper 本身没有沙沙声、而且快 15~20 倍**
> （安静帧高频占比 0.1% vs ZipVoice 0.8%；RTF 0.07 vs 1.18；首声 0.18 秒 vs 3.6 秒），
> 代价是音色换成 huayan 的女声。要试就把 `[tts] backend` 改回 `piper`。


训练规模按「目标 epoch 数」自动换算（`--epochs`，默认 20）：实测 **283 秒素材 ÷ 每批最多 60 秒
≈ 4.7 批/epoch**，所以 100 iter ≈ 21 epoch。实测速度见下表。

#### 换精度：int8 / fp32（**待办：需要你听一遍**）

导出那一步（第 7 步）**每次都会同时产出 int8 和 fp32 两份**，装哪份由 `--precision` 决定；
角色目录里两份可以共存，运行时按 `config.toml` 的 `[tts] clone_precision` 选：

```powershell
# 两份都装进角色目录（不会删掉已有的那份）
python scripts/persona_voice.py --persona kaltsit --stage 8 --stop-stage 8 --precision both

# 生成「精度 × 步数」的对比 wav + 客观指标（不需要扬声器，文件摆在 sessions/ab_clone/）
python scripts/tools/ab_clone_model.py
```

| 精度 | 体积 | 速度（同一句话，4 步） | 什么时候选它 |
| --- | --- | --- | --- |
| `int8`（默认） | 125 MB | 4.4 s | 当前在用、实测过的那一套 |
| `fp32` | 600 MB | 8.3 s（慢一倍） | 想排除「动态量化带来的沙沙声」时 |

> ⚠️ **状态：还没被你耳朵确认，而且它的优先级已经降了。** 2026-09-24 下午把整张 A/B
> 网格（40 个 wav）当数据算了一遍：配对对比下 fp32 **确实**更低（凯尔希长句 4/4 档、
> 平均 −2.6 个百分点），但**沙沙声的主要来源是素材与参考音频**，不是精度：
>
> | 手段 | 效果 | 代价 |
> | --- | --- | --- |
> | 换/处理参考音频 | 输出高频 ×1.8~2.5 | 零（改一行配置） |
> | 换素材重训 | 能压低基线 | 几十分钟训练 |
> | 换精度 int8 → fp32 | −2~3 个百分点 | 合成时间 ×2 |
> | 加步数 | **没有可测效果** | ×4~×30 |
>
> 根因证据：阿米娅 38 条素材的**安静帧**高频占比（只算停顿/气口，与句子无关）
> 中位 **34.4%**，凯尔希只有 **7.5%**（4.5 倍）；两个角色的文件格式完全一样。
> 所以 `clone_precision` 默认仍是 `int8`，**先挑干净的参考**（下表），精度排最后。
> 完整推导见 [`docs/ENGINEERING_LOG.md`](../ENGINEERING_LOG.md) 第 16.7 节；
> 体检与选参考：`python scripts/tools/pick_voice_ref.py`。
>
> **⚠️ 2026-09-25 更正**：16.7 那句「沙沙声来自素材底噪/参考音频」是**指标假象**——
> 它用的「安静帧高频」量的是 −78 dBFS 的数字静音（耳朵听不到）。换成相对语音电平的口径后，
> 两条参考的安静帧高频是 0.37% / 1.06%，**都干净**；而把参考真去净化（谱门/高架）
> 在 5 遍配对里反而让输出更亮。详见工程日志第 18 节。
>
> **⚠️ 2026-09-25 更正（去沙沙声：指纹在 10~12 kHz）**：第三轮量出沙沙声长什么样——
> 自然语音的高频**一路滚降**（真人参考：4-6k −10.7 → 6-8k −11.3 → 8-10k −13.6 → 10-12k **−16.0** dB），
> 而微调模型的 **10-12k 是 −8.0 dB**（比自己的 8-10k 还高 4.3 dB）——就是耳朵听到的沙。
> 它**不在停顿里**（ZipVoice 的停顿是精确数字零），所以参考净化/噪声门都没用（那就是 18.2 无效的原因）；
> 旧的 `out_tilt 7kHz/−3` **打偏了**：把 8-10k 砍了 3 dB（齿音变闷）而 10-12k 只降 3 dB（依旧翘着）。
> 现在默认 **`out_tilt_hz = 10000` / `out_tilt_db = -9`**：4-8k 一点不动、10-12k 落到 −22 dB。
> ★这个值跟模型绑定★（微调 −9 / 出厂 −4，出厂模型本来就不沙）；换模型/换参考后先量：
> `python scripts/tools/ab_clone_model.py --profile <wav...>`。试听 `sessions/hiss_last/`。详见工程日志第 20 节。
>
> **⚠️ 2026-09-25 更正（音高）**：「语气连贯」的另一半是**音区**，不是停顿。量音高必须先验尺子：
> 第一版跟踪器（归一化自相关）连**真人参考音**都能量出 150/280 Hz 来回翻的假象——
> 换成 YIN 之后才看出真相：句内摆幅与真人参考相当（合成 4.4 / 3.3 半音 vs 参考 4.3 / 2.5），
> 但**每遍的整体音区**会飘 1.7~2.4 半音。所以现在默认开音区守卫（重采，不做变调）。
> 详见工程日志第 19 节。
>
> **要不要加数据重训？实测：不用**（日志 19.6）。出厂模型（Emilia 大数据）与 38 条/4.8 分钟的
> 微调版，抖动是同一量级（0.89 vs 1.30 半音，差别在噪声里）——因为抖动来自「每次 generate 都
> 重新采样」，不是数据少。**能撬动它的是参考音频（同一模型换参考能差近 4 倍：0.90~3.41 半音），
> 但关键不是把参考加长**（16 s 的反而 3.09）。顺带发现 `精英化晋升2.wav` 这类情绪化参考会让
> 输出几乎没有浊音段（5 遍里 4 遍量不出音高），要避开。真要继续压，就去挑参考（免费），别去训。

> 手工调参/排障时可以单步跑：`scripts/tools/finetune_zipvoice.py --stage 1..8`（分步版）、
> `scripts/tools/prepare_tts_dataset.py --dir data/personas/<id> --out data/finetune/<id> --apply`
> （只产数据集，`python scripts/tests/test_prepare_dataset.py` 有 24 条测试）、
> `scripts/persona_voice.py --no-wire` 则跑完不自动写 `voice_model`（只训练，不动人格文件）。
>
> **2026-09-25：另起一条路 —— 训 Piper/VITS 专属声线（路线 C）**。理由：ZipVoice 的沙沙声
> 住在 10~12 kHz（日志第 20 节），而 Piper 的 8 kHz 以上几乎为零（带限）+ RTF 0.05、开口 0.13 s。
> 环境链已打通（底模在 `datasets/rhasspy/piper-checkpoints`、`--language cmn`、
> 要编 `monotonic_align`、`pytorch-lightning==1.9.5`、ckpt 里带 PosixPath …）：
> 踩坑与命令全部记在工程日志第 23 节，启动器 `scripts/tools/train_piper.py`，
> 数据准备 `scripts/tools/prepare_piper_dataset.py`。
>
> **训练跑起来之后怎么看进度**：`python scripts/tools/train_piper_watch.py` —— 终端看板，实时刷新，
> 一眼看到「步数 / loss / 速率 / 预计完成时间 + **可用内存**」。为什么必须单独做看板：
> `train_piper.py` 关掉了 Lightning 的进度条（`enable_progress_bar=False`），关掉之后
> **日志里就没有逐步 loss 了**（`sessions/train_*_watchdog.log` 只有启动那几行）——
> 进度只存在于 tfevents 里。而且本机训练**唯一**的崩因是内存不足（写 checkpoint 时
> `0xC0000005`，没有 traceback），所以看板把可用内存摆在最显眼处，**低于 3 GB 就报警**。
> 可选参数：`--once` 只看一眼、`-i 30` 改刷新间隔、`--arms A,B` 只看某几支、`--all` 连不活跃的实验目录一起看。
> 想要曲线图：`.venv-piper\Scripts\tensorboard.exe --logdir data\piper`，再开 http://localhost:6006 。
> 读 tfevents 要 tensorboard（**只装在 `.venv-piper` 里**）→ 用系统 python 启动时脚本会**自动换成那个解释器**。

### 从清单文件导入台词（只添加，不替换）

上面那些台词素材是「文件名 + 文本」的清单（`data/personas/kaltsit/kaltsit.txt`，38 条）。
`scripts/import_lines.py` 可以把任意这样的清单导进任意/对应 json 的数组字段：

```powershell
# 只看会发生什么（默认试运行，不写文件）
python scripts/import_lines.py data/personas/kaltsit/kaltsit.txt

# 真写进去：目标 json 自动找（同目录 <名字>.json → 上一级 <名字>.json → 索引里对应的 file）
python scripts/import_lines.py data/personas/kaltsit/kaltsit.txt --apply

# 指定目标 / 场景名 / 过滤
python scripts/import_lines.py 台词.txt --json data/personas/amiya.json --apply \
    --scene 随意对话 --skip 作战 --max-chars 40
```

- **只添加，不替换**：原有内容一个字不动，同文本的条目自动跳过（实测：38 条里
  已有 6 条被手工搬过 → 只新增 32 条，再跑一次 0 新增）。
- 写入前自动备份成 `<名字>.json.bak`（`.bak` 已进 `.gitignore`），回滚就是拷回来。
- 清单两种排版都认：**名字紧接正文**（实测素材）和 *名字 / 空行 / 正文*；
  也认 `名字<TAB>正文`。整份都是段落的文本用 `--plain`。
- `--key` / `--as text` 可以导到别的字段（例如 `style`，写纯字符串而不是 `{scene,text}`）。
- 写之前会量一遍角色提示词长度，给你一个「+多少字」的数字。

**「台词多了会不会变成长篇大论/复读」——实测：不会，反而更短。**

只把 system prompt 换掉（其余完全一样），每题问 2 遍，数汉字：

| 配置 | 提示词 | 回答长度（6 次） | 平均 |
| --- | --- | --- | --- |
| 39 条台词（导入后） | 2547 字 | 31 / 31 / 64 / 65 / 67 / 71 | **54.8 字** |
| 只有最初 7 条 | 1326 字 | 48 / 65 / 85 / 89 / 91 / 94 | **78.7 字** |

原因大概是这些台词本身就是「短、克制、不说教」的口吻，提示词里也明确写了
「只看语气，不要原样重复」，所以它是往**更像凯尔希**的方向收敛。
（这条推翻了我最初在工具里写的担心，已经把提示改成实测结论。）
运行 `scripts/tests/test_import_lines.py` 可以复验「只添加」的 36 条断言。

---
