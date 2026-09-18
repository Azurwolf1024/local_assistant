"""SenseVoiceSmall（sherpa-onnx / ONNX Runtime int8）—— 中文快速路径。"""

from __future__ import annotations

import re
import time

import numpy as np

from .base import AsrResult

# SenseVoice 会在结果前面附加 <|zh|><|NEUTRAL|><|Speech|><|withitn|> 之类的标签
_TAG = re.compile(r"<\|[^|>]*\|>")
_WS = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


class SenseVoiceEngine:
    """低延迟中文 ASR：模型约 230 MB，CPU 上通常 10 倍速以上。"""

    name = "sensevoice"

    def __init__(
        self,
        model_path,
        tokens_path,
        num_threads: int = 4,
        use_itn: bool = True,
        language: str = "zh",
    ) -> None:
        import sherpa_onnx

        self.language = "" if language in ("auto", "") else language
        kwargs = dict(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=int(num_threads),
            use_itn=bool(use_itn),
            provider="cpu",
            debug=False,
        )
        if self.language:
            kwargs["language"] = self.language
        try:
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)
        except TypeError:
            # 兼容较老的 sherpa-onnx：不支持 language / debug 参数
            for key in ("language", "debug"):
                kwargs.pop(key, None)
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)
        self._last_meta: dict = {}

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> AsrResult:
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        duration = audio.size / float(sample_rate)
        t0 = time.perf_counter()
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, audio)
        self._rec.decode_stream(stream)
        latency = time.perf_counter() - t0
        text = strip_tags(getattr(stream.result, "text", ""))
        return AsrResult(
            text=text,
            engine=self.name,
            latency=latency,
            audio_seconds=duration,
        )
