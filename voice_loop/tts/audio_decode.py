"""把「一段音频字节」变成「(采样率, int16 单声道 PCM)」。

为什么单独一个文件：云端 TTS 返回的编码五花八门（裸 PCM / WAV / MP3 / OGG），
而引擎只该关心「说什么」。「怎么解码」是一种反复出现的脏活，收在一处。

依赖策略：优先 ``soundfile``（libsndfile ≥ 1.1 就能解 MP3，本机 1.2.2 实测可用）；
没有 soundfile 时，WAV 用标准库 ``wave`` 兜底，其它格式直接报清楚缺什么。
"""

from __future__ import annotations

import io
import wave

import numpy as np

RAW_FORMATS = ("pcm", "raw", "s16le", "pcm_s16le", "pcm16")
WAV_FORMATS = ("wav", "wave", "audio/wav")
SOUNDFILE_FORMATS = ("mp3", "mpeg", "ogg", "oga", "opus", "flac", "m4a", "aac")


class AudioDecodeError(RuntimeError):
    pass


def decode(data: bytes, fmt: str = "", *, sample_rate: int = 0) -> tuple[int, np.ndarray]:
    """按 ``fmt`` 解码成 int16 单声道。``sample_rate`` 只对裸 PCM 有意义。"""
    if not data:
        return int(sample_rate or 0), np.zeros(0, dtype=np.int16)
    kind = str(fmt or "").strip().lower()
    kind = kind.split(";")[0] if kind else ""
    if kind in RAW_FORMATS:
        rate = int(sample_rate or 24000)
        even = len(data) - (len(data) % 2)
        pcm = np.frombuffer(data[:even], dtype="<i2").astype(np.int16, copy=False)
        return rate, pcm
    if kind in WAV_FORMATS or (not kind and data[:4] == b"RIFF"):
        return _decode_wav(data)
    if kind in SOUNDFILE_FORMATS:
        return _decode_soundfile(data, kind)
    # 没写格式：先当 WAV 试（有 RIFF 头就是），不行再交给 soundfile 嗅探
    if data[:4] == b"RIFF":
        return _decode_wav(data)
    return _decode_soundfile(data, "?")


def _decode_wav(data: bytes) -> tuple[int, np.ndarray]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        rate = int(wf.getframerate())
        channels = int(wf.getnchannels())
        width = int(wf.getsampwidth())
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=False)
    elif width == 1:                                   # 8-bit 无符号
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
    elif width == 4:                                   # 32-bit → 16-bit
        pcm = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    else:
        raise AudioDecodeError(f"不支持的 WAV 位宽：{width * 8} bit")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return rate, pcm


def _decode_soundfile(data: bytes, kind: str) -> tuple[int, np.ndarray]:
    try:
        import soundfile as sf  # noqa: PLC0415 - 只有云端后端才用得上
    except ImportError as exc:
        raise AudioDecodeError(
            f"{kind or '这个'} 格式需要 soundfile（pip install soundfile）"
        ) from exc
    try:
        pcm, rate = sf.read(io.BytesIO(data), dtype="int16", always_2d=False)
    except Exception as exc:  # noqa: BLE001 - libsndfile 的报错信息很杂
        raise AudioDecodeError(f"解码 {kind or '未知格式'} 失败：{exc}") from exc
    array = np.asarray(pcm)
    if array.ndim > 1:
        array = array.mean(axis=1)
    return int(rate), array.astype(np.int16, copy=False)


def blocks(rate: int, pcm: np.ndarray, seconds: float = 0.2):
    """把整段 PCM 切成小块往外吐，形状就是引擎的契约：``(采样率, pcm)``。"""
    if pcm.size == 0:
        return
    step = max(128, int(int(rate or 24000) * max(0.05, seconds)))
    for i in range(0, pcm.size, step):
        chunk = pcm[i : i + step]
        if chunk.size:
            yield rate, chunk


__all__ = ["AudioDecodeError", "blocks", "decode"]
