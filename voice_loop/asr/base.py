"""ASR 引擎公共接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class AsrResult:
    text: str = ""
    engine: str = ""
    latency: float = 0.0          # 推理耗时（秒）
    audio_seconds: float = 0.0    # 音频时长
    score: float = 0.0            # 质量分（启发式，越高越可信）
    avg_logprob: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def rtf(self) -> float:
        """实时率：推理耗时 / 音频时长，越小越快。"""
        return self.latency / self.audio_seconds if self.audio_seconds > 0 else 0.0


@runtime_checkable
class AsrEngine(Protocol):
    name: str

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> AsrResult:
        ...
