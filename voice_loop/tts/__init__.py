"""语音合成引擎集合。

- ``piper``    ：低延迟固定音色（RTF ~0.04、首段 0.13s）
- ``zipvoice`` ：零样本音色克隆（sherpa-onnx / CPU，RTF ~1.1~1.4），用参考音频把角色音色搬到中文上

两者接口一样，改 ``[tts] backend`` 就能切换。
**无论选哪个都不会哑**：克隆模型没装全、或参考音频不可用时会自动退回 Piper 出声。
"""

import sys

from .base import TtsEngine
from .lazy import LazyTts
from .piper_tts import PiperTts
from .zipvoice_tts import ZipVoiceTts, missing_files

__all__ = ["TtsEngine", "PiperTts", "ZipVoiceTts", "LazyTts", "create_tts", "missing_files"]

BACKENDS = ("piper", "zipvoice")
ZIPVOICE_HINT = "python scripts/download_models.py --only zipvoice"


def create_tts(settings, logger=None, lazy: bool = True) -> TtsEngine:
    """按配置创建 TTS 引擎。

    ``lazy=True``（默认）返回按需加载的封装：待唤醒状态完全不占内存，
    被唤醒时才加载，睡回去时释放（克隆模型 156 MB，这一条很重要）。
    """
    backend = (settings.tts.backend or "piper").strip().lower()
    if backend == "zipvoice":
        missing = missing_files(settings)
        if missing:  # 模型没装全就退回 Piper：宁可音色不对，也不能把嘴弄哑
            print(
                f"[tts] ZipVoice 模型没装全（缺 {missing[0]}），先用 Piper 出声。\n"
                f"      想用克隆音色就执行：{ZIPVOICE_HINT}",
                file=sys.stderr,
                flush=True,
            )
            backend = "piper"

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
