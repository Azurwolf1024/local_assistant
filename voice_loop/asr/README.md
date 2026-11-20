# `voice_loop/asr/` —— 语音识别子系统（可单独拿走）

> 这一个目录就是「怎么听」。它**不认识** TTS、记忆、技能、管线（有自测守着：
> `scripts/tests/test_module_boundaries.py`），所以可以单独优化、单独评测、单独替换。

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
唤醒        wake.py（文本匹配）或 kws.py（流式关键词）    
        ↓
评测        eval.py（CER / 唤醒 / 误唤醒 / RTF）
```

| 文件 | 管什么 | 什么时候要改它 |
| --- | --- | --- |
| `base.py` | `AsrResult` / `AsrEngine` 契约 | 几乎不改 |
| `spec.py` | `AsrSpec`（怎么听）、`ProviderSpec`（后端怎么描述自己） | 加"能力"或"复核条件"时 |
| `registry.py` | 发现后端、合成配置副本、降级链、实例缓存 | 加别名 / 改默认策略时 |
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
h.transcribe(audio, 16000, prefer="fast")         # ★按能力挑最快的★（不要写引擎名）
h.set_heavy_enabled(False) / h.load_heavy()       # 两级加载（待唤醒 ↔ 唤醒后）
h.describe()                                      # "sensevoice(常驻) + whisper(按需)"
h.close()
```

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
[asr.providers.<id>]   # 每个后端的私有参数（model/device/num_threads…）
[asr.lexicon]          # 词库：files / from_personas / fix / fix_min_score / max_words
[asr.kws]              # 两阶段唤醒：enabled / model_dir / keywords_file /
                       #             keywords_score / keywords_threshold
```

老写法 `strategy = "sensevoice" | "whisper" | "hybrid"` 仍然认（映射成同一份 spec）。

## 三条不变量

1. **待唤醒绝不加载 `heavy` 引擎**：`set_heavy_enabled(False)` 之后连"顺手加载"都不许。
   关闭**不阻塞**（< 0.3 s）；后台正在加载时返回 `False`，由加载线程自己收尾卸掉。
2. **复核只在条件命中时发生**：`verify_when` 没命中就连复核引擎都不构造。
3. **宁可"没听清"，也不凭空编**：主引擎没听出字 + 复核结果不像话（中文场景零汉字）
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
