# 安装、环境、加速与性能

> 从 [`README.md`](../../README.md) 拆出：第 3 章、第 7 章、第 9 章、第 12 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。

---
## 3. 安装

### 3.1 一键（推荐）

```powershell
cd <项目目录>                                   # 已经有代码的话
powershell -ExecutionPolicy Bypass -File install.ps1
```

它只做三件事，每件都**先检查再动手**，所以随时可以再跑一遍：

1. 拿到代码（当前目录有 `main.py` 就用现成的；没有就 `git clone`；也可以用 `-Zip <压缩包>` 从 Releases 的包装）
2. 建 `.venv` 并 `pip install -r requirements.txt`
3. 剩下的交给 `python main.py setup`（下模型 → 体检 → 告诉你下一步）

先看看它要干什么（**不动机器**）：`... -File install.ps1 -DryRun`。
其它开关：`-Dir D:\local_AI`（装到哪）、`-Zip .\local_assistant-1.1.0-source.zip`、
`-Mirror https://pypi.tuna.tsinghua.edu.cn/simple`（国内加速）、`-NoModels`（先不下模型）。

### 3.2 手动（和上面等价，六条命令）

```powershell
cd <项目目录>
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py setup                  # 环境/依赖/配置/模型/自检一条龙（幂等，可反复跑）
ollama pull qwen3.5:4b                # 主模型：文本 + 看图 + 工具（3.4 GB）
python scripts/download_models.py --only zipvoice   # 可选：ZipVoice 音色克隆（约 156 MB；不装就退回 Piper）
python scripts/download_models.py --only speaker    # 可选：声纹尺子（27 MB，量「像不像她」，不参与合成）
```

`python main.py setup` 会在最后给你一句结论：**「可以用了」** 或「还有 N 项要处理」，
每一条都带「怎么办」。等价的手工命令是
`pip install -r requirements.txt` + `python scripts/download_models.py` + `python main.py selftest`。

> **Whisper（高精度 ASR）的权重是导出来的，不是下载的**——`download_models.py` 不管它。
> 想让 `[asr] strategy = "hybrid"` 真的能复核，按下面这条导出一次（约 930 MB，要装
> `optimum-intel[openvino]`，`requirements.txt` 里已经有了）：
>
> ```powershell
> optimum-cli export openvino --trust-remote-code --model openai/whisper-large-v3-turbo `
>     --weight-format int8 --disable-stateful models/asr/whisper-large-v3-turbo-int8-ov
> ```
>
> 没导出也能跑：`selftest` 会提示缺哪个文件，运行时会一直用 SenseVoice 那条快路径
> （中文短句够用）。各模型分别多大见 [`models/README.md`](../../models/README.md)。

想让它能**读文件**（PDF），再补一行：

```powershell
pip install pypdf                 # 可选：读 PDF（Word 靠已装的 python-docx）
```

### 3.3 装完先自检（换机器 / 换系统也一样）

```powershell
python main.py doctor                     # 只读体检：环境/依赖/配置/模型/服务/平台/磁盘（几秒）
python scripts/check_deploy.py            # 同一套检查的原始输出（更啰嗦）
python scripts/check_deploy.py --json     # 给脚本或 CI 看
python main.py selftest                   # 真加载一遍模型，端到端 10 项
```

`doctor` 与 `setup` 都**只读**（不改配置、不下载、不加载模型），
它们会把每个问题连「怎么办」一起说清楚（缺哪个包、哪条路径不对、当前系统会少哪个功能）。
搬家后会坏的东西基本都能在这儿暴露出来。

### 3.4 升级与备份（★你的数据不会被碰★）

```powershell
python main.py upgrade            # 只检查：和远端差几个提交、能不能升（不动工作区）
python main.py upgrade --apply    # 真升级：备份 → git pull --ff-only → 依赖 → 模型 → 自检
python main.py upgrade --restore  # 看能还原什么（默认只列，加 --apply 才真写）
python main.py update             # 压缩包/exe 安装的升级（下载新版并安全合并，见下）
python main.py update --apply     # 真更新（先备份，再覆盖程序文件）
```

规矩写在代码里，也写在工程日志第 55 节与第 56 节：

| 事 | 怎么做 |
| --- | --- |
| **动手前先备份** | `data/` 下的 json / personas / knowledge / memory + `config.toml` 一起拷进 `data/backup/upgrade-<时间>-<sha>/`，附 `MANIFEST.txt`（每个文件的 sha256）与 `RESTORE.txt`（不依赖本工具的还原命令） |
| **工作区脏就停** | 有未提交改动时 `git pull` 的结果不可预测，直接拦下并告诉你怎么保命（`git stash push -u`）；硬来要显式 `--allow-dirty` |
| **失败给回去的路** | 任何一步失败都打印 `git reset --hard <升级前的 sha>` 与备份目录 |
| **只换程序，不碰数据** | 升级改的是源码与配置模板；`data/` 里的日程、备忘、人格、记忆、素材都不在升级范围内 |
| 压缩包安装的怎么升 | 走 `python main.py update`（或控制台「更新」标签页）：读 GitHub Release 里的 `latest.json` → 备份 → 下载 zip → **校验 sha256** → 只覆盖程序文件（`data/`、`models/`、`sessions/`、`.venv/`、`config.toml` 一律跳过） |
| **界面上也能升** | 控制台「更新」标签页：看当前/远端版本 → 试运行（只列步骤）→ 现在更新（进度实时刷出来）；出问题可以一键还原备份 |

```powershell
python --version  # 顺带一提：项目要求 Python ≥ 3.11（settings.py 用标准库 tomllib）
```

> 版本号只有一个地方写：根目录的 [`VERSION`](../../VERSION)。
> 要发版：改它 → `python scripts/make_release.py`（产出源码 zip + sha256 + `latest.json`）→
> 按它打印的 `git tag` / `gh release create` 两条命令发布。
> 发布包里只有 **git 跟踪的文件**（`data/`、`models/`、`sessions/`、`.venv*` 天然不进包）。
> 想连控制台 exe 一起发：`python scripts/make_console_exe.py`（见第 4 节末尾）。

### 3.5 控制台 exe（不想开命令行的话）

把控制台冻成**双击就能用**的 exe（原生窗口，不再是浏览器标签页）：

```powershell
python scripts/make_console_exe.py                  # 产出 dist/local-assistant-console/（约 50 MB，启动 1~2 秒）
python scripts/make_console_exe.py --with-webview    # 连 pywebview 一起打（真原生窗口，约 54 MB）
python scripts/make_console_exe.py --onefile         # 单个 exe（好分发，但每次启动要解包：约 6 秒）
python scripts/make_console_exe.py --check           # 只打印计划与要补的 hidden-import
```

把产物（`dist/local-assistant-console/` 整个文件夹，或单个 exe）放进项目根，双击即可：

- exe 会**自己往上找** `config.toml` + `main.py` 定位项目（也可以 `--root` 或环境变量 `LOCAL_AI_ROOT` 指定）
- 窗口按「pywebview → Edge 应用窗口 → 系统浏览器」挑第一个能用的（`--window` 可写死）
- ★关窗口 = 退出控制台★；语音服务不受影响（它有自己的一套启停，见顶栏）
- 同一个端口已经有控制台在跑时，双击**不再报错**，直接把那个窗口打开
- 出问题时看 `sessions/console-exe.log`；`local-assistant-console.exe --selftest` 会把
  「面板装上了吗、接口答不答」写进同一份日志

> **为什么不把语音服务也打进 exe**：服务要几 GB 模型 + 外部 Ollama + 真实音频设备，
> 冻进去会是 2~4 GB 的怪物，还得给用户装驱动。所以 exe 是**控制台/界面**，服务仍旧用
> 系统 Python 跑（exe 点「启动服务」时会自己找 `.venv` 或 `LOCAL_AI_PYTHON`，找不到会明说）。


---


## 7. 实测性能（要换机器 / 换模型先看这个）

> 各环节延迟、内存、加速卡实测，以及「换模型怎么选」「要不要上更大的模型」
> 都在 [`docs/ENGINEERING_LOG.md`](../ENGINEERING_LOG.md) 的「7. 实测性能」
> （含 9b 反而比 4b 工具选择更差这类反直觉结论）。


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
- **音色克隆（可选）**：只用 `sherpa-onnx`（本机早有）+ 纯 Python 的 `pykakasi`
  （把日语参考文本转罗马字，见第 14 节）。**不需要 torch / CUDA / MNN**；
  `IndexTTS` 那条路暂时走不通——它的中文前端依赖 `pynini`，Python 3.13 没有 wheel（实测）。

### 9.1 换到别的系统（Linux / macOS）跑得怎么样

代码里的平台差异**集中在几处**，其余全是标准库 + 跨平台库。现状：

| 能力 | Windows（开发机，实测过） | Linux | macOS |
| --- | --- | --- | --- |
| 语音主流程（VAD→ASR→LLM→TTS） | √ | √ | √ |
| 唤醒词常驻服务 `listen` | √（`-B` 用 pythonw，无窗口） | √（`-B` 用 `start_new_session`，关终端不会被带走） | √ |
| 「关屏幕 / 开屏幕」 | √（`SC_MONITORPOWER`） | √ X11（`xset dpms`）；**Wayland 会直说做不到** | √（`pmset` / `caffeinate`） |
| 底部字幕 | √ 点得穿 + 自动避开任务栏 | 显示正常，但**不会点穿**、位置按屏幕高度估算 | 同 Linux |
| 截屏看屏幕 | √ | √（自动带 `xdisplay`）；**Wayland 可能全黑** | √ |
| 读剪贴板图片 | √ | ×（Pillow 没实现，会回一句人话） | √ |
| 开机自启 | 快捷方式放 `shell:startup` | systemd user unit / `.desktop` | LaunchAgent |
| Ollama 核显开关 `gpu --enable-igpu` | √（Windows 版会丢核显） | 不适用（自己会枚举） | 不适用（Metal） |

还没做、换系统前要知道的两件事：

1. **找文件的根目录**：`[vision] file_roots = ["桌面", "下载", "文档"]` 是中文目录名
   （`voice_loop/vision.py` 里有 `桌面→Desktop` 这类别名表，所以英文系统直接可用）。
   德语/法语等本地化目录名要自己改成实际路径，建议直接写绝对路径。
2. **依赖轮子**：本项目在 **Python 3.13** 上验证。换系统/换版本时
   `onnxruntime`、`openvino`、`sherpa-onnx` 的轮子发布节奏会先卡住你——
   `scripts/check_deploy.py` 的第 2 项会直接把缺的包名和 pip 命令列出来。

一套判断只在 `voice_loop/tts/precision.py` 和 `voice_loop/system_ops.py` 里，
所以加新平台只需改这两个地方（离线测试 `scripts/tests/test_precision.py` 会守着接口不变）。

---


## 12. GPU / 加速

一条命令看清这台机器的现状和该改什么：

```powershell
python main.py gpu                        # 有哪些加速器、每段现在用哪个
python main.py gpu --bench                # 真的跑一遍 Whisper CPU/GPU 对比（约 1 分钟）
python main.py gpu --enable-igpu          # 让 Ollama 用上核显（写环境变量 + 重启 Ollama）
```

本机（Core Ultra 5 225H + **Intel Arc 130T 核显** + AI Boost NPU）实测：

| 环节 | CPU | Arc 核显 | 说明 |
| --- | --- | --- | --- |
| Whisper turbo int8（5.6 s 音频） | 3.3 s | **0.49 s** | 快 6.8 倍；首次要编译图约 10 s |
| SenseVoiceSmall | 0.08 s | — | sherpa-onnx 官方轮子只有 CPU，但已经够快 |
| Piper TTS | RTF 0.04 | — | 瓶颈不在 TTS，加速收益很小 |
| ZipVoice 克隆 TTS | RTF 1.1~1.4 | 未验证 | **当前真正的瓶颈**；sherpa-onnx 的 zipvoice 例子只给了 `provider="cpu"`，换 provider 能不能用还没试 |
| qwen3.5:4b 生成 | 12.3 tok/s（旧模型） | **17.6 tok/s** | 新模型在核显上更快；预填稍慢（284 vs 345 tok/s） |
| qwen3.5:4b 看图（1024） | 14.6 s | **7.3 s** | 一个模型同时管文本和看图，省掉那 3.2 GB 的视觉模型 |

### 已经默认打开的

- **`[asr] whisper_device = "auto"`**：OpenVINO 报出核显就用 GPU，编译失败自动回退 CPU。
  hybrid 模式下超过 `whisper_min_duration`（默认 6 s）的音频会追加 Whisper 校验，
  这一步就是省下来的地方。
- 想强制：写 `"GPU"` / `"CPU"` / `"NPU"`。**别写 NPU** —— 这份导出是动态形状
  （`[?,128,3000]`），而 NPU 要求静态 shape，**连编译都过不了**
  （Level0 `ZE_RESULT_ERROR_INVALID_ARGUMENT`；早先版本是直接 `0xC0000005` 硬崩）。
  就算把音频一律补齐到 30 秒让它编过，核显也快它 **7.9 倍**（0.168s vs 1.33s），
  而它编译一次要 **294 秒**。`auto` 因此永远不会选它。
  附带一条结论：小模型上 NPU 比 CPU 还慢约 9 倍（Silero VAD 0.4ms vs 3.8ms），
  所以本机三件套（VAD / ASR / TTS）里它没有位置。实测脚本：`python scripts/tools/probe_npu.py`。

### 让 Ollama 用上核显

Windows 上的官方 Ollama 默认**主动把核显丢掉**，它的日志写得很清楚：

```
msg="dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1"
```

所以不用装别的发行版，设一个环境变量就行。本项目把这个开关收进了一条命令：

```powershell
python main.py gpu --enable-igpu          # 写用户环境变量 OLLAMA_IGPU_ENABLE=1 并重启 Ollama
python main.py gpu --enable-igpu --no-restart   # 只写变量，下次开机自己生效
```

手动等价的写法（在**新开的** PowerShell 里）：

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_IGPU_ENABLE", "1", "User")
$env:OLLAMA_IGPU_ENABLE = "1"      # 这一句不能省，否则从当前窗口重启的 Ollama 读不到
```

生效后 `ollama ps` 的 PROCESSOR 列会从 `100% CPU` 变成 `100% GPU`，
`python main.py gpu` 也会直接告诉你它选中了哪块设备（本机是
`Intel(R) Arc(TM) 130T GPU (16GB)（Vulkan，核显）`）。

几个坑：

- **`OLLAMA_GPU_BACKEND=sycl` / `OLLAMA_NUM_GPU=99` 是没用的**。前者不在官方
  Ollama 认识的环境变量表里（那套是 Intel 的 IPEX-LLM 分支在用），后者只是把模型
  尽量多放显存、默认已经是这个行为。真正起作用的只有 `OLLAMA_IGPU_ENABLE`。
- Intel 那个 `ipex-llm` 仓库**已于 2026-01-28 归档**，之前 README 里
  “装 IPEX-LLM 版 Ollama” 的建议已过时，别照做了。
- 设完用户环境变量后，从**已经开着的**窗口用 `Start-Process` 重启 Ollama 是没用的
  （子进程继承的是旧环境）——`--enable-igpu` 会自己在启动进程里补上这份环境。
- 核显和 CPU **共享同一块内存**（本机 31.5 GiB 里大约能借到 16~18 GiB）。
  模型常驻会让系统可用内存变少，本项目默认 `[wake] unload_llm` 已在待机时把模型
  卸掉；嫌内存紧张再加 `OLLAMA_MAX_LOADED_MODELS=1`。
- Ollama 官方对**核显 + Vulkan** 的稳定性是留了余量的（不如独显稳）。如果偶尔出现
  加载失败或结果异常，先 `python main.py gpu` 看一眼设备，再考虑退回 CPU。
