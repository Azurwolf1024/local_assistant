"""TTS 引擎公共接口。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class TtsEngine(Protocol):
    name: str

    @property
    def sample_rate(self) -> int:
        ...

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        """把文本合成为 (采样率, int16 单声道 PCM) 片段流。"""
        ...
