"""通用 HTTP TTS：「给个 API 就能接」的那种后端。

它自己不绑定任何厂商，全靠 `[tts.providers.<id>]` 里的 url / headers / body 模板。
所以同一个文件能接 ElevenLabs、Azure、Fish Audio、火山、自建服务 —— 换家只改配置。

用法见 :mod:`voice_loop.tts.http_tts` 的模块说明；这里只负责把参数声明出来，
让控制台能画表单、让注册表能做校验。
"""

from __future__ import annotations

from ..spec import CAP_CLOUD, FieldSpec, ProviderSpec

FORMATS = ("mp3", "wav", "pcm", "ogg", "flac")


def _ready(settings) -> tuple[bool, str]:
    block = (getattr(settings.tts, "providers", {}) or {}).get("http") or {}
    if not str(block.get("url") or "").strip():
        return False, "没配 [tts.providers.http] url"
    return True, ""


def _create(settings, spec):
    from ..http_tts import HttpTts  # noqa: PLC0415

    return HttpTts(settings, spec)


def _apply(settings, spec):
    """url/headers/body 都是 provider 私有参数，引擎从 options 里读。"""
    return settings


PROVIDER = ProviderSpec(
    id="http",
    label="通用 HTTP TTS（自己填接口）",
    capabilities=frozenset({CAP_CLOUD}),
    fields=(
        FieldSpec("url", "接口地址", "text", hint="POST 一段 JSON，返回音频字节"),
        FieldSpec("method", "方法", "select", default="POST", options=("POST", "GET")),
        FieldSpec("headers", "请求头（JSON）", "json",
                  hint='{"Authorization": "Bearer {key}"}；{key} 从 key_env 取'),
        FieldSpec("body", "请求体模板", "json",
                  hint='{"input": "{text}", "voice": "{voice}"}；占位符 text/voice/model/lang/key'),
        FieldSpec("audio_format", "音频格式", "select", default="mp3", options=FORMATS),
        FieldSpec("sample_rate", "裸 PCM 采样率", "number", default=24000),
        FieldSpec("key_env", "密钥环境变量", "text", hint="把 key 放环境变量，别写进仓库"),
        FieldSpec("timeout", "超时（秒）", "number", default=60),
    ),
    create=_create,
    apply=_apply,
    ready=_ready,
    hint="一个文件接任意云端 TTS：改 url / headers / body 就行，不用写 Python。",
    wants_fallback_factory=True,
)
