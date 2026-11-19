"""DeepSeek 官方 TTS（云端）。

官方只有 ``stella`` / ``tide`` 两个音色，没有克隆、没有语速 —— 所以这里的
「换声线」就是换 `[api] tts_voice`。:func:`_apply` 把 `VoiceSpec.voice_id`
映射到 **api 那份副本**（`[api]` 也是 copy，绝不改全局）。

★退路声线★：云端这一句彻底失败时，`api_tts` 会用一个本地引擎兜底。
用哪把嗓子由这里决定：`spec.fallback` 里的 ``piper:xxx`` > 配置里的
``fallback_voice`` > 当前 `[tts]` 的 piper 声线。
"""

from __future__ import annotations

import dataclasses

from ..spec import CAP_CLOUD, CAP_VOICE_ID, FieldSpec, ProviderSpec, VoiceSpec

VOICES = ("stella", "tide")


def _opts(settings) -> dict:
    return dict((getattr(settings.tts, "providers", {}) or {}).get("deepseek") or {})


def _ready(settings) -> tuple[bool, str]:
    from ..api_tts import api_tts_ready  # noqa: PLC0415

    return api_tts_ready(settings)


def _create(settings, spec):
    from ..api_tts import ApiTts  # noqa: PLC0415

    return ApiTts(settings, spec)


def _set_piper(tts, voice: str) -> None:
    name = str(voice or "").strip()
    if not name:
        return
    tts.voice = name
    tts.model = f"models/tts/piper/{name}.onnx"
    tts.config = f"models/tts/piper/{name}.onnx.json"


def _apply(settings, spec):
    opts = _opts(settings)
    api = dataclasses.replace(settings.api)
    voice = (str(spec.voice_id or "").strip()
             or str(opts.get("voice_id") or "").strip()
             or str(getattr(api, "tts_voice", "") or "").strip())
    if voice:
        api.tts_voice = voice.lower()
    # 退路声线：先看角色写的 fallback，再看配置里的 fallback_voice
    fallback_voice = str(opts.get("fallback_voice") or "").strip()
    for raw in spec.fallback:
        got = VoiceSpec.parse(raw)
        if got.provider == "piper" and got.voice_id:
            fallback_voice = got.voice_id
            break
    _set_piper(settings.tts, fallback_voice)
    return dataclasses.replace(settings, api=api)


PROVIDER = ProviderSpec(
    id="deepseek",
    label="DeepSeek 官方 TTS（云端）",
    capabilities=frozenset({CAP_VOICE_ID, CAP_CLOUD}),
    fields=(
        FieldSpec("voice_id", "官方音色", "select", default="stella", options=VOICES,
                  hint="官方只有这两个，没有克隆、没有语速"),
        FieldSpec("tts_format", "返回格式", "select", default="pcm", options=("pcm", "wav")),
        FieldSpec("tts_block_seconds", "每块秒数", "number", default=0.2,
                  hint="收够这么久就先播，不等整句"),
        FieldSpec("tts_timeout", "超时（秒）", "number", default=120),
        FieldSpec("fallback_voice", "退路 piper 声线", "text", default="zh_CN-huayan-medium"),
    ),
    create=_create,
    apply=_apply,
    ready=_ready,
    voices=lambda _settings: list(VOICES),
    hint="每 10000 字符 $0.45；文本会发到 DeepSeek。失败自动退回 Piper。",
    wants_fallback_factory=True,
)
