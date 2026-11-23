"""Edge TTS 引擎：微软 Edge 的在线朗读接口（免费、不用 key）。

★它的意义是「验证解耦」★：这是一个以前完全不存在的后端 —— 只有这个文件 +
一个 `providers/edge.py` 声明 + 一段 config，pipeline / settings / 控制台都没为它改过。
如果哪天要换成 ElevenLabs / Fish / 火山，照抄这个文件的形状就行。

实现要点：
- 官方库 ``edge_tts`` 只有异步接口 → 用 :func:`..async_bridge.run_sync` 桥接；
- 返回的是 **MP3**（实测 24 kHz）→ 交给 :func:`..audio_decode.decode`；
- 整句拿回来再按 0.2 秒切块吐出去：句子本来就被管线切得很短，
  为了「首音早 200 ms」去自己解 MP3 帧不划算。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import numpy as np

from ..settings import Settings
from .async_bridge import run_sync
from .audio_decode import blocks, decode


class EdgeTtsError(RuntimeError):
    pass


class EdgeTts:
    """接口与 PiperTts / ZipVoiceTts / ApiTts 一致。"""

    name = "edge-tts"

    def __init__(self, settings: Settings, spec=None) -> None:
        self.settings = settings
        self.log = logging.getLogger("voice_loop")
        opts = dict(getattr(settings.tts, "providers", {}) or {}).get("edge") or {}
        opts.update(dict(getattr(spec, "params", None) or {}))
        self.voice = str(
            (getattr(spec, "voice_id", "") or opts.get("voice_id") or opts.get("voice")
             or "zh-CN-XiaoxiaoNeural")
        ).strip()
        self.rate = str(opts.get("rate") or "+0%")
        self.volume = str(opts.get("volume") or "+0%")
        self.pitch = str(opts.get("pitch") or "+0Hz")
        self.timeout = float(opts.get("timeout") or 30.0)
        self._block_seconds = float(opts.get("block_seconds") or 0.2)
        self._rate_hz = 24000
        self._text_verifier = None
        self._fallback = None
        self._fallback_factory = None

    def set_fallback_factory(self, fn) -> None:
        """注册表注入的退路工厂：这一句彻底失败时用降级链上的下一个引擎出声。"""
        self._fallback_factory = fn

    def _fallback_engine(self):
        if self._fallback is not None:
            return self._fallback
        if self._fallback_factory is None:
            return None
        try:
            self._fallback = self._fallback_factory()
        except Exception as exc:  # noqa: BLE001 - 连退路都拿不到就只记日志
            self.log.warning(f"[tts:edge] 连退路都拿不到：{exc}")
            self._fallback = None
        return self._fallback

    # ------------------------------------------------------------------ 状态
    @property
    def sample_rate(self) -> int:
        return self._rate_hz

    def set_text_verifier(self, fn) -> None:  # noqa: ARG002 - 接口兼容
        """云端不做「合成回听重采」：重采一次就多一次网络请求，而且不会吞字。"""
        self._text_verifier = fn
        self.log.info("[tts:edge] 在线 TTS 不做合成回听重采")

    # ------------------------------------------------------------------ 合成
    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        text = (text or "").strip()
        if not text:
            return
        t0 = time.perf_counter()
        yielded = False
        try:
            audio = self._fetch(text)
            if not audio:
                return
            rate, pcm = decode(audio, "mp3")
            self._rate_hz = rate or self._rate_hz
            self.log.debug(f"[tts:edge] {len(text)} 字 → {pcm.size / max(1, rate):.2f}s "
                           f"音频（{time.perf_counter() - t0:.2f}s）")
            for item in blocks(rate, pcm, self._block_seconds):
                yielded = True
                yield item
        except Exception as exc:  # noqa: BLE001 - 云端失败不该把一轮对话带崩
            if yielded:
                self.log.warning(f"[tts:edge] 合成中途失败（保留已播部分）：{exc}")
                return
            self.log.warning(f"[tts:edge] 合成失败，这一段改用本地：{exc}")
            engine = self._fallback_engine()
            if engine is None:
                return
            yield from engine.synth(text)

    def _fetch(self, text: str) -> bytes:
        try:
            import edge_tts  # noqa: PLC0415 - 没装这个包就不该在 import 时炸
        except ImportError as exc:
            raise EdgeTtsError("没装 edge-tts（pip install edge-tts）") from exc

        async def collect() -> bytes:
            comm = edge_tts.Communicate(text, self.voice, rate=self.rate,
                                        volume=self.volume, pitch=self.pitch)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk.get("data") or b"")
            return bytes(buf)

        try:
            return run_sync(collect(), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - 网络问题要变成一句人话
            raise EdgeTtsError(f"edge-tts 合成失败：{type(exc).__name__}: {exc}") from exc

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        parts: list[np.ndarray] = []
        rate = self._rate_hz
        for r, pcm in self.synth(text):
            rate = r
            parts.append(pcm)
        if not parts:
            return rate, np.zeros(0, dtype=np.int16)
        return rate, np.concatenate(parts)

    def close(self) -> None:
        close = getattr(self._fallback, "close", None)
        if callable(close):
            close()
        self._fallback = None

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        t0 = time.perf_counter()
        rate, pcm = self.synth_bytes(text)
        elapsed = time.perf_counter() - t0
        audio_s = pcm.size / float(rate) if rate else 0.0
        return {"engine": self.name, "text_len": len(text), "audio_seconds": audio_s,
                "synth_seconds": elapsed, "rtf": elapsed / audio_s if audio_s else 0.0,
                "sample_rate": rate}
