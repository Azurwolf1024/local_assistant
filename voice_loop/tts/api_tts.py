"""DeepSeek 官方 TTS 后端（可选）。

走官方文档里的**整段合成**接口（流式 WebSocket 版本这里不用）：

    POST https://api.deepseek.com/v1/tools/tts
    {"text": "...", "voice_id": "stella", "format": "pcm"}

官方返回：
- ``format = "pcm"``：24 kHz、16-bit、单声道裸 PCM（可以直接塞给扬声器）
- ``format = "wav"``：带文件头的 ``audio/wav``

★为什么默认用 ``pcm``★：扬声器要的就是裸 PCM，省掉一次 WAV 解析；
而且它是边合成边吐字节的，收够 0.2 秒就先播，不用等整句合成完。

★没有音色克隆 / 语速参数★（官方文档明确不支持），音色只有 ``stella`` / ``tide``。
失败时**自动退回 Piper**（只在这一段还没出声时），保证「不会把嘴弄哑」。
"""

from __future__ import annotations

import io
import logging
import sys
import time
import wave
from collections.abc import Iterator

import numpy as np
import requests

from ..llm import resolve_api_key
from ..settings import Settings

API_TTS_VOICES = ("stella", "tide")
API_TTS_SAMPLE_RATE = 24000
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/tts"


class TtsApiError(RuntimeError):
    pass


def endpoint(cfg) -> str:
    """TTS 地址：显式 ``tts_url`` 优先，否则由 ``base_url`` 推出来。"""
    url = str(getattr(cfg, "tts_url", "") or "").strip()
    if url:
        return url
    base = str(getattr(cfg, "base_url", "") or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/v1"):
        return f"{base}/tools/tts"
    return f"{base}/v1/tools/tts"


def voice_of(cfg) -> str:
    """当前音色；写错就退回官方默认的 ``stella``。"""
    want = str(getattr(cfg, "tts_voice", "") or "").strip().lower()
    return want if want in API_TTS_VOICES else API_TTS_VOICES[0]


def api_tts_ready(settings: Settings) -> tuple[bool, str]:
    """能不能用官方 TTS（不能就退回 Piper，绝不让助手变哑）。"""
    cfg = getattr(settings, "api", None)
    if cfg is None:
        return False, "没有 [api] 配置"
    if not bool(getattr(cfg, "enabled", True)):
        return False, "[api] enabled = false"
    if not bool(getattr(cfg, "tts_enabled", True)):
        return False, "[api] tts_enabled = false"
    if not resolve_api_key(cfg):
        path = str(getattr(cfg, "api_key_file", "") or "").strip()
        return False, (
            f"没有 API key（写 [api] api_key、放一个 api_key_file（现在是 {path or '没填'}），"
            f"或设置环境变量 {getattr(cfg, 'api_key_env', 'DEEPSEEK_API_KEY')}）"
        )
    if not endpoint(cfg):
        return False, "[api] tts_url 是空的"
    return True, ""


class ApiTts:
    """官方 TTS 引擎（接口与 PiperTts / ZipVoiceTts 一致）。"""

    name = "deepseek-tts"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.api
        self.log = logging.getLogger("voice_loop")
        block = max(0.05, float(getattr(self.cfg, "tts_block_seconds", 0.2) or 0.2))
        self._block_samples = max(128, int(API_TTS_SAMPLE_RATE * block))
        self._fallback = None

    # ------------------------------------------------------------------ 状态
    @property
    def sample_rate(self) -> int:
        return API_TTS_SAMPLE_RATE

    @property
    def voice(self) -> str:
        return voice_of(self.cfg)

    def _format(self) -> str:
        fmt = str(getattr(self.cfg, "tts_format", "pcm") or "pcm").strip().lower()
        return fmt if fmt in ("pcm", "wav") else "pcm"

    # ------------------------------------------------------------------- 合成
    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        text = (text or "").strip()
        if not text:
            return
        yielded = False
        try:
            for item in self._remote(text):
                yielded = True
                yield item
        except Exception as exc:  # noqa: BLE001 - TTS 失败不能把一轮对话带崩
            if yielded:
                self.log.warning(f"[tts:api] 合成中途失败（已播出的部分保留）：{exc}")
                return
            self.log.warning(f"[tts:api] 合成失败，这一段改用 Piper：{exc}")
            yield from self._fallback_engine().synth(text)

    def _remote(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        fmt = self._format()
        payload = {
            "text": text,
            "voice_id": self.voice,
            "format": fmt,
            "filter_markdown": True,
        }
        headers = {
            "Authorization": f"Bearer {resolve_api_key(self.cfg)}",
            "Content-Type": "application/json",
            "Accept": "audio/pcm" if fmt == "pcm" else "audio/wav",
        }
        timeout = float(getattr(self.cfg, "tts_timeout", 120.0) or 120.0)
        try:
            with requests.post(
                endpoint(self.cfg), json=payload, headers=headers,
                stream=True, timeout=(10, timeout),
            ) as resp:
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    raise TtsApiError(f"TTS {resp.status_code}：{body}")
                if fmt == "pcm":
                    yield from self._pcm_chunks(resp.iter_content(chunk_size=8192))
                else:
                    body = b"".join(resp.iter_content(chunk_size=16384))
                    yield from self._wav_audio(body)
        except TtsApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TtsApiError(f"调用 TTS 失败（{endpoint(self.cfg)}）：{exc}") from exc

    def _pcm_chunks(self, chunks) -> Iterator[tuple[int, np.ndarray]]:
        """裸 PCM：按 0.2 秒一块往外吐（收够就播，不等整句）。"""
        buf = bytearray()
        block_bytes = self._block_samples * 2
        for chunk in chunks:
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) >= block_bytes:
                take = len(buf) - (len(buf) % 2)
                data = bytes(buf[:take])
                del buf[:take]
                pcm = np.frombuffer(data, dtype="<i2").astype(np.int16, copy=False)
                if pcm.size:
                    yield API_TTS_SAMPLE_RATE, pcm
        take = len(buf) - (len(buf) % 2)
        if take:
            pcm = np.frombuffer(bytes(buf[:take]), dtype="<i2").astype(np.int16, copy=False)
            if pcm.size:
                yield API_TTS_SAMPLE_RATE, pcm

    def _wav_audio(self, body: bytes) -> Iterator[tuple[int, np.ndarray]]:
        """``format=wav``：整段取回后按 WAV 头解析（不是流式）。"""
        with wave.open(io.BytesIO(body), "rb") as wf:
            rate = int(wf.getframerate())
            channels = int(wf.getnchannels())
            width = int(wf.getsampwidth())
            raw = wf.readframes(wf.getnframes())
        if width == 2:
            pcm = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=False)
        elif width == 1:                       # 8-bit 无符号
            pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
        elif width == 4:                       # 32-bit，降采样到 16-bit
            pcm = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
        else:
            raise TtsApiError(f"不支持的 WAV 位宽：{width * 8} bit")
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
        step = max(128, int(rate * 0.2))
        for i in range(0, pcm.size, step):
            block = pcm[i : i + step]
            if block.size:
                yield rate, block

    # ------------------------------------------------------------- 自检/收尾
    def set_text_verifier(self, fn) -> None:
        """接口兼容：管线会把「合成回听」的校验器接给每个引擎。

        官方 TTS **不做**这层重采：每合成一次都要计费，而且它不像 ZipVoice 那样
        采样生成（不会偶发吞字）。记下来只是为了不报 AttributeError。
        """
        self._text_verifier = fn
        self.log.info("[tts:api] 官方 TTS 不做合成回听重采（按次计费）")

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        parts: list[np.ndarray] = []
        rate = self.sample_rate
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
            "engine": self.name,
            "text_len": len(text),
            "audio_seconds": audio_s,
            "synth_seconds": elapsed,
            "rtf": elapsed / audio_s if audio_s else 0.0,
            "sample_rate": rate,
        }

    def _fallback_engine(self):
        if self._fallback is None:
            from .piper_tts import PiperTts  # noqa: PLC0415 - 只有失败时才加载

            print("[tts:api] 退回 Piper 出声", file=sys.stderr, flush=True)
            self._fallback = PiperTts(self.settings)
        return self._fallback

    def close(self) -> None:
        if self._fallback is not None:
            close = getattr(self._fallback, "close", None)
            if callable(close):
                close()
            self._fallback = None
