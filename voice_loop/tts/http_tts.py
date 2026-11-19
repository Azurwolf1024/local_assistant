"""通用 HTTP TTS：**不写代码**就能接一个云端 API。

大多数云端 TTS 的形状是一样的：POST 一段 JSON → 拿回音频字节。差别只在
「地址 / 鉴权头 / 请求体字段名 / 音频编码」。把这几样变成配置，就不用为每家写一个文件：

```toml
[tts.providers.my-tts]
url = "https://api.example.com/v1/audio/speech"
headers = { Authorization = "Bearer {key}", Content-Type = "application/json" }
body = '{"input": "{text}", "voice": "{voice}", "response_format": "mp3"}'
audio_format = "mp3"          # pcm | wav | mp3 | ogg | flac
sample_rate = 24000           # 只有裸 PCM 才需要
key_env = "EXAMPLE_TTS_KEY"   # 有就用它填 {key}（绝不写进仓库）
```

模板占位符：``{text} {voice} {model} {lang} {key}``。
★密钥只能来自环境变量★：配置里写 ``{key}`` 时读 ``key_env``；
把 key 直接写进 config.toml 是不允许的（仓库是公开的，`config.local.toml` 例外）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import numpy as np

from ..settings import Settings
from .audio_decode import blocks, decode

PLACEHOLDERS = ("text", "voice", "model", "lang", "key")


class HttpTtsError(RuntimeError):
    pass


def render(value: Any, mapping: dict[str, str]) -> Any:
    """把模板里的 ``{text}`` 之类换成实际值（字典/列表递归处理）。"""
    if isinstance(value, dict):
        return {k: render(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, mapping) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for key, got in mapping.items():
        out = out.replace("{" + key + "}", str(got))
    return out


class HttpTts:
    """接口与其它引擎一致；具体接哪家全看配置。"""

    name = "http-tts"

    def __init__(self, settings: Settings, spec=None) -> None:
        self.settings = settings
        self.log = logging.getLogger("voice_loop")
        pid = str(getattr(spec, "provider", "") or "http")
        self.provider_id = pid
        opts = dict(getattr(settings.tts, "providers", {}) or {}).get(pid) or {}
        opts.update(dict(getattr(spec, "params", None) or {}))
        self.url = str(opts.get("url") or "").strip()
        self.method = str(opts.get("method") or "POST").upper()
        headers = opts.get("headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except ValueError:
                raise HttpTtsError(f"[tts.providers.{pid}] headers 不是合法 JSON") from None
        self.headers = dict(headers)
        self.body = opts.get("body")
        self.audio_format = str(opts.get("audio_format") or "mp3").lower()
        self.raw_rate = int(opts.get("sample_rate") or 24000)
        self.timeout = float(opts.get("timeout") or 60.0)
        self.key_env = str(opts.get("key_env") or "").strip()
        self.voice = str(getattr(spec, "voice_id", "") or opts.get("voice") or "").strip()
        self.model = str(getattr(spec, "model", "") or opts.get("model") or "").strip()
        self.lang = str(opts.get("lang") or "zh").strip()
        self._rate = self.raw_rate
        self._text_verifier = None
        self._fallback = None
        self._fallback_factory = None
        if not self.url:
            raise HttpTtsError(f"[tts.providers.{pid}] 没配 url")

    @property
    def sample_rate(self) -> int:
        return self._rate

    def set_text_verifier(self, fn) -> None:  # noqa: ARG002 - 接口兼容
        self._text_verifier = fn
        self.log.info(f"[tts:{self.provider_id}] 在线 TTS 不做合成回听重采")

    def set_fallback_factory(self, fn) -> None:
        """注册表注入：这一句彻底失败时用它拿一个本地引擎（不把嘴弄哑）。"""
        self._fallback_factory = fn

    # ------------------------------------------------------------------ 合成
    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        text = (text or "").strip()
        if not text:
            return
        yielded = False
        try:
            audio, fmt = self._request(text)
            rate, pcm = decode(audio, fmt, sample_rate=self.raw_rate)
            self._rate = rate or self.raw_rate
            for item in blocks(rate, pcm, 0.2):
                yielded = True
                yield item
        except Exception as exc:  # noqa: BLE001 - 一段失败不该带崩整轮
            if yielded:
                self.log.warning(f"[tts:{self.provider_id}] 合成中途失败（保留已播部分）：{exc}")
                return
            self.log.warning(f"[tts:{self.provider_id}] 合成失败，这一段改用本地：{exc}")
            engine = self._fallback_engine()
            if engine is None:
                return
            yield from engine.synth(text)

    def _mapping(self, text: str) -> dict[str, str]:
        key = ""
        if self.key_env:
            key = os.environ.get(self.key_env, "")
            if not key:
                self.log.warning(f"[tts:{self.provider_id}] 环境变量 {self.key_env} 是空的")
        return {"text": text, "voice": self.voice, "model": self.model,
                "lang": self.lang, "key": key}

    def _request(self, text: str) -> tuple[bytes, str]:
        import requests  # noqa: PLC0415 - 只有真合成时才需要

        mapping = self._mapping(text)
        headers = render(self.headers, mapping)
        body = self.body
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                pass                       # 不是 JSON 就原样当文本发
        payload = render(body, mapping)
        try:
            if self.method == "GET":
                resp = requests.get(self.url, headers=headers, params=payload,
                                    timeout=self.timeout)
            else:
                resp = requests.request(self.method, self.url, headers=headers,
                                        json=payload, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise HttpTtsError(f"请求 {self.url} 失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise HttpTtsError(f"{resp.status_code}：{(resp.text or '')[:200]}")
        fmt = str(resp.headers.get("Content-Type") or self.audio_format)
        return resp.content, fmt

    def _fallback_engine(self):
        if self._fallback is not None:
            return self._fallback
        if self._fallback_factory is None:
            return None
        try:
            self._fallback = self._fallback_factory()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[tts:{self.provider_id}] 连退路都拿不到：{exc}")
            self._fallback = None
        return self._fallback

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
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
        return {"engine": f"{self.provider_id}", "text_len": len(text),
                "audio_seconds": audio_s, "synth_seconds": elapsed,
                "rtf": elapsed / audio_s if audio_s else 0.0, "sample_rate": rate}

    def close(self) -> None:
        close = getattr(self._fallback, "close", None)
        if callable(close):
            close()
        self._fallback = None
