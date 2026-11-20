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
        hr_lexicon: str = "",
        hr_dict_dir: str = "",
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
        # ★同音字替换（引擎侧词库）★：sherpa-onnx 的 hr_* 参数。配了就能把
        # 「白泽/白则」这类同音错字在**解码后立刻**改回来，比纯事后纠正准。
        if hr_lexicon:
            kwargs["hr_lexicon"] = str(hr_lexicon)
        if hr_dict_dir:
            kwargs["hr_dict_dir"] = str(hr_dict_dir)
        try:
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)
        except TypeError:
            # 兼容较老的 sherpa-onnx：不支持 language / debug（/ hr_*）参数
            for key in ("language", "debug", "hr_lexicon", "hr_dict_dir"):
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
