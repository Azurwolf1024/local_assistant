# `voice_loop/tts/` —— 语音合成子系统（可单独拿走）

> 这一个目录就是「怎么出声」。它**不认识** ASR、记忆、技能、管线
> （`scripts/tests/test_module_boundaries.py` 用 AST 盯着，docstring 里提到不算违规）。
> 需要识别能力时走**注入**（`hooks.py`），不是 import。

## 分层

```
VoiceSpec（谁的声音）        spec.py         provider / voice_id / model / ref_audio / params / fallback
        ↓
TtsRegistry（谁来实现）      registry.py     扫 providers/、配置副本、降级链、LRU 缓存
        ↓
引擎（providers/*.py）        create(settings, spec) → 有 .synth(text) 的对象
        ↓
VoiceShell（换嗓子）         session.py      幂等、组合 LazyTts（保留 on_load/configure 语义）
        ↓
文本 → 音频的公共加工        textcheck.py / pacing.py / pitch.py / precision.py / refclean.py
```

| 文件 | 管什么 | 什么时候要改它 |
| --- | --- | --- |
| `spec.py` | `VoiceSpec` / `ProviderSpec` / 能力常量 | 加"能力"或参数 |
| `registry.py` | 发现后端、`effective()` 配置副本、`chain()` 降级、`open()` 缓存 | 加别名 / 改降级策略 |
| `providers/*.py` | 每个后端一条 `PROVIDER`（**加后端只改这里**） | 加后端 |
| `session.py` | `VoiceShell`：`use(spec)` 幂等换嗓子 | 改切换逻辑 |
| `piper_tts.py` / `zipvoice_tts.py` / `edge_tts.py` / `http_tts.py` / `api_tts.py` | 具体引擎 | 改推理/协议 |
| `audio_decode.py` / `async_bridge.py` | 音频解码、同步里跑异步 | 接新格式/新 SDK |
| `pacing.py` / `pitch.py` / `precision.py` / `textcheck.py` | 断句停顿、音区守卫、精度、合成回听校验 | 调音 |
| `refclean.py` / `speaker.py` | 参考音频净化、声纹相似度（尺子） | 做声线质量评估 |
| `hooks.py` | **注入位**：谁帮我转写参考音频 | 想给 TTS 加外部能力时 |

## 公开契约

```python
from voice_loop.tts import create_tts

tts = create_tts(settings, logger, lazy=True)   # 惰性：第一次 synth 才加载
for rate, pcm in tts.synth("你好呀"):           # pcm: np.int16 / np.float32 都行，看引擎
    ...
tts.use(spec)                                   # 换嗓子（幂等；不污染全局 settings）
tts.load() / tts.unload() / tts.benchmark() / tts.close()
```

后端要实现的只有一件事：`synth(text) -> Iterator[(sample_rate, pcm)]`，
外加可选的 `configure(settings)` / `set_text_verifier(fn)` / `close()`。

## 加一个后端（不改别处）

```python
# voice_loop/tts/providers/mine.py
from ..spec import CAP_SPEED, FieldSpec, ProviderSpec

def _create(settings, spec):
    from ..mine_tts import MyTts      # ★重依赖在这里才 import★
    return MyTts(settings)

PROVIDER = ProviderSpec(
    id="mine", label="我的后端",
    capabilities=frozenset({CAP_SPEED}),        # clone / voice_id / speed / stream / cloud / style
    fields=(FieldSpec("model", "模型", "path"),),
    create=_create,
)
```

控制台表单、`[tts.providers.<id>]` 覆盖、降级链都会自动认它。

## 配置面

```toml
[tts]
provider = "zipvoice"        # 主后端（空 = piper）
fallback = ["piper"]         # 降级链（末尾永远有 piper，保证"至少能出声"）
max_loaded = 1               # 同时缓存几个引擎实例
[tts.providers.<id>]         # 每个后端的私有参数
```

人格卡里存的是 `"tts": {provider, voice_id, ref_audio, params, fallback}`（`VoiceSpec`）。

## 两条不变量

1. **不会把嘴弄哑**：降级链末尾永远有 `piper`；配置问题（缺模型/缺 key）只提示一次。
2. **换嗓子不污染全局**：`effective()` 造的是**配置副本**，两个角色可以同时持有不同后端。

## 单独优化它

```powershell
python scripts\tests\test_tts_registry.py            # 契约自测（假服务器，不联网）
python scripts\tests\test_tts_registry.py --online   # 真机合成（piper / edge）
python main.py tts "你好呀" --out a.wav              # 合成一段
python scripts\tools\ab_voice.py --hiss              # 音质 A/B（沙沙声 / 节奏）
python scripts\tools\pitch_report.py                 # 音区/音高报告
python scripts\tools\pick_voice_ref.py               # 挑参考音频
python scripts\tools\spk_check.py out.wav --ref ...  # 声纹像不像（尺子）
```

## 已知限制

- `zipvoice_tts.py` 还有 34 KB：克隆参考音频的预处理（剪静音、净化、转写、情绪参考）
  都还在里面，想再拆一层的话按「素材 → 特征 → 解码」切。
- `edge` / `http` 后端是"能接任意云端"的通道，但**不保证**延迟和音质；
  生产用前先看 `[tts.providers.edge]` 的说明。
- 想量化"像不像她"，用 `speaker.py` 的声纹相似度，别靠耳朵记。
