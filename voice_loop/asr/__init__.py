"""语音识别引擎集合。"""

from .base import AsrEngine, AsrResult
from .router import AsrRouter
from .sensevoice import SenseVoiceEngine
from .whisper_ov import WhisperOpenVinoEngine

__all__ = [
    "AsrEngine",
    "AsrResult",
    "AsrRouter",
    "SenseVoiceEngine",
    "WhisperOpenVinoEngine",
]
