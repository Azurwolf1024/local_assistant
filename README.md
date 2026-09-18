# 本地语音到语音助手（全离线）

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**SenseVoiceSmall / Whisper-large-v3-turbo → Ollama qwen2.5:7b → Piper 中文女声**

完全跑在本机的「说话 → 识别 → 思考 → 说话」闭环，不联网、不上传任何数据。
支持唤醒词常驻待命、闹钟与会议提醒、备忘、日程查询，并且边说边播（首音 1~2 秒）。

---

## 1. 架构

```mermaid
flowchart LR
    A["🎤 麦克风<br/>16 kHz"] --> B["VAD 断句<br/>Silero"]
    B --> C{"唤醒词?"}
    C -->|未唤醒| B
    C -->|已唤醒 / 连续对话中| D["ASR 路由"]
    D -->|快| E["SenseVoiceSmall<br/>ONNX int8"]
    D -->|准| F["Whisper turbo<br/>OpenVINO int8"]
    E --> G{"生活技能命中?"}
    F --> G
    G -->|是| H["闹钟 / 备忘 / 日程 / 时间<br/>本地 JSON"]
    G -->|否| I["Ollama qwen2.5:7b<br/>流式"]
    H --> J["整句分块"]
    I --> J
    J --> K["Piper<br/>zh_CN-huayan-medium"]
    K --> L["🔊 边说边播"]

    M["调度器<br/>每 5s 轮询"] -. 到点播报 .-> J
    M -. 右下角弹窗 .-> N["可视提醒<br/>置顶小窗，点一下即关"]
```

### 1.1 两级加载（`listen` 常驻服务）

| 状态 | 加载了什么 | 内存 |
| --- | --- | --- |
| **待唤醒**（平时） | Silero VAD + SenseVoiceSmall | **约 0.9 GB** |
| **已唤醒**（加载） | 前台加载 TTS，后台预热 Whisper 与 qwen2.5:7b | 约 2.5 GB + Ollama |
| **空闲超时**（回收） | 卸载 Whisper / TTS，并让 Ollama 释放模型 | 回到 0.9 GB |

实测日志：

```
[待唤醒] 请说「凯尔希」…
[已唤醒] 凯尔希。                          ← 1.6s 加载 TTS
你说：用一句话介绍杭州。
助手：杭州是个美丽的地方。  [首字 0.35s / 首音 1.25s]   ← 秒回
  Whisper 已就绪（后台加载 17.8s）           ← 后台加载，不阻塞对话
  LLM 已就绪（预热 0.1s）
  Whisper 已卸载，内存已释放                 ← 3 分钟无指令
  TTS 已卸载
[待唤醒] 已释放：Whisper、TTS、Ollama/qwen2.5:7b
```

关键设计：

| 位置 | 做法 | 原因 |
| --- | --- | --- |
| ASR | SenseVoice 快路径 + Whisper 按需复核 + 标点移植 | 中文短句 0.1s 出结果，长句/可疑结果才花 3s 让 Whisper 复核 |
| 意图 | **先本地技能，后大模型** | 「几点了」「十分钟后提醒我」这类指令用正则 0ms 命中，零幻觉 |
| TTS 分块 | **只在句末切，短句合并** | Piper 中文音色看不到标点，切太碎会又平又顿，见第 6 节 |
| 播放 | 单独线程 + 队列，随时可打断 | 边生成边播，说错了按回车立刻停 |

---

## 2. 目录结构

```
<项目目录>\
├─ main.py                     # 入口：listen / chat / text / ask / skills / asr / tts / selftest
├─ config.toml                 # 全部可调参数
├─ data/                       # ← 可以直接用编辑器改
│  ├─ wakewords.json           #   唤醒词
│  ├─ alarms.json              #   闹钟 / 定时提醒
│  ├─ memos.json               #   备忘
│  └─ schedule.json            #   课程表 / 会议
├─ voice_loop/
│  ├─ pipeline.py              # 会话编排（唤醒服务、连续对话、打断、提醒播报）
│  ├─ wake.py                  # 唤醒词匹配（精确 + 别名 + 模糊）
│  ├─ skills.py                # 生活技能：时间 / 闹钟 / 备忘 / 日程
│  ├─ scheduler.py             # 后台提醒调度器
│  ├─ system_ops.py            # 系统操作：关/开显示器（只关屏，不休眠）
│  ├─ toast.py                 # 右下角可视提醒弹窗
│  ├─ nlp_time.py              # 中文时间解析（明天早上七点 / 十分钟后 / 下周三）
│  ├─ store.py                 # JSON 存储（保留注释、外部改动自动重载）
│  ├─ text.py                  # LLM 输出清洗 + 流式分块 + 标点移植
│  ├─ audio.py                 # 麦克风、VAD、播放器
│  ├─ llm.py                   # Ollama 客户端
│  ├─ asr/                     # sensevoice / whisper_ov / router
│  └─ tts/piper_tts.py
├─ scripts/
│  ├─ download_models.py       # 一键下载模型
│  ├─ test_offline.py          # 离线自测（分块/时间/技能/唤醒/生命周期）
│  ├─ test_skills_route.py     # 技能路由 + 关屏 + 课表 + 提醒文案 + 重启不丢数据
│  ├─ test_toast.py            # 看一眼右下角可视提醒长什么样
│  ├─ test_dialog.py           # 对话链路自测（不用麦克风）
│  ├─ test_wake.py             # ★ 唤醒词实测与调优（打印听到的内容 / 自动写 aliases）
│  ├─ test_mic_loopback.py     # 麦克风回环诊断（放一段语音，看能不能听到 + 识别）
│  ├─ clean_junk_data.py       # 清理早期版本写坏的备忘/闹钟
│  ├─ say.py                   # 用扬声器念一句话（不想开口时测唤醒词用）
│  └─ tts_probe.py             # TTS 调音工具（含语调对比）
└─ models/                     # 模型权重（不进 git，见 models/README.md）
```

---

## 3. 安装

```powershell
cd <项目目录>
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python scripts/download_models.py     # 补上 SenseVoice / Silero VAD / Piper 中文女声（约 300 MB）
python main.py selftest               # 7 项检查，全过就能用了
```

---

## 4. 使用

```powershell
python main.py listen        # ★ 唤醒词服务（前台，推荐先这么跑，看得见日志）
python main.py listen -B     # 唤醒词服务（后台，无窗口，日志写 sessions/listen.log）
python main.py stop          # 停止后台服务（先发信号优雅退出，超时才强杀）
python main.py chat          # 普通对话，听到说话就回答（不认唤醒词）
python main.py text          # 打字调试：完整技能 + LLM + 语音播报，不用麦克风
python main.py skills        # 查看闹钟 / 备忘 / 日程
python main.py skills "十分钟后提醒我喝水"   # 测试某句话会命中哪个技能
python main.py ask "介绍一下杭州"            # 单次提问 + 播报
python main.py asr test.wav                  # 音频转文字
python main.py tts "你好呀" -o a.wav          # 文本转语音
python main.py devices                       # 查看音频设备
```

`listen` 模式下：

- 平时只等唤醒词（默认「凯尔希」），内存只占约 0.9 GB
- 被唤醒才加载 TTS / Whisper / LLM，**3 分钟没指令自动释放回到待唤醒**
- 唤醒后连续对话不用反复喊（每次说话都会续上 3 分钟）
- 唤醒词后面带停顿也认：「凯尔希……现在几点了」会先收下半句再回答，不会只说「在的」
- 后台运行时改 `data/wakewords.json` 也能生效（几秒内热加载）
- 后台同时跑闹钟和会议提醒，到点会自己开口播报，**并在屏幕右下角弹一个可随时关掉的小窗**
  （扬声器没开、戴耳机走开了也不会错过；点「知道了」或按 Esc 立刻关）
- 回答过程中**按回车**立刻打断（后台无终端时自动跳过）

### 关闭服务

| 启动方式 | 怎么关 |
| --- | --- |
| `python main.py listen`（前台） | 在那个窗口按 **Ctrl+C**，或另开窗口跑 `python main.py stop` |
| `python main.py listen -B`（后台） | **只能** `python main.py stop`（窗口是隐藏的，Ctrl+C 用不上） |

`stop` 的工作方式：读 `sessions/listen.pid` → 写一个停止信号文件 →
服务在 5 秒内看到并优雅退出（释放模型、删掉 pid 文件）；
如果 15 秒内没反应，才用 `taskkill /F` 强杀。

```powershell
python main.py stop
# 已发送停止信号（PID 39592），等待优雅退出…
# ✓ 唤醒服务已停止
```

如果 pid 文件被别人删了、或者窗口被强制关掉留下了孤儿进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Select ProcessId, CommandLine
Stop-Process -Id <PID> -Force
```

> 注意：在 VS Code 里直接关掉终端面板**不保证**会把子进程一起杀掉，
> 残留的进程会一直占着麦克风。用 `main.py stop` 最稳。

### 能用语音做的事

| 想说的事 | 例子 |
| --- | --- |
| 问时间 | 现在几点 / 今天星期几 / 今天几号 |
| 定时提醒 | 十分钟后提醒我喝水 / 明天早上七点叫我起床 / 今晚九点提醒我吃药 |
| 管理提醒 | 我的提醒有哪些 / 取消第2个提醒 / 清空所有提醒 |
| 备忘 | 记一下买牛奶 / 我的备忘有哪些 / 删除第1条备忘 |
| 课程与会议 | 今天有什么课 / 这周有什么安排 / 下一个会议是什么 |
| 录课表 | 每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302 |
| 一次性的日程 | 明天下午三点安排组会 |
| 关/开显示器 | 关屏幕 / 黑屏 / 息屏（只是关屏，不是休眠；服务照常跑） |

技能命中就本地直接回答，零延迟也不会胡说；没命中才交给 Ollama。
所有技能返回 `None` 时不会抢话，所以闲聊不受影响。

### 人格设定（明日方舟 · 凯尔希）

`config.toml` 的 `[llm] system_prompt` 已经写成凯尔希：

```
你是《明日方舟》里的凯尔希（Kal'tsit），罗德岛的医疗主管。
现在正通过语音和博士对话——你就是博士身边的那个凯尔希，不是"扮演助手的 AI"。
【身份与称呼】称对方为「博士」…
【说话风格】冷静、克制、用词精准…一到两句就说完…可以流露一点克制的关心…
【硬性要求】不要 Markdown / 表情 / 动作神态描写…
```

配套细节：

| 位置 | 内容 |
| --- | --- |
| 唤醒应答 `[wake] ack` | `我在，博士。` |
| 事务类应答（代码内置） | `已记录。明天早上七点，也就是十小时后，我会提醒你喝水。` |
| 备忘 | `已归档：买牛奶。` |
| 日程 | `已排入日程：明天上午九点，组会。` |
| 帮助 | `我在。我能做这些：「现在几点」报时间…其余的，直接问我就好。` |

想换成别的人设（你自己的名字、或者别的角色），只改 `system_prompt` 即可，
事务类应答的措辞在 `voice_loop/skills.py` 里，搜 `已记录`、`已归档` 就能找到。

> 想「3 分钟没指令就彻底退出进程」而不是回到待唤醒，把 `config.toml` 里的
> `[wake] idle_action` 改成 `"exit"` 即可。

调试命令：

```powershell
python scripts/test_offline.py         # 几秒钟，不加载模型，测分块/时间/技能/唤醒
python scripts/test_skills_route.py    # 技能路由 + 关屏幕 + 课表 + 提醒文案 + 重启不丢数据
python scripts/test_toast.py           # 看一眼右下角可视提醒长什么样
python scripts/test_dialog.py --no-tts # 13 轮对话，只看文本与耗时
python scripts/test_wake.py --rounds 5 # ★ 拿真实嗓音试唤醒词，看被听成什么
python scripts/tts_probe.py --compare  # 生成语调对比音频
python scripts/test_mic_loopback.py    # 扬声器放一句、麦克风收，诊断麦克风
python scripts/say.py "凯尔希，现在几点了"   # 不想开口时，让电脑替你喊唤醒词
python scripts/clean_junk_data.py --apply    # 清理早期版本写坏的备忘/闹钟（先备份）
```

---

## 5. 三个可配置的 json

### 5.1 唤醒词 `data/wakewords.json`

```json
{
  "enabled": true,
  "words": ["凯尔希"],
  "aliases": {
    "凯尔希": ["凯尔西", "凯尔惜", "开尔希", "卡尔希", "太尔西", "凯尔信", "凯尔希医生"]
  },
  "ack": "在的",
  "idle_timeout": 180,
  "min_silence": 0.3,
  "fuzzy_ratio": 0.75
}
```

- **保存即生效**，前台/后台都会在几秒内自动重新加载，不用重启。
- `aliases` 是最关键的一栏。语音识别经常听错，尤其是三字人名：
  实测单独说「凯尔希」时，两套 ASR 分别听成 **「开尔信」** 和 **「太尔西」**。
  把听错的说法填进 `aliases` 就能命中。
- **不知道会被听成什么？** 跑专用的实测工具：
  ```powershell
  python main.py stop                      # 先停掉常驻服务，两边别抢麦克风
  python scripts/test_wake.py --rounds 5   # 说 5 次，看每次被听成什么
  python scripts/test_wake.py --apply      # 未命中时直接写进 aliases
  ```
  输出会告诉你：SenseVoice 听到的是什么（**只有它算数**，因为待唤醒时只加载了它）、
  命中的是精确/别名还是模糊匹配、与唤醒词的相似度、以及该不该调 `fuzzy_ratio`：
  ```
  SenseVoice 听到：'可尔西'
  × 未命中。与「凯尔希」相似度 0.67（需要 ≥ 0.75 才算模糊命中）
    → 建议把 '可尔西' 加进 aliases
    → 已写入
  ```
  后台上跑着服务时，它同样会在日志里直接告诉你听到了什么：
  ```
  [未唤醒] 听到：胎儿戏。    若是唤醒词，请把它加进 wakewords.json 的 aliases
  ```

  **调优思路**：
  - 相似度 ≥ 0.75 却没命中 → 说明就差一点点，把 `fuzzy_ratio` 降到 0.7 试试
  - 相似度很低（像「胎儿戏」）→ 降阈值没用，必须加进 `aliases`
  - 同一种听错反复出现 → 加进 `aliases`，几秒内自动生效，不用重启

> 小提示：三字人名（尤其末字韵母短）很容易被听错。
> 如果调 aliases 后仍然不稳，可换成音节更长的说法（例：`凯尔希医生`、`嘿凯尔希`）。

### 5.2 闹钟 / 提醒 `data/alarms.json`

平时不用手改，语音说就行，程序会写进去：

```json
{
  "items": [
    { "when": "2026-09-18 07:00", "what": "起床", "fired": false, "kind": "alarm" }
  ]
}
```

支持的说法（都实测通过）：

| 说法 | 结果 |
| --- | --- |
| 十分钟后提醒我喝水 | 10 分钟后 |
| 半小时后提醒我 | 30 分钟后 |
| 一个半小时后叫我 | 90 分钟后 |
| 明天早上七点叫我起床 | 次日 07:00 |
| 今晚九点提醒我吃药 | 当天 21:00 |
| 后天中午十二点半提醒我 | 后天 12:30 |
| 19:30 提醒我 | 次日 19:30 |
| 我的提醒有哪些 / 取消第2个提醒 / 清空所有提醒 | 查询与删除 |

到点时的播报会带上「提醒你做什么」，例如：

```
【提醒】时间到了，喝水。
```

### 5.3 课程表 / 会议 `data/schedule.json`

```json
{
  "items": [
    { "title": "AIAA3102 机器学习", "kind": "course", "repeat": "weekly",
      "weekday": 3, "time": "09:00", "duration_minutes": 90,
      "location": "教学楼 A302", "remind_before": 15 },

    { "title": "项目评审", "kind": "meeting", "repeat": "once",
      "start": "2026-09-20 14:00", "location": "线上", "remind_before": 30 }
  ]
}
```

- `weekday`：`0=周一 … 6=周日`（写错了就会在那天不提醒，改完存盘即生效）
- `remind_before`：提前几分钟语音提醒（到点会主动开口）
- 查询说法：「今天有什么课」「明天有什么安排」「这周有什么安排」「下一个会议是什么」
- 新增一次性的：「明天下午三点安排组会」「帮我记录晚上七点半有跆拳道课」
- 新增每周重复的：「每周三上午九点有 AIAA3102 机器学习，地点教学楼 A302」
  （「每周X」会被识别成 `repeat=weekly`，地点也能从「地点/教室」后面拆出来）

到点前会这样播报（语音 + 右下角弹窗同时出现）：

```
提醒你：十四分钟后，也就是 09:00，有 AIAA3102 机器学习，地点教学楼 A302。
```

> 文件里的 `_说明` / `_格式` 字段是给你看的注释，程序写回数据时会**保留**它们。

---

## 6. 语调和断句（重点）

之前听起来「又平又顿」，根因有两个：

1. **Piper 的中文音色看不到标点。** 用 espeak-ng 的 `cmn` 声线时，
   `，。？！` 全部不会变成音素——把 `我们下午一起去西湖边散步，顺便买奶茶。`
   和去掉标点的版本分别音素化，结果**完全一样**。所以模型只能靠自己学到的韵律断句。
2. **原来的做法每个逗号就切一块**，每块单独合成、各自收尾，再在中间塞 150ms 静音。
   一句话被切成五六个碎片，每片都以「收尾降调」结束，听起来自然又平又碎。

现在的做法：

| 改进 | 效果 |
| --- | --- |
| 只在**句末**标点切；整句超过 60 字才退到逗号切 | 每块都是完整句子，模型能规划整句韵律 |
| 短于 14 字的句子与下一句**合并** | 不再出现「好的。」这种一秒钟的碎片 |
| 块间补白从 150ms 降到 80ms | 不再出现「说完一句停半天」 |
| `length_scale` 1.0、`noise_w_scale` 0.85 | 语速与起伏回到模型训练时的分布 |
| 不删句首的「好的，…」 | 开头不再突兀 |

同一句话的实测：**11.22s → 9.65s**，中间不再有 6 次做作的停顿。

想亲耳对比：

```powershell
python scripts/tts_probe.py --compare
# 生成 sessions\prosody_old_按逗号切块.wav 与 sessions\prosody_new_整句合成.wav
```

继续微调（`config.toml` 的 `[tts]`）：

| 参数 | 作用 |
| --- | --- |
| `length_scale` | 语速，1.0 最自然，1.05 更从容，0.95 更快 |
| `noise_w_scale` | 音长起伏，0.9~1.0 更有感情，0.7 更平稳 |
| `min_chunk_chars` | 调大 → 更连贯但更晚出声；调小 → 更早出声但可能碎 |
| `max_hold_seconds` | 攒句等待上限，保证短回答也能及时出声 |
| `inject_pauses` | `true` 时用 `[[,]]` 强行注入停顿音素，停顿更清晰但语气偏平 |
| `sentence_silence` | 块间补白，0 也可以 |

还可以直接看音素与停顿分布：

```powershell
python scripts/tts_probe.py --sweep          # 扫参数看语速
python scripts/tts_probe.py "你好[[,]]世界"   # 看注入停顿的效果
```

---

## 7. 实测性能（i7 / 纯 CPU）

| 环节 | 实测 |
| --- | --- |
| 本地技能（几点了 / 定闹钟 / 查课表） | **0 s**（正则直接命中，不调模型） |
| SenseVoiceSmall（5.6s 音频） | 0.08 s（RTF 0.014） |
| Whisper turbo int8（5.6s 音频） | 2.8 ~ 3.4 s（RTF ≈ 0.5） |
| Piper 合成 | RTF 0.04~0.07，首块 0.12 s |
| qwen2.5:7b 首字 | 0.1 ~ 0.5 s（预热后） |
| **端到端首音** | **0.7 ~ 2.1 s** |
| 首次加载（编译 OpenVINO 图） | 约 40 s，之后有缓存会快很多 |

优化建议，按收益排序：

1. `keep_alive = "30m"` + 先跑一次 `selftest`，避免模型反复加载。
2. `strategy = "sensevoice"`：中文短句场景直接砍掉 Whisper 的 1~3 秒。
3. 想更快可以让 Whisper 走 openvino-genai（需要 stateful 导出）：
   ```powershell
   optimum-cli export openvino --trust-remote-code --model openai/whisper-large-v3-turbo `
       --weight-format int8 whisper-large-v3-turbo-int8-ov-stateful
   ```
   当前模型是 `--disable-stateful` 导出的，GenAI 的 `WhisperPipeline` 会报
   `beam_idx not found`，所以代码走的是 Optimum 路径（会自动识别并提示）。
4. `ollama pull qwen2.5:7b-instruct-q4_K_M` 更快更省内存。
5. `whisper_device = "GPU"`（Intel 核显）或给 Ollama 换上独显。

---

## 8. 常见问题

**Q：识别不到我说话 / 一直不发送**
先 `python main.py devices` 确认 `[audio] input_device`；`selftest` 里会有麦克风电平提示，
峰值低于 0.01 说明电平太低（正常人说话在 0.05 以上）。可以：
① 检查 Windows 隐私设置里的麦克风权限；② 试试 `input_device = 12`（原生 16 kHz 的阵列）；
③ 把 `mic_gain` 调到 2.0。若 `selftest` 显示 VAD 退化为能量 VAD 也能用。

**Q：喊了唤醒词没反应**
先看终端/日志里有没有 `[未唤醒] 听到：xxx`，把那个 xxx 照抄进 `wakewords.json` 的
`aliases` 里，几秒后自动生效——这是最快的调法。还可以：
① 把 `fuzzy_ratio` 从 0.75 降到 0.65；② 待唤醒用的 `min_silence` 从 0.3 调到 0.45；
③ 跑 `python main.py selftest` 看第 7 项是否命中。

**Q：为什么唤醒后第一句回答会慢一点**
唤醒时会加载 TTS（约 1.6 秒），并在后台预热 Whisper / LLM。
加载 TTS 之前会先把你的下半句收进来，所以不会丢指令；
Whisper 是后台加载（首次编译图约 18~40 秒，之后有缓存），期间用 SenseVoice 回答，
完全不影响对话。

**Q：常驻服务占多少内存**
待唤醒约 **0.9 GB**（只跑 VAD + SenseVoice）；被唤醒后峰值约 2.5 GB，再加 Ollama 的
4~5 GB；3 分钟没指令会自动释放回 0.9 GB。不想等就调小 `idle_timeout`。

**Q：怎么开机自启**
先 `python main.py listen -B` 确认能跑，再把这条命令做成快捷方式放进
`shell:startup`（`Win+R` 输入即可打开启动文件夹）。

**Q：老是误唤醒**
调高 `fuzzy_ratio`（0.8 以上），删掉过于宽泛的别名，或把 `min_silence` 调大。

**Q：助手自己唤醒自己**
播放期间程序会丢弃麦克风输入并清空缓冲区（`_muted` + `mic.flush()`），正常不会发生。
如果仍然出现，用耳机或把 `[audio] playback_volume` 调低。

**Q：我说到一半它不搭理我**
看终端有没有出现「我刚在说话，这段语音被忽略了」。
播放回答期间麦克风是被丢弃的（否则会自己唤醒自己），所以请等它说完，
或者按回车先打断它再说话。

**Q：提醒没有声音**
提醒只在 `listen` / `chat` / `text` 这几种常驻模式下播报，`ask` 是单次问答不会后台提醒。
另外提醒会等当前回答播完再说话（避免打断）。
没开扬声器也没关系：到点同时会在屏幕右下角弹一个窗口，点「知道了」或按 Esc 就能关掉，
`[skills] visual_timeout` 秒后也会自己消失（默认 25 秒，置顶但不抢焦点）。

**Q：「关屏幕」只是关显示器吗？会不会把服务也停掉**
只是关显示器背光（`WM_SYSCOMMAND` + `SC_MONITORPOWER`），**不是休眠、不是睡眠**，
CPU、服务、提醒调度器都照常跑，闹钟到点仍然会响。敲一下键或动一下鼠标就能亮回来，
也可以说「开屏幕」。不想让语音控制这个，把 `[skills] allow_system_commands` 改成 `false`。

**Q：重启服务会不会丢日程和闹钟**
不会。闹钟 / 备忘 / 日程都在 `data/*.json` 里，重启只是重新读一遍文件：
已响过的闹钟（`fired: true`）不会重复响，已经提醒过的日程当天也不会重复提醒，
写入用的是「临时文件 + 替换」的原子写法，不会写到一半变成坏 JSON。

**Q：TTS 有爆音 / 太机器人**
Piper 的 `zh_CN-huayan-medium` 是官方唯一的中文女声。调 `noise_w_scale` 到 0.9~1.0、
`length_scale` 到 1.0~1.05 会明显更自然。想要更接近真人可以换 Kokoro-82M 的中文女声
（`zf_xiaobei` 等），需要额外装 `kokoro-onnx` + `misaki[zh]`，接口与本项目 `TtsEngine` 一致，可平滑替换。

**Q：LLM 输出里有 `**`、`#` 被念出来**
`voice_loop/text.py` 已经过滤 Markdown / 表情 / 链接 / LaTeX。若还有漏网，
把样例加进 `[llm] system_prompt` 的约束，或在 `text.py` 里补一条正则。

**Q：Ollama 连不上**
`ollama list` 能列出模型但程序报错时，多半是没起服务：另开终端跑 `ollama serve`。

---

## 9. 运行环境说明

- 实际运行在**系统 Python 3.13.7**（路径用 `where python` 或 `Get-Command python` 查，
  形如 `C:\...\Python313\python.exe`）。3.13 是目前唯一能凑齐依赖的版本：
  OpenVINO / sherpa-onnx / optimum-intel 都还没有更晚版本的轮子。
- 安装时对系统 Python 3.13 做过这些改动（如影响其它项目可回滚）：
  `torch 2.13.0 → 2.14.0`、`numpy 2.5.3 → 2.4.6`、`requests 2.32.5 → 2.34.2`，
  并新增 `openvino`、`openvino-genai`、`sherpa-onnx`、`optimum-intel`、`transformers`、
  `piper-tts`、`onnxruntime`、`sounddevice`、`soundfile`。
- VS Code 里按 `Ctrl+Shift+P` → `Python: Select Interpreter` → 选 3.13.7，
  右下角、调试、终端就都一致了。
- **控制台中文**：重定向到管道/文件时 Windows 用 cp936，代码里已把
  `✓ ✗` 换成 GBK 安全的 `√ ×`。直接看控制台或输出到文件再看都正常。

---

## 10. 隐私说明

麦克风音频、识别文本、模型推理全部在项目目录本地完成（包括闹钟、备忘、日程，
都只是本机 json 文件）。唯一的外部依赖是 `http://127.0.0.1:11434`（本机 Ollama）。
断网可正常使用。

---

## 11. 协议

[MIT](LICENSE) © 2026 Azurwolf1024

用到的模型各自遵守自己的协议：Whisper 与 SenseVoiceSmall 是 MIT，
Piper 与它自带的中文声线是 MIT，Silero VAD 是 MIT，Qwen2.5 是 Apache-2.0。

