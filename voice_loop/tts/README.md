# `voice_loop/tts/` —— 语音合成子系统（可单独拿走）

> 这一个目录就是「怎么出声」。它**不认识** ASR、记忆、技能、管线
> （`scripts/tests/test_module_boundaries.py` 用 AST 盯着，docstring 里提到不算违规）。
> 需要识别能力时走**注入**（`hooks.py`），不是 import。
> 定位：本地 / 云端双模——五个后端按角色切换；项目总览见 [`../../README.md`](../../README.md) §1.3。

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
风格参考                     style.py        <style=calm> / 人格 tts.styles → 按风格换参考音频
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
| `style.py` | 风格标签解析 + 多档参考音频（`tts.styles`） | 让语气跟着场景走 |
| `refclean.py` / `speaker.py` | 参考音频净化、声纹相似度（尺子） | 做声线质量评估 |
| `hooks.py` | **注入位**：谁帮我转写参考音频 | 想给 TTS 加外部能力时 |

## 公开契约

```python
from voice_loop.tts import create_tts

tts = create_tts(settings, logger, lazy=True, spec=None)
#   ★lazy 只决定要不要预先 load()；两种用法返回的都是 VoiceShell
#   spec = 这次要用的声音（空 = 跟 [tts] provider）
for rate, pcm in tts.synth("你好呀"):           # pcm 恒为 np.int16 单声道
    ...
rate, pcm = tts.synth_bytes("你好呀")           # 一次性拿完整音频
tts.use(spec)                                   # 换嗓子（幂等；不污染全局 settings）
tts.load() / tts.unload() / tts.benchmark()     # 外壳没有 close()，释放用 unload()
```

后端必须实现 `synth(text) -> Iterator[(sample_rate, np.int16 PCM)]` 和 `sample_rate`
（`LazyTts` / `VoiceShell` 都要读它）；可选 `configure(settings)` / `set_text_verifier(fn)` /
`close()`；云端退路还需要 `set_fallback_factory(fn)`（注册表会尝试注入，见 `registry.py`）。

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
fallback = ["piper"]         # 链尾追加 piper；★前提是 piper 模型装好了★（见"两条不变量"）
max_loaded = 1               # 同时缓存几个引擎实例
[tts.providers.<id>]         # 每个后端的私有参数
```

人格卡里存的是一份与后端无关的声音描述（`VoiceSpec`）：
`{provider, voice_id, model, ref_audio, ref_text, params, fallback, note, styles}`
（`styles` 由人格层读取并按 `<style=...>` 挑参考；`params` 只有 edge / http 会原样透传，
deepseek 的分块参数在 `[api]` 里，不在这里）。

## 两条不变量（含边界）

1. **尽量不哑，但不是绝对**：`chain()` 末尾会追加 `piper`；deepseek / http / edge 运行时失败
   都会沿注册表注入的退路工厂换下一个候选（edge 于 2026-11-22 补齐）。★边界★：`piper` 缺模型时
   链尾仍会失败，但现在 `ready()` 会在体检阶段先报出来。
   缺文件提示由各引擎自行打印，不走注册表的去重通道，可能重复。
2. **换嗓子不污染全局**：`effective()` 造的是**配置副本**，`use()` 幂等，两个角色可以同时持有不同后端；
   外壳没有 `close()`，释放用 `unload()`。

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
  生产用前先看 `[tts.providers.edge]` 的说明。两者都已在运行时异常时走退路工厂。
- `deepseek` 的 `[tts.providers.deepseek] fallback_voice` **只用于显示兼容**，实际退路声线由角色
  `tts.fallback` 或全局 `[tts] fallback` 决定；`tts_block_seconds` 要写在 `[api]`，放 `spec.params` 无效。
- `speak_streaming` 是残留配置字段，当前没有任何读取点；`inject_pauses` 的运行时 setter 对已加载的
  Piper 引擎不生效（只有构造时读得到）。
- `main.py::require_models` 仍硬编码检查 Piper 模型路径，换后端但没装 Piper 时一次性命令会被拦下。
- `zipvoice` 的 `ready()` 判定（允许任一套精度、不查 vocoder）与引擎 `missing_files()`（精确查精度、
  查 vocoder）口径不同，体检结论和缺文件报错可能不一致。
- 想量化"像不像她"，用 `speaker.py` 的声纹相似度，别靠耳朵记。
