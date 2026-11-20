"""任意 sherpa-onnx **离线**模型（paraformer / transducer / CTC）。

为什么要这个通用引擎：本项目的"识别准确性"上限很大程度取决于**能不能换模型 +
能不能给模型喂词库**。SenseVoice 只支持同音字替换，而 transducer 系（zipformer）
原生支持 ``hotwords_file``（真正的热词加权）—— 有了这个 provider，换模型
不需要写代码，改 ``[asr] provider`` 和几个路径就行。

三种模型形态（``kind``）
    paraformer : 一个 model.onnx + tokens.txt（中文大模型，例如 paraformer-zh）
    transducer : encoder + decoder + joiner + tokens（zipformer；★支持 hotwords★）
    ctc        : 一个 model.onnx + tokens.txt（zipformer2-ctc / nemo-ctc）

★它是"按需"的★★：模型多大我不知道，所以不标 ``heavy``——
用的人自己决定放在主引擎还是复核位；真嫌大的话把 ``[asr.providers.sherpa]
num_threads`` 调小、或者干脆只在复核位用它。
"""

from __future__ import annotations

import time

import numpy as np

from .base import AsrResult

KINDS = ("paraformer", "transducer", "ctc")


class SherpaOfflineEngine:
    name = "sherpa"

    def __init__(
        self,
        kind: str = "paraformer",
        model: str = "",
        tokens: str = "",
        encoder: str = "",
        decoder: str = "",
        joiner: str = "",
        num_threads: int = 4,
        provider: str = "cpu",
        language: str = "zh",
        hotwords_file: str = "",
        hotwords_score: float = 1.5,
        modeling_unit: str = "cjkchar",
        bpe_vocab: str = "",
        rule_fsts: str = "",
    ) -> None:
        import sherpa_onnx

        kind = str(kind or "paraformer").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"sherpa 的 kind 只能是 {'/'.join(KINDS)}（现在是 {kind!r}）")
        self.kind = kind
        self.name = f"sherpa-{kind}"
        self.language = language
        common = dict(tokens=str(tokens), num_threads=int(num_threads),
                      provider=str(provider or "cpu"), debug=False)
        if rule_fsts:
            common["rule_fsts"] = str(rule_fsts)
        if kind == "paraformer":
            self._rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(model), **common)
        elif kind == "ctc":
            self._rec = sherpa_onnx.OfflineRecognizer.from_zipformer2_ctc(
                model=str(model), **common)
        else:
            kwargs = dict(common)
            if hotwords_file:
                # ★真正的热词加权★：只有 transducer 系支持
                kwargs.update(hotwords_file=str(hotwords_file),
                              hotwords_score=float(hotwords_score),
                              modeling_unit=str(modeling_unit or "cjkchar"))
                if bpe_vocab:
                    kwargs["bpe_vocab"] = str(bpe_vocab)
            self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(encoder), decoder=str(decoder), joiner=str(joiner), **kwargs)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> AsrResult:
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        duration = audio.size / float(sample_rate)
        t0 = time.perf_counter()
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, audio)
        self._rec.decode_stream(stream)
        latency = time.perf_counter() - t0
        text = str(getattr(stream.result, "text", "") or "").strip()
        return AsrResult(text=text, engine=self.name, latency=latency,
                         audio_seconds=duration)


__all__ = ["KINDS", "SherpaOfflineEngine"]
