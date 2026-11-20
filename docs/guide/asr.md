# 语音识别：怎么听、词库、评测

> 从 [`README.md`](../../README.md) 拆出：第 16 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。

---
## 16. 语音识别（ASR）：怎么听、词库、评测

> 解耦的动机、踩过的坑、以及与「识别准确性 / 唤醒速度 / 词库匹配」的关系，
> 见工程日志 **第 63 节**。这一节只讲**怎么用**。

### 16.1 它现在是四层，不再是一个大类

```
音频前置（VAD / 回声 / 增益）         ← 已有，只暴露「一句一句交出来」
   ↓
引擎（providers/）                    ← 只负责 audio → 文字 + 置信度
   ↓
解码策略（session.Hearing）           ← 主引擎快路径 + 按需复核 + 择优（+ 标点移植）
   ↓
词库（lexicon.Lexicon）               ← 一份数据，四种用法（见 16.3）
   ↓
唤醒（wake.py 文本匹配，或 kws.py 流式关键词检测，见 16.4）
```

`pipeline` 只认识 `self.asr`（一个 `Hearing`），**不再出现引擎名字**：
以前那几处 `set_whisper_enabled / whisper_loaded / prefer="sensevoice"` 现在分别是
`set_heavy_enabled / heavy_loaded / prefer="fast"`（按**能力**选，不按名字）。

### 16.2 配置：主引擎 + 复核引擎 + 复核条件

```toml
[asr]
provider = "sensevoice"                     # 常驻的快引擎
verify = "whisper"                          # 按需的准引擎（空 = 不复核）
verify_when = ["empty", "long:6.0", "score<0.45"]   # 什么情况下复核
fallback = []                               # 主引擎起不来时的兜底顺序
max_loaded = 2                              # 同时常驻几个引擎
language = "zh"

[asr.providers.sensevoice]                  # 每个后端的私有参数
model = "models/asr/sensevoice-small/model.int8.onnx"
tokens = "models/asr/sensevoice-small/tokens.txt"
sensevoice_use_itn = true

[asr.providers.whisper]
model = "models/asr/whisper-large-v3-turbo-int8-ov"
device = "auto"                             # auto = 有核显用核显，否则 CPU
```

| 复核条件 | 意思 |
| --- | --- |
| `empty` | 主引擎什么都没听出来 |
| `long:6.0` | 音频超过 6 秒（长句 Whisper 更稳） |
| `score<0.45` | 质量分偏低（复读、幻觉句、过短都会拉低） |
| `always` | 总是复核（调试用） |

★老写法仍然认★：`strategy = "sensevoice" / "whisper" / "hybrid"` 会被映射成同样的
`provider/verify/verify_when`（`hybrid` 就是上面那三条，阈值跟着 `whisper_min_duration` 走）。

### 16.3 词库：一份数据，四种用法

```toml
[asr.lexicon]
files = ["data/lexicon/*.txt"]    # 一行一个词：源石 / 罗德岛 2.5 / 泰拉|泰拉大陆,泰拉世界
from_personas = true              # 名字 / 唤醒词 / aliases 自动并入
fix = true                        # 事后纠正（拼音 + 编辑距离）
fix_min_score = 0.78              # 相似度低于它就改（越低越激进、误改越多）
```

| 后端能力 | 词库怎么进去 | 谁是这样 |
| --- | --- | --- |
| `native`（热词表） | 生成 `hotwords.txt` 交给引擎，真正加权 | `sherpa` 的 transducer / zipformer |
| `prompt`（解码提示） | 塞进初始提示词 | 需要支持的运行时（当前 Whisper 导出不支持） |
| `post`（事后纠正） | 拼音近似 + 编辑距离 | `sensevoice`、`whisper` |

★保守优先★：纠正只在「同长度窗口 + 只有一个音节不同 + 相似度够高」时动手，
而且遇到常见虚词/已有规范词一律跳过 —— 实测「海尔洗衣机」不会被改成「凯尔希」。

### 16.4 唤醒速度：两阶段唤醒（可选，默认关）

```
待唤醒：麦克风 → KeywordSpotter（流式、30 MB、毫秒级）→ 命中
   ↓ 只在这一刻
活跃：  VAD 分段 → 完整识别（SenseVoice / hybrid）→ 命令
```

```powershell
python scripts\download_models.py --only kws     # 下 30 MB 关键词模型（可选）
```
```toml
[asr.kws]
enabled = true                    # ★先量过再开★
model_dir = "models/asr/kws-zipformer"
keywords_file = ""                # 空 = 用词库自动生成 keywords.txt
```

关掉时（默认）完全回到「整句识别 + 文本匹配」；开了但模型没下，也会自动退回，
只在日志里说一句原因。所以**开着 kws 才需要那个模型** —— 体检（`main.py doctor`）
也只在 `enabled = true` 时才为它报「模型不存在」，关着不会拦你。

### 16.5 评测台：改了什么，立刻能量出来

```powershell
python scripts\eval_asr.py --synth                   # 用本地 TTS 合成素材（唤醒词/人名/命令/负样本）
python scripts\eval_asr.py --set wake                # 唤醒命中率 + 误唤醒
python scripts\eval_asr.py --set names --set command # CER
python scripts\eval_asr.py --matrix                  # 各后端一张对比表
```

素材两套，放在一起但**分开看**：

- `data/asr_eval/synth/…`：TTS 合成的，干净、覆盖广 → 证明「词库和模型对得上」
- `data/asr_eval/wake|names|command|negative/…`：你自己录的 + 同名 `.txt`
  → 带口音和噪声，**这才是结论**

本机一次真实结果（合成素材，8 条唤醒词）：

```
CER 0.499 · 唤醒命中 25.0% · 误唤醒 0.0% · RTF 0.052 · 平均 2.04s
「白泽」→「白色」  「能天使」→「明天」  「克莱门莎」→「克莱」
```

—— 这就是**下一步该改哪里**：同音不同调的字靠 `post` 纠正改不回来，
要么加 aliases（`data/personas/*.json` 的 `aliases`）、要么把 `fix_min_score` 调低
（误改会上升，用 `--set negative` 盯着）、要么换 `sherpa` + zipformer 用原生热词。
评测台就是为了让这三个选择**有数可依**。

### 16.6 加一个识别后端 = 加一个文件

```python
# voice_loop/asr/providers/我的后端.py
from ..spec import CAP_LOCAL, CAP_FAST, HOTWORDS_POST, ProviderSpec

def _create(settings, spec):
    from ..我的实现 import MyEngine      # ★在 create() 里才 import★
    return MyEngine(...)

PROVIDER = ProviderSpec(
    id="mine", label="我的后端", capabilities=frozenset({CAP_LOCAL, CAP_FAST}),
    hotwords=HOTWORDS_POST, create=_create,
)
```

放进 `voice_loop/asr/providers/` 就会被注册表扫到，配置里写 `provider = "mine"` 即可，
`pipeline` / 控制台 / `require_models` 一处都不用改。

### 16.7 兼容

| 老东西 | 现在 |
| --- | --- |
| `AsrRouter` | `Hearing` 的别名（同一个类） |
| `set_whisper_enabled / load_whisper / whisper_loaded` | `set_heavy_enabled / load_heavy / heavy_loaded`（老名字仍可调） |
| `VoiceLoop(lazy_whisper=True)` | `lazy_heavy`（老参数名仍可传） |
| `transcribe(..., prefer="sensevoice")` | `prefer="fast"`（按能力选；写具体后端也认） |
| `[asr] strategy` | 映射成 `provider/verify/verify_when` |
