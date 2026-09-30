"""说话人向量（声纹）相似度：量「像不像她」的那把尺子。

★它不是合成模型★：不参与出声，只在工具里出现 ——

- ``scripts/spk_check.py``：一批 wav 的「两两相似度 / 对参考的相似度」；
- ``scripts/pick_voice_ref.py``：给每条素材加一列「像不像这个角色的整体音色」。

为什么需要它（2026-09-30）：本项目已有的尺子能量**音区**（``pitch.py``）、
**停顿/节奏**（``pacing.py``）、**沙沙声**（``ab_clone_model.py`` 的频段表），
唯独量不了用户最在意的那件事 —— **「这是不是同一个人在说」**。
于是换参考、换精度、换步数、重训模型，全都只能靠耳朵判，几次调整之间没有共同刻度。

模型：3D-Speaker CAM++ 中文版（16 kHz 单声道 → 固定维向量，27 MB，纯 CPU，
走 sherpa-onnx 的 ``SpeakerEmbeddingExtractor``，零新依赖）。
下载：``python scripts/download_models.py --only speaker``

★怎么用才算严谨★

1. 相似度是**相对量**：不同模型、不同时长之间不能横比，只跟同一套条件下的数字比；
2. 做 A/B 时**配对 + 多遍**（模型是采样的，单次数字会骗人 —— 工程日志 §18.2 那条教训
   在声纹上也一样成立）；
3. 判「像不像」要跟**同一个角色的素材基准（质心）**比，不要拿绝对阈值套所有人。
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np

from .pacing import resample        # 单一实现，别再抄一份

SAMPLE_RATE = 16000                 # 这个模型只吃 16 kHz
MIN_SECONDS = 0.6                   # 太短算不出稳定向量（短音频的声纹噪声很大）
MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
RELATIVE = f"models/speaker/{MODEL_NAME}"


# --------------------------------------------------------------------------- #
def model_path(settings: Any = None) -> Path:
    """声纹模型在哪：``[tts] speaker_model`` 优先（相对路径按项目根算）。"""
    rel = str(getattr(getattr(settings, "tts", None), "speaker_model", "") or RELATIVE)
    if settings is not None and hasattr(settings, "resolve"):
        return Path(settings.resolve(rel))
    return Path(rel)


def missing(settings: Any = None) -> str:
    """缺什么（空串 = 能用）。专门返回一句人话，给工具直接打印。"""
    path = model_path(settings)
    if not path.is_file():
        return (f"没有声纹模型：{path}\n"
                f"       下它：python scripts/download_models.py --only speaker")
    return ""


def read_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """读成 float32 单声道。读不了就返回 ``(空数组, 0)``，不抛。

    soundfile 优先（wav/flac/ogg 都能读），没有它再退回标准库 ``wave``
    （只支持 PCM 8/16/32 位，浮点 wav 会走 soundfile 那条）。
    """
    p = Path(path)
    try:
        import soundfile as sf  # noqa: PLC0415 - 让没装 soundfile 的环境也能用 wave 兜底

        data, rate = sf.read(str(p), dtype="float32", always_2d=True)
        if data.size == 0:
            return np.zeros(0, dtype=np.float32), 0
        return data.mean(axis=1).astype(np.float32), int(rate)
    except Exception:  # noqa: BLE001 - 换 wave 再试
        pass
    try:
        with wave.open(str(p), "rb") as w:
            rate = int(w.getframerate() or 0)
            width = int(w.getsampwidth())
            channels = max(1, int(w.getnchannels()))
            raw = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001
        return np.zeros(0, dtype=np.float32), 0
    if not raw or not rate:
        return np.zeros(0, dtype=np.float32), 0
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return np.zeros(0, dtype=np.float32), 0
    if channels > 1:
        x = x[: x.size // channels * channels].reshape(-1, channels).mean(axis=1)
    return x.astype(np.float32), rate


def cosine(a, b) -> float:
    """两个向量的余弦相似度。空/长度不等/零向量一律返回 ``0.0``（不抛）。"""
    va = np.asarray(a, dtype=np.float64).ravel()
    vb = np.asarray(b, dtype=np.float64).ravel()
    if va.size == 0 or va.size != vb.size:
        return 0.0
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def centroid(vecs) -> np.ndarray:
    """一组向量取平均并归一化 = 这个角色的「整体音色」基准。空输入返回空数组。"""
    got = [np.asarray(v, dtype=np.float64).ravel() for v in vecs
           if np.asarray(v).size]
    if not got:
        return np.zeros(0, dtype=np.float64)
    mean = np.mean(np.stack(got), axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm > 1e-12 else mean


# ★判读阈值是量出来的，不是拍的★（2026-09-30，CAM++ 中文版，凯尔希 6 条 × 阿米娅 6 条素材）：
#   同人素材两两：kaltsit 0.781~0.977（中位 0.865）；amiya 0.635~0.901（中位 0.755）
#   跨人素材两两：0.313~0.535（中位 0.440）
#   → 0.60 是个干净的分界（同人最低 0.635 > 0.60 > 跨人最高 0.535，两边各留约 0.1 余量）
#   ★只在这套模型 + 这种素材条件下成立★：换模型、换语言、极短音频都要重新量一遍。
SAME_SPEAKER = 0.60         # ≥ 它：判成同一个人
UNSURE_SPEAKER = 0.50       # ≥ 它：存疑（可能是同人的不同情绪/录音条件）；< 它：不像


def verdict(score: float) -> str:
    """把相似度翻译成一句话（阈值见上，量出来的）。"""
    if score >= SAME_SPEAKER:
        return "同人"
    if score >= UNSURE_SPEAKER:
        return "存疑"
    return "不像"


# --------------------------------------------------------------------------- #
class Embedder:
    """懒加载的声纹提取器（一个进程里建一次就够；模型 27 MB，首次加载几秒）。"""

    def __init__(self, path: str | Path | None = None, threads: int = 2):
        self.path = Path(path) if path else model_path()
        self.threads = max(1, int(threads))
        self._extractor = None
        self._failed = ""

    # ---- 加载 ----
    def ready(self) -> bool:
        """能不能用（模型在 + sherpa-onnx 建得起来）。失败原因记在 ``self._failed``。"""
        if self._extractor is not None:
            return True
        if self._failed:
            return False
        if not self.path.is_file():
            self._failed = f"模型文件不在：{self.path}"
            return False
        try:
            import sherpa_onnx  # noqa: PLC0415

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.path), num_threads=self.threads,
                debug=False, provider="cpu",
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        except Exception as exc:  # noqa: BLE001
            self._failed = f"{type(exc).__name__}: {exc}"
            return False
        return True

    @property
    def problem(self) -> str:
        """为什么用不了（空串 = 能用）。"""
        return "" if self.ready() else self._failed

    @property
    def dim(self) -> int:
        return int(self._extractor.dim) if self.ready() and self._extractor else 0

    # ---- 算向量 ----
    def embedding(self, pcm, rate: int) -> np.ndarray | None:
        """给一段波形（float32，任意采样率）→ 声纹向量；太短/失败返回 None。"""
        x = np.asarray(pcm, dtype=np.float32).ravel()
        if rate and int(rate) != SAMPLE_RATE:
            x = resample(x, int(rate), SAMPLE_RATE)
        if x.size < int(MIN_SECONDS * SAMPLE_RATE) or not self.ready():
            return None
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=x)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            return np.asarray(self._extractor.compute(stream), dtype=np.float64).ravel()
        except Exception:  # noqa: BLE001 - 单条音频算不出来不该让整批挂掉
            return None

    def embedding_file(self, path: str | Path) -> np.ndarray | None:
        pcm, rate = read_audio(path)
        if pcm.size == 0:
            return None
        return self.embedding(pcm, rate)


def load(settings: Any = None, threads: int = 2) -> Embedder | None:
    """拿一个能用的提取器；模型不在（或加载失败）返回 ``None``，调用方自己提示。"""
    threads = int(getattr(getattr(settings, "tts", None), "speaker_threads", 0) or threads)
    got = Embedder(model_path(settings), threads)
    return got if got.ready() else None


def similarity(a: str | Path, b: str | Path, settings: Any = None) -> float | None:
    """两个音频文件的音色相似度；算不出来返回 ``None``（别当成 0）。"""
    emb = load(settings)
    if emb is None:
        return None
    va, vb = emb.embedding_file(a), emb.embedding_file(b)
    if va is None or vb is None:
        return None
    return cosine(va, vb)
