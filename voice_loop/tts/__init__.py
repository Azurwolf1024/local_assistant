"""语音合成引擎集合。"""

from .base import TtsEngine
from .piper_tts import LazyTts, PiperTts

__all__ = ["TtsEngine", "PiperTts", "LazyTts", "create_tts"]


def create_tts(settings, logger=None, lazy: bool = True) -> TtsEngine:
    """按配置创建 TTS 引擎。

    ``lazy=True``（默认）返回按需加载的封装：待唤醒状态完全不占内存，
    被唤醒时才加载，睡回去时释放。
    """
    backend = (settings.tts.backend or "piper").lower()
    if backend != "piper":
        raise ValueError(f"暂不支持 TTS 后端：{backend}（当前仅支持 piper）")
    engine = LazyTts(settings, logger) if lazy else PiperTts(settings)
    if logger:
        logger.info(f"TTS: piper / {settings.tts.voice}" + ("（按需加载）" if lazy else ""))
    return engine
