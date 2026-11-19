"""Edge TTS：微软 Edge 的在线朗读（免费、无需 key）。

★这是本次解耦的验收标尺★：整个后端只有两个新文件
（``tts/edge_tts.py`` 引擎 + 这个声明）+ 一段 config，
pipeline / settings / 控制台都没为它写过一个 if。

它也是唯一一个既能「选音色」又「不用配 key」的云端后端，
所以很适合当「换一个模型/服务试试」的第一站。
"""

from __future__ import annotations

from ..spec import CAP_CLOUD, CAP_SPEED, CAP_STREAM, CAP_VOICE_ID, FieldSpec, ProviderSpec

#: 官方 zh-CN 音色（名字稳定；完整列表用 `edge-tts --list-voices` 看）
VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
)


def _ready(settings) -> tuple[bool, str]:
    try:
        import edge_tts  # noqa: F401,PLC0415 - 只做存在性检查
    except ImportError:
        return False, "没装 edge-tts（pip install edge-tts）"
    return True, ""


def _create(settings, spec):
    from ..edge_tts import EdgeTts  # noqa: PLC0415 - 扫目录时不加载引擎

    return EdgeTts(settings, spec)


def _apply(settings, spec):
    """edge 的音色/语速都是它的私有参数，引擎自己从 options 里读，这里不用改 TtsConfig。"""
    return settings


PROVIDER = ProviderSpec(
    id="edge",
    label="Edge TTS（在线 · 免费 · 无需 key）",
    capabilities=frozenset({CAP_VOICE_ID, CAP_SPEED, CAP_STREAM, CAP_CLOUD}),
    fields=(
        FieldSpec("voice_id", "音色", "select", default=VOICES[0], options=VOICES),
        FieldSpec("rate", "语速", "text", default="+0%", hint="如 +10% / -20%"),
        FieldSpec("volume", "音量", "text", default="+0%"),
        FieldSpec("pitch", "音调", "text", default="+0Hz"),
        FieldSpec("timeout", "超时（秒）", "number", default=30),
        FieldSpec("block_seconds", "每块秒数", "number", default=0.2),
    ),
    create=_create,
    apply=_apply,
    ready=_ready,
    voices=lambda _settings: list(VOICES),
    hint="免费、不用 key；文本会发到微软的朗读服务。失败自动退回 Piper。",
    wants_fallback_factory=True,
)
