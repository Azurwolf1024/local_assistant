"""Piper 语音合成（中文女声 zh_CN-huayan-medium）。

piper-tts >= 1.3 的 Python API：
    from piper import PiperVoice, SynthesisConfig
    voice = PiperVoice.load("zh_CN-huayan-medium.onnx")
    for chunk in voice.synthesize("你好", syn_config=...):
        chunk.sample_rate, chunk.audio_int16_array
"""

from __future__ import annotations

import dataclasses
import gc
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from ..settings import Settings
from ..text import ensure_terminal, inject_pauses


def _load_piper():
    """兼容 piper-tts 1.2（piper.voice）与 1.3+（piper）两种包结构。"""
    try:
        from piper import AudioChunk, PiperVoice, SynthesisConfig  # type: ignore
    except Exception:  # pragma: no cover
        from piper.voice import PiperVoice, SynthesisConfig  # type: ignore
        from piper.voice import AudioChunk  # type: ignore
    return PiperVoice, SynthesisConfig, AudioChunk


class PiperTts:
    name = "piper"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.tts
        PiperVoice, SynthesisConfig, _ = _load_piper()

        model_path, config_path = self._resolve_paths(settings)
        self.model_path = model_path
        self.config_path = config_path

        self._voice = PiperVoice.load(
            str(model_path),
            config_path=str(config_path) if config_path else None,
            use_cuda=bool(cfg.use_cuda),
        )
        self._syn = self._make_syn_config(SynthesisConfig, cfg)
        voice_cfg = getattr(self._voice, "config", None)
        self._rate = int(getattr(voice_cfg, "sample_rate", 22050) or 22050)

        self.inject_pauses = bool(getattr(cfg, "inject_pauses", False))
        silence_s = max(0.0, float(cfg.sentence_silence))
        self._silence = np.zeros(int(self._rate * silence_s), dtype=np.int16)
        self._synth_calls = 0

    # ------------------------------------------------------------------ 路径
    @staticmethod
    def _resolve_paths(settings: Settings) -> tuple[Path, Path | None]:
        cfg = settings.tts
        model = settings.resolve(cfg.model)
        if not model.exists():
            # 兜底：在 models/tts/piper 下找任意 onnx
            alt_dir = settings.resolve("models/tts/piper")
            found = sorted(alt_dir.glob("*.onnx")) if alt_dir.exists() else []
            if not found:
                raise FileNotFoundError(
                    f"找不到 Piper 语音模型：{model}\n"
                    f"请执行：python scripts/download_models.py --only piper"
                )
            print(f"[tts] 配置的语音 {model.name} 不存在，改用 {found[0].name}", file=sys.stderr)
            model = found[0]

        config = settings.resolve(cfg.config) if cfg.config else None
        if config is not None and not config.exists():
            sibling = model.with_suffix(model.suffix + ".json")
            if sibling.exists():
                config = sibling
            else:
                config = None
        return model, config

    # ------------------------------------------------------------- 合成参数
    @staticmethod
    def _make_syn_config(SynthesisConfig, cfg):
        """只传 piper 当前版本支持的字段，跨版本兼容。"""
        wanted = {
            "length_scale": float(cfg.length_scale),
            "noise_scale": float(cfg.noise_scale),
            "noise_w_scale": float(cfg.noise_w_scale),
            "volume": float(cfg.volume),
        }
        if dataclasses.is_dataclass(SynthesisConfig):
            supported = {f.name for f in dataclasses.fields(SynthesisConfig)}
            wanted = {k: v for k, v in wanted.items() if k in supported}
        try:
            return SynthesisConfig(**wanted)
        except TypeError:
            return SynthesisConfig()

    # ------------------------------------------------------------------- api
    @property
    def sample_rate(self) -> int:
        return self._rate

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        """逐块产出音频，第一个 chunk 通常在几十毫秒内返回。

        ``text`` 建议是一句（或几句）完整的话：Piper 的中文音色看不到标点，
        句读停顿靠模型自身的韵律，切得太碎会明显发平、发顿。
        """
        text = (text or "").strip()
        if not text:
            return
        if self.inject_pauses:
            text = inject_pauses(ensure_terminal(text))
        self._synth_calls += 1
        for chunk in self._voice.synthesize(text, syn_config=self._syn):
            pcm = getattr(chunk, "audio_int16_array", None)
            if pcm is None:  # 兼容旧版本
                pcm = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
            if pcm.size:
                yield int(chunk.sample_rate), pcm
        if self._silence.size:
            yield self._rate, self._silence

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        """一次性合成完整音频（用于导出 wav 或自检）。"""
        parts: list[np.ndarray] = []
        rate = self._rate
        for r, pcm in self.synth(text):
            rate = r
            parts.append(pcm)
        if not parts:
            return rate, np.zeros(0, dtype=np.int16)
        return rate, np.concatenate(parts)

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        t0 = time.perf_counter()
        rate, pcm = self.synth_bytes(text)
        elapsed = time.perf_counter() - t0
        audio_s = pcm.size / float(rate) if rate else 0.0
        return {
            "text_len": len(text),
            "audio_seconds": audio_s,
            "synth_seconds": elapsed,
            "rtf": elapsed / audio_s if audio_s else 0.0,
            "sample_rate": rate,
        }

    def close(self) -> None:
        """释放 onnxruntime 会话占用的内存（回到待唤醒状态时用）。"""
        self._voice = None
        gc.collect()


class LazyTts:
    """按需加载、可卸载的 Piper 封装，对外接口与 :class:`PiperTts` 一致。

    待唤醒时完全不存在于内存里；被唤醒后才加载，睡回去时再释放。
    """

    name = "piper"

    def __init__(self, settings: Settings, logger=None) -> None:
        self.settings = settings
        self.log = logger
        self._engine: PiperTts | None = None
        self._lock = threading.Lock()
        self.inject_pauses = bool(getattr(settings.tts, "inject_pauses", False))

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def load(self) -> PiperTts:
        with self._lock:
            if self._engine is None:
                t0 = time.perf_counter()
                self._engine = PiperTts(self.settings)
                if self.log:
                    self.log.info(
                        f"TTS 已加载：{self.settings.tts.voice}（{time.perf_counter() - t0:.1f}s）"
                    )
            return self._engine

    def unload(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
                if self.log:
                    self.log.info("TTS 已卸载")

    @property
    def sample_rate(self) -> int:
        return self.load().sample_rate

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        yield from self.load().synth(text)

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        return self.load().synth_bytes(text)

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        return self.load().benchmark(text)
