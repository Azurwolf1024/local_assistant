"""TTS 层对外只留一个入口：:func:`create_tts`。

以前这里有 ``BACKENDS = ("piper", "zipvoice", "api")`` 和一大串 ``if backend == ...``。
现在那些都变成了数据（见 :mod:`voice_loop.tts.spec` / :mod:`voice_loop.tts.registry`）：

    voice_loop/tts/providers/
      piper.py     zipvoice.py     deepseek.py     edge.py     http.py
                                    ↑ 加后端 = 在这里加一个文件

对外仍然导出老名字（``PiperTts`` / ``ZipVoiceTts`` / ``ApiTts`` / ``LazyTts`` / ``BACKENDS``），
因为脚本和自测一直在用；但它们只是转发，真正的选择逻辑已经不在这个文件里了。
"""

from __future__ import annotations

from .api_tts import ApiTts, api_tts_ready, voice_of
from .base import TtsEngine
from .lazy import LazyTts
from .piper_tts import PiperTts
from .registry import TtsRegistry
from .session import VoiceShell
from .spec import (
    CAP_CLONE, CAP_CLOUD, CAP_SPEED, CAP_STREAM, CAP_STYLE, CAP_VOICE_ID,
    FieldSpec, ProviderSpec, TtsConfigError, VoiceSpec,
)
from .zipvoice_tts import ZipVoiceTts, missing_files

__all__ = [
    "BACKENDS", "CAP_CLONE", "CAP_CLOUD", "CAP_SPEED", "CAP_STREAM", "CAP_STYLE",
    "CAP_VOICE_ID", "ApiTts", "FieldSpec", "LazyTts", "PiperTts", "ProviderSpec",
    "TtsConfigError", "TtsEngine", "TtsRegistry", "VoiceShell", "VoiceSpec",
    "ZipVoiceTts", "api_tts_ready", "create_tts", "missing_files", "registry_for",
    "voice_of",
]

#: 兼容用：真正有哪些后端以注册表为准（`TtsRegistry.ids()`）
BACKENDS = ("piper", "zipvoice", "deepseek", "edge", "http")
ZIPVOICE_HINT = "python scripts/download_models.py --only zipvoice"


def registry_for(settings, logger=None) -> TtsRegistry:
    """扫一遍 `providers/` 建注册表（结果不缓存：设置可能被换掉）。"""
    registry = TtsRegistry(settings, logger)
    registry.discover()
    return registry


def create_tts(settings, logger=None, lazy: bool = True,
               spec: VoiceSpec | str | dict | None = None) -> TtsEngine:
    """按配置创建 TTS。

    ``spec``：这次要用的声音（空 = 跟着 `[tts] provider`）。
    管线拿到角色后再调 ``shell.use(角色的 spec)``，不需要重建整个 TTS。
    ``lazy=True``（默认）返回按需加载的外壳：待唤醒状态完全不占内存。
    """
    registry = registry_for(settings, logger)
    if not registry.providers:
        raise TtsConfigError("没有发现任何 TTS 后端（voice_loop/tts/providers/ 是空的？）")
    parsed = VoiceSpec.parse(spec)
    # ★永远返回外壳★（lazy 只决定「要不要马上加载」）：外壳才是能换声线的那一层，
    # 早加载 / 晚加载只是内存策略，不该让调用方分成两种用法。
    shell = VoiceShell(settings, registry, parsed, logger)
    if not lazy:
        shell.load()
    return shell
