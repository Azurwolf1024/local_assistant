"""语音识别：引擎（providers/）+ 策略（Hearing）+ 词库（lexicon）+ 评测（eval）。

分层（从下往上）：

    base.py           AsrResult / AsrEngine 契约（只认 audio → text）
    sensevoice.py     SenseVoiceSmall 实现（sherpa-onnx）
    whisper_ov.py     Whisper large-v3-turbo 实现（OpenVINO）
    sherpa_offline.py 任意 sherpa-onnx 离线模型（paraformer / transducer / ctc）
    quality.py        质量评分（择优 / 触发复核）
    spec.py           「怎么听」的数据描述 + 后端自描述
    registry.py       发现后端、配置副本、降级链、实例缓存
    lexicon.py        ★词库★：一份数据 → 引擎热词 / 提示词 / 事后纠正 / 唤醒匹配
    kws.py            ★流式关键词检测★（可选的两阶段唤醒）
    session.py        Hearing：管线唯一入口（策略 + 两级加载 + 择优）
    eval.py           评测：CER / 唤醒命中 / 首字延迟 / 误唤醒

★加引擎 = 在 ``providers/`` 里加一个文件★，别的地方不用改。
"""

from .base import AsrEngine, AsrResult
from .registry import AsrRegistry
from .sensevoice import SenseVoiceEngine
from .session import Hearing
from .spec import AsrSpec, ProviderSpec
from .whisper_ov import WhisperOpenVinoEngine

#: 老名字（见 asr/router.py 的说明）
AsrRouter = Hearing

__all__ = [
    "AsrEngine",
    "AsrRegistry",
    "AsrResult",
    "AsrRouter",
    "AsrSpec",
    "Hearing",
    "ProviderSpec",
    "SenseVoiceEngine",
    "WhisperOpenVinoEngine",
]
