"""Whisper large-v3-turbo（OpenVINO int8）—— 高精度路径。

本目录下的模型由
    optimum-cli export openvino --trust-remote-code --model openai/whisper-large-v3-turbo \
        --weight-format int8 --disable-stateful whisper-large-v3-turbo-int8-ov
导出，属于「非 stateful」格式，因此走 Optimum Intel 的 OVModelForSpeechSeq2Seq，
而不是 openvino-genai 的 WhisperPipeline（后者要求 stateful 导出）。
"""

from __future__ import annotations

import time

import numpy as np

from .base import AsrResult

MAX_WHISPER_SECONDS = 29.0  # Whisper 特征固定 30 秒窗，留一点余量


class WhisperOpenVinoEngine:
    name = "whisper"

    def __init__(
        self,
        model_dir,
        device: str = "CPU",
        language: str = "zh",
        num_threads: int = 0,
        word_timestamps: bool = False,
    ) -> None:
        import logging

        import openvino as ov
        import torch  # noqa: F401  (optimum 推理依赖)
        from optimum.intel.openvino import OVModelForSpeechSeq2Seq
        from transformers import AutoProcessor

        for noisy in ("transformers", "optimum", "optimum.intel"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

        self.device = device
        self.language = language if language not in ("auto", "") else None
        self.word_timestamps = word_timestamps
        self._processor = AutoProcessor.from_pretrained(str(model_dir))
        ov_config: dict = {"PERFORMANCE_HINT": "LATENCY"}
        if num_threads:
            ov_config["INFERENCE_NUM_THREADS"] = str(num_threads)
        self._model = OVModelForSpeechSeq2Seq.from_pretrained(
            str(model_dir),
            device=device,
            ov_config=ov_config,
        )
        self._supports_scores = True

    # ------------------------------------------------------------------ util
    def _decode(self, sequences) -> str:
        text = self._processor.batch_decode(sequences, skip_special_tokens=True)
        return text[0].strip() if text else ""

    def _avg_logprob(self, sequences, scores) -> float | None:
        """从 generate 的 scores 推算平均对数概率，用于置信度比较。"""
        try:
            import torch
            import torch.nn.functional as F

            if not scores:
                return None
            logits = torch.stack(list(scores), dim=1)  # (bs, T, V)
            length = logits.shape[1]
            tokens = sequences[:, -length:]
            logprobs = F.log_softmax(logits.float(), dim=-1)
            picked = logprobs.gather(-1, tokens.unsqueeze(-1)).squeeze(-1)
            eos = getattr(self._model.config, "eos_token_id", None)
            mask = torch.ones_like(tokens, dtype=torch.bool)
            if eos is not None:
                mask = tokens != eos
            if mask.sum().item() == 0:
                return None
            return float(picked[mask].mean().item())
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ api
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> AsrResult:
        import torch

        audio = np.ascontiguousarray(samples, dtype=np.float32)
        if sample_rate != 16000:
            raise ValueError("Whisper 只接受 16 kHz 音频")
        duration = audio.size / 16000.0
        if duration > MAX_WHISPER_SECONDS:
            audio = audio[: int(MAX_WHISPER_SECONDS * 16000)]

        features = self._processor(
            audio, sampling_rate=16000, return_tensors="pt"
        ).input_features

        generate_kwargs: dict = {
            "task": "transcribe",
            "return_timestamps": bool(self.word_timestamps),
        }
        if self.language:
            generate_kwargs["language"] = self.language

        t0 = time.perf_counter()
        avg_logprob: float | None = None
        text = ""
        with torch.no_grad():
            if self._supports_scores:
                try:
                    out = self._model.generate(
                        features,
                        return_dict_in_generate=True,
                        output_scores=True,
                        **generate_kwargs,
                    )
                    text = self._decode(out.sequences)
                    avg_logprob = self._avg_logprob(out.sequences, out.scores)
                    if not out.scores:
                        # 该 OpenVINO 导出不支持输出 scores，之后不再尝试
                        self._supports_scores = False
                except Exception:  # noqa: BLE001
                    # 该 OpenVINO 导出不支持输出 scores，退回普通生成
                    self._supports_scores = False
            if not text:
                sequences = self._model.generate(features, **generate_kwargs)
                text = self._decode(sequences)
        latency = time.perf_counter() - t0

        return AsrResult(
            text=text,
            engine=self.name,
            latency=latency,
            audio_seconds=duration,
            avg_logprob=avg_logprob,
        )
