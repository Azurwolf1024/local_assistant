# `voice_loop/asr/` —— 语音识别子系统（可单独拿走）

> 这一个目录就是「怎么听」。它**不认识** TTS、记忆、技能、管线（有自测守着：
> `scripts/tests/test_module_boundaries.py`），所以可以单独优化、单独评测、单独替换。
> 定位：全本地（没有云端识别后端）；项目总览见 [`../../README.md`](../../README.md) §1.4。

## 分层

```
音频前置（voice_loop/audio.py 的 VAD / 回声 / 增益）   ← 只负责「一句一句交出来」
        ↓  np.ndarray(float32, 16 kHz)
引擎        base.py + providers/*.py                   audio → AsrResult(text, score, rtf)
        ↓
解码策略    session.py（Hearing）                       主引擎快路径 + 按需复核 + 择优
        ↓
词库        lexicon.py                                  一份数据 → 热词表 / 提示词 / 事后纠正
        ↓
唤醒        wake.py（文本匹配）或 kws.py（逐句触发的流式模型，不是常驻监听）
        ↓
评测        eval.py（CER / 唤醒 / 误唤醒 / RTF）
```

| 文件 | 管什么 | 什么时候要改它 |
| --- | --- | --- |
| `base.py` | `AsrResult` / `AsrEngine` 契约 | 几乎不改 |
| `spec.py` | `AsrSpec`（怎么听）、`ProviderSpec`（后端怎么描述自己） | 加"能力"或"复核条件"时 |
| `registry.py` | 发现后端、合成配置副本、`prefer="fast"` 的选路链、实例缓存 | 加别名 / 改默认策略时 |
| `providers/*.py` | 每个后端一条 `PROVIDER` 声明（**加引擎只改这里**） | 加引擎 |
| `sensevoice.py` / `whisper_ov.py` / `sherpa_offline.py` | 具体引擎实现（重依赖都在 `create()` 里才 import） | 改推理细节 |
| `quality.py` | 质量分（择优 + 触发复核） | 调"什么时候算可疑" |
| `session.py` | `Hearing`：管线唯一入口 | 改策略/两级加载 |
| `lexicon.py` | 词库（四种消费方式） | 提准确率主要在这 |
| `kws.py` | 流式关键词检测（可选两阶段唤醒） | 提唤醒速度 |
| `eval.py` | 指标与素材 | 加指标 |

## 公开契约（外面只该用这些）

```python
from voice_loop.asr import Hearing, AsrSpec, AsrRegistry, AsrResult

h = Hearing(settings, logger)                     # 预加载策略里的引擎
h.transcribe(audio, 16000)                        # 按策略（快路径 + 需要时复核）
h.transcribe(audio, 16000, prefer="fast")         # ★按能力挑最快的★（推荐；引擎名仍兼容）
h.set_heavy_enabled(False) / h.load_heavy()       # 两级加载（待唤醒 ↔ 唤醒后）
h.describe()                                      # "sensevoice(常驻) + whisper(按需)"
h.close()
```

★`prefer="fast"` 只在 Python API 里可用★（按能力选路，也会用 `fallback` / chain）；
`python main.py asr` 的 `--engine` 只有 `auto / sensevoice / whisper / both`。
`Hearing(...)` 默认 `preload=True`，会按 `_wanted()` 提前构造复核引擎；
只有两级加载（`heavy_enabled=False`）或 `preload=False` 时，"复核引擎没命中就不构造"才严格成立。

后端要实现的只有一件事：

```python
class MyEngine:
    name = "mine"
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> AsrResult: ...
```

## 加一个引擎（不改别处）

```python
# voice_loop/asr/providers/mine.py
from ..spec import CAP_FAST, CAP_LOCAL, HOTWORDS_POST, ProviderSpec

def _create(settings, spec):                     # ★重依赖在这里才 import★
    from ..mine_engine import MyEngine
    return MyEngine(settings.asr.model)

PROVIDER = ProviderSpec(
    id="mine", label="我的引擎",
    capabilities=frozenset({CAP_LOCAL, CAP_FAST}),   # heavy = 要走两级加载
    hotwords=HOTWORDS_POST,                          # native / prompt / post / none
    create=_create,
)
```

配置里写 `provider = "mine"` 就生效 —— 控制台、`require_models`、展示字符串都是从注册表读的。

## 配置面（`[asr]`）

```toml
provider / verify / verify_when / fallback / max_loaded / language
  ★ fallback 只在 prefer="fast" 选路时参与；默认 transcribe 主引擎起不来直接返空，靠 verify 兜底
[asr.providers.<id>]   # 每个后端的私有参数（model/device/num_threads…）
  ★ whisper 的 initial_prompt 当前不生效（导出没有 prompt 入口），词库走事后纠正
[asr.lexicon]          # 词库：files / from_personas / fix / fix_min_score / max_words
  ★ fix_min_score：相似度**达到**阈值才改；阈值越低越激进（代码为准）
[asr.kws]              # 两阶段唤醒：enabled / model_dir / keywords_file /
                       #             keywords_score / keywords_threshold
                       # keywords_file 留空时由词库按 tokens.txt 自动生成（config.toml 未写出该字段）
```

老写法 `strategy = "sensevoice" | "whisper" | "hybrid"` 仍然认（映射成同一份 spec）。

## 三条不变量

1. **待唤醒绝不加载 `heavy` 引擎**：`set_heavy_enabled(False)` 之后连「顺手加载」都不许。
   关闭**不阻塞**（拿不到锁就返回 `False`，由加载线程自己收尾卸掉）；没有 `< 0.3 s` 的上限保证。
2. **复核只在条件命中时才跑**（不等于"不构造"）：`verify_when` 没命中就不做第二次识别；
   但默认 `preload=True` 会按 `_wanted()` 提前构造复核引擎。只有两级加载（`heavy_enabled=False`）
   或 `preload=False` 时，"没命中连引擎都不构造"才成立。
3. **宁可「没听清」，也不凭空编**：主引擎没听出字母数字、且复核结果零汉字**或汉字占比 < 0.15**
   时按空结果处理 —— Whisper 在短音上会吐 `MING PAO CANADA…` 这种整句幻觉。

## 单独优化它

```powershell
python scripts\tests\test_asr_registry.py            # 契约自测（假引擎，不下载模型）
python scripts\eval_asr.py --synth                   # 合成素材（TTS 由脚本注入）
python scripts\eval_asr.py --set wake                # 唤醒命中率 / 误唤醒
python scripts\eval_asr.py --set names --set command # CER
python scripts\eval_asr.py --matrix                  # 所有就绪后端一张对比表
python main.py asr some.wav                          # 单个文件，看分工时/RTF
```

调参→量→再调的常见三条路（按代价从小到大）：

| 想改善 | 先试 | 再不济 |
| --- | --- | --- |
| 人名/术语听错 | `data/lexicon/*.txt` 加词、人格卡加 `aliases` | 换 `sherpa` + zipformer（原生热词） |
| 短句听不清 | 调 `verify_when`（更像"总是复核"一点） | 换更大的主引擎 |
| 唤醒慢/费电 | 开 `[asr.kws]`（30 MB 模型） | 缩短 VAD 静音判定 |

## 已知限制（写清楚，免得当成 bug）

- **SenseVoice 不支持热词表**：只能事后纠正，或配 `hr_lexicon` + `hr_dict_dir`（引擎侧同音字替换）。
- **Whisper 的提示词偏置目前不生效**：这个 OpenVINO 导出 + transformers 5.x 没有
  `prompt/prompt_ids` 入口；所以它的词库走事后纠正。
- **KWS 是"整段音频上跑流式模型"**，不是真正的常驻流式监听：VAD 切完那段喂进去，
  省掉的是"完整识别"的开销。要更激进得改管线的麦克风循环。
- **没有云端识别**：`CAP_CLOUD` 只是闸门占位（`[asr] allow_cloud = false`）。
- **`HOTWORDS_PROMPT` 目前没有后端使用**：SenseVoice / Whisper 都是 `post`，只有 sherpa 的
  transducer / zipformer 是 `native`；`providers/whisper.py` 顶部的旧注释已改正，以 `PROVIDER`
  声明为准。
- **`verify_when` 用字符串最省心**：`["empty", "long:6.0", "score<0.45"]`；字典写法
  `{"score<": 0.45}` 也已支持（2026-11-22 修），裸 `"long"` 没有阈值会被忽略，不会恒真。
- **`fallback` 不是默认转写的重试链**：只在 `prefer="fast"` 选路时用；主引擎失败默认返空，
  靠 `verify` 复核补（旧注释里的"降级链"按此理解）。
