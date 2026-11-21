"""TTS 层对外只留一个入口：:func:`create_tts`。

现在那些都变成了数据（见 :mod:`voice_loop.tts.spec` / :mod:`voice_loop.tts.registry`）：

    voice_loop/tts/providers/
      piper.py     zipvoice.py     deepseek.py     edge.py     http.py
                                    ↑ 加后端 = 在这里加一个文件

★这个 `__init__` 故意「什么都不重」★（2026-11-21 改的）
    以前它在顶层 `from .api_tts import …` / `from .piper_tts import …`，
    于是**只要有人 `import voice_loop.tts.spec`（比如读人格卡校验 TTS 字段），
    就会连带拉进 numpy / sherpa_onnx / onnxruntime**。语音服务无所谓，
    但控制台 exe 把那堆重库全排掉了（见 scripts/make_console_exe.py）——
    结果就是 exe 里**角色列表整个空掉**：`CharacterRegistry.load()` 抛
    `ModuleNotFoundError: No module named 'numpy'`，控制台又把它当成「没有角色」。
    现在顶层只 import `spec`（纯 dataclass/校验，几毫秒），引擎类按需取（PEP 562），
    `from voice_loop.tts import PiperTts` 这种老写法照样能用。
"""

from __future__ import annotations

from typing import Any

from .spec import (
    CAP_CLONE, CAP_CLOUD, CAP_SPEED, CAP_STREAM, CAP_STYLE, CAP_VOICE_ID,
    FieldSpec, ProviderSpec, TtsConfigError, VoiceSpec,
)

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

#: 名字 → (子模块, 属性名)。不在这里的都当不存在。
_LAZY = {
    "ApiTts": (".api_tts", "ApiTts"),
    "api_tts_ready": (".api_tts", "api_tts_ready"),
    "voice_of": (".api_tts", "voice_of"),
    "PiperTts": (".piper_tts", "PiperTts"),
    "ZipVoiceTts": (".zipvoice_tts", "ZipVoiceTts"),
    "missing_files": (".zipvoice_tts", "missing_files"),
    "TtsEngine": (".base", "TtsEngine"),
    "LazyTts": (".lazy", "LazyTts"),
    "VoiceShell": (".session", "VoiceShell"),
    "TtsRegistry": (".registry", "TtsRegistry"),
}


def __getattr__(name: str) -> Any:
    """PEP 562：用到哪个引擎才 import 哪个（`from voice_loop.tts import PiperTts` 仍然可用）。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value          # 缓存一下，下次不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


def registry_for(settings, logger=None) -> TtsRegistry:
    """扫一遍 `providers/` 建注册表（结果不缓存：设置可能被换掉）。"""
    from .registry import TtsRegistry  # noqa: PLC0415 - 轻，但仍然按需

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
    from .session import VoiceShell  # noqa: PLC0415 - 拖 numpy，必须按需

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
