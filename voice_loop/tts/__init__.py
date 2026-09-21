"""语音合成引擎集合。

- ``piper``    ：低延迟固定音色（RTF ~0.04），日常对话首选
- ``zipvoice`` ：零样本音色克隆（sherpa-onnx / CPU），用参考音频把音色搬到中文上

两者接口一样，改 ``[tts] backend`` 就能切换。
"""

from .base import TtsEngine
from .lazy import LazyTts
from .piper_tts import PiperTts
from .zipvoice_tts import ZipVoiceTts

__all__ = ["TtsEngine", "PiperTts", "ZipVoiceTts", "LazyTts", "create_tts"]

BACKENDS = ("piper", "zipvoice")


def create_tts(settings, logger=None, lazy: bool = True) -> TtsEngine:
    """按配置创建 TTS 引擎。

    ``lazy=True``（默认）返回按需加载的封装：待唤醒状态完全不占内存，
    被唤醒时才加载，睡回去时释放（克隆类模型几百 MB，这一条很重要）。
    """
    backend = (settings.tts.backend or "piper").strip().lower()
    if backend == "piper":
        engine_cls, desc = PiperTts, f"piper / {settings.tts.voice}"
    elif backend == "zipvoice":
        ref = str(getattr(settings.tts, "clone_audio", "") or "").strip()
        engine_cls, desc = ZipVoiceTts, f"zipvoice / 参考 {ref or '（未设）'}"
    else:
        raise ValueError(f"暂不支持 TTS 后端：{backend}（可选：{'、'.join(BACKENDS)}）")

    engine = LazyTts(settings, engine_cls, logger, name=backend) if lazy else engine_cls(settings)
    if logger:
        logger.info(f"TTS: {desc}" + ("（按需加载）" if lazy else ""))
    return engine
