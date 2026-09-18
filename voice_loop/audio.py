"""音频 I/O：麦克风采集、VAD 断句、扬声器播放。"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque

import numpy as np

try:  # sounddevice 是可选依赖，缺失时给出清晰提示
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None  # type: ignore[assignment]

from .settings import Settings

EPS = 1e-9


# --------------------------------------------------------------------------- #
# 设备工具
# --------------------------------------------------------------------------- #
def require_sounddevice() -> None:
    if sd is None:
        raise RuntimeError("未安装 sounddevice，请执行：pip install sounddevice")


def list_devices() -> str:
    require_sounddevice()
    lines = [f"sounddevice / PortAudio {sd.get_portaudio_version()[1]}"]
    for idx, dev in enumerate(sd.query_devices()):
        tag = []
        if dev["max_input_channels"] > 0:
            tag.append("IN")
        if dev["max_output_channels"] > 0:
            tag.append("OUT")
        lines.append(
            f"  [{idx:2d}] {'/'.join(tag):7s} {dev['name']}"
            f"  (in={dev['max_input_channels']}, out={dev['max_output_channels']},"
            f" default_sr={int(dev['default_samplerate'])})"
        )
    try:
        din, dout = sd.default.device
        lines.append(f"  默认输入设备 = {din}，默认输出设备 = {dout}")
    except Exception:
        pass
    return "\n".join(lines)


def resolve_device(spec: str | int | None, want_input: bool):
    """把配置里的设备串（编号或名称关键字）解析为 sounddevice 的设备编号。"""
    if spec is None or spec == "" or spec == -1:
        return None
    if isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if text.isdigit():
        return int(text)
    require_sounddevice()
    key = text.lower()
    for idx, dev in enumerate(sd.query_devices()):
        if key in dev["name"].lower():
            if want_input and dev["max_input_channels"] <= 0:
                continue
            if not want_input and dev["max_output_channels"] <= 0:
                continue
            return idx
    raise ValueError(f"找不到匹配 {'输入' if want_input else '输出'} 设备：{spec}")


# --------------------------------------------------------------------------- #
# 麦克风
# --------------------------------------------------------------------------- #
class MicReader:
    """16 kHz 单声道 float32 的连续采集流。"""

    def __init__(self, settings: Settings) -> None:
        require_sounddevice()
        self.cfg = settings.audio
        self.rate = int(self.cfg.sample_rate)
        self.frame_size = int(self.cfg.frame_size)
        self.gain = float(self.cfg.mic_gain)
        self.device = resolve_device(self.cfg.input_device, want_input=True)
        self._stream = None

    def __enter__(self) -> MicReader:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=self.rate,
            blocksize=self.frame_size,
            channels=1,
            dtype="float32",
            device=self.device,
            latency="low",
        )
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def read(self) -> np.ndarray:
        """阻塞读取一帧，返回 shape=(frame_size,) 的 float32。"""
        assert self._stream is not None, "MicReader 未打开"
        data, overflowed = self._stream.read(self.frame_size)
        if overflowed:
            print("[audio] 输入溢出，已丢弃一帧", file=sys.stderr)
        frame = np.asarray(data[:, 0], dtype=np.float32)
        if self.gain != 1.0:
            np.clip(frame * self.gain, -1.0, 1.0, out=frame)
        return frame

    def flush(self) -> int:
        """丢弃驱动缓冲区里堆积的陈旧音频。

        播放语音期间麦克风仍在采集，如果不清理，回到监听时会先"听到"刚播出去的
        内容（自我唤醒）。返回丢弃的采样点数。
        """
        if self._stream is None:
            return 0
        dropped = 0
        try:
            available = int(self._stream.read_available)
        except Exception:  # noqa: BLE001
            return 0
        while available > 0:
            n = min(available, self.frame_size * 8)
            try:
                self._stream.read(n)
            except Exception:  # noqa: BLE001
                break
            available -= n
            dropped += n
        return dropped


class MicRecorder:
    """手动开始/结束的录音器（push-to-talk 与文件模式使用）。"""

    def __init__(self, mic: MicReader) -> None:
        self.mic = mic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames: list[np.ndarray] = []

    def start(self) -> None:
        self.frames = []
        self._stop.clear()
        self.mic.open()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mic-record")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.frames.append(self.mic.read())

    def stop(self) -> np.ndarray:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.frames)


# --------------------------------------------------------------------------- #
# VAD 断句
# --------------------------------------------------------------------------- #
class BaseSegmenter:
    """逐帧喂入，语音停顿后吐出完整语音片段。"""

    def accept(self, frame: np.ndarray) -> np.ndarray | None:  # pragma: no cover
        raise NotImplementedError

    def flush(self) -> list[np.ndarray]:  # pragma: no cover
        return []

    def reset(self) -> None:
        pass


class SileroSegmenter(BaseSegmenter):
    """基于 sherpa-onnx 自带 Silero VAD 的断句器。"""

    def __init__(self, settings: Settings, min_silence: float | None = None) -> None:
        import sherpa_onnx

        cfg = settings.vad
        model = settings.resolve(cfg.model)
        if not model.exists():
            raise FileNotFoundError(f"缺少 Silero VAD 模型：{model}")

        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = str(model)
        vc.silero_vad.threshold = float(cfg.threshold)
        vc.silero_vad.min_silence_duration = float(
            min_silence if min_silence is not None else cfg.min_silence_duration
        )
        vc.silero_vad.min_speech_duration = float(cfg.min_speech_duration)
        vc.silero_vad.max_speech_duration = float(cfg.max_speech_duration)
        vc.silero_vad.window_size = int(settings.audio.frame_size)
        vc.sample_rate = int(settings.audio.sample_rate)
        self._vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=60)
        self._pending: list[np.ndarray] = []

    def _drain(self) -> None:
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            self._vad.pop()
            if samples.size:
                self._pending.append(samples)

    def accept(self, frame: np.ndarray) -> np.ndarray | None:
        self._vad.accept_waveform(frame)
        self._drain()
        if self._pending:
            return self._pending.pop(0)
        return None

    def flush(self) -> list[np.ndarray]:
        self._vad.flush()
        self._drain()
        out, self._pending = self._pending, []
        return out

    def reset(self) -> None:
        self._vad.reset()
        self._pending.clear()

    @property
    def speech_detected(self) -> bool:
        return bool(self._vad.is_speech_detected())


class EnergySegmenter(BaseSegmenter):
    """纯 numpy 能量 VAD，作为 Silero 缺失时的零依赖兜底。"""

    def __init__(self, settings: Settings, min_silence: float | None = None) -> None:
        cfg = settings.vad
        self.threshold = float(cfg.energy_threshold)
        self.rate = int(settings.audio.sample_rate)
        self.frame_size = int(settings.audio.frame_size)
        self.frame_ms = 1000.0 * self.frame_size / self.rate
        self.speech_need = max(2, int(cfg.min_speech_duration * 1000 / self.frame_ms))
        silence_s = min_silence if min_silence is not None else cfg.min_silence_duration
        self.silence_need = max(2, int(silence_s * 1000 / self.frame_ms))
        self.max_frames = max(1, int(cfg.max_speech_duration * 1000 / self.frame_ms))
        self.preroll_frames = max(1, int(200 / self.frame_ms))  # 保留 200ms 前导
        self.reset()

    def reset(self) -> None:
        self._pre: deque[np.ndarray] = deque(maxlen=self.preroll_frames)
        self._cur: list[np.ndarray] = []
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._noise = None

    def accept(self, frame: np.ndarray) -> np.ndarray | None:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2) + EPS))
        # 自适应噪声底噪
        if self._noise is None:
            self._noise = rms
        elif not self._in_speech:
            self._noise = 0.95 * self._noise + 0.05 * rms
        gate = max(self.threshold, self._noise * 3.0)
        loud = rms > gate

        if not self._in_speech:
            self._pre.append(frame)
            self._speech_run = self._speech_run + 1 if loud else 0
            if self._speech_run >= self.speech_need:
                self._in_speech = True
                self._cur = list(self._pre)
                self._pre.clear()
                self._speech_run = 0
                self._silence_run = 0
            return None

        self._cur.append(frame)
        self._silence_run = self._silence_run + 1 if not loud else 0
        if self._silence_run >= self.silence_need or len(self._cur) >= self.max_frames:
            utterance = np.concatenate(self._cur)
            self._cur = []
            self._in_speech = False
            self._silence_run = 0
            self._pre.clear()
            if utterance.size >= self.frame_size * self.speech_need:
                return utterance
        return None

    def flush(self) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        if self._cur:
            out.append(np.concatenate(self._cur))
        self.reset()
        return [x for x in out if x.size]

    @property
    def speech_detected(self) -> bool:
        return self._in_speech


def make_segmenter(
    settings: Settings,
    logger=None,
    min_silence: float | None = None,
    quiet: bool = False,
) -> BaseSegmenter:
    """按配置创建断句器，自动在 Silero / 能量 VAD 之间降级。

    ``min_silence`` 可以覆盖配置里的 ``min_silence_duration``，
    用来为「待唤醒」状态做一个更灵敏的断句器。
    """
    backend = (settings.vad.backend or "auto").lower()
    if backend in ("silero", "auto"):
        try:
            seg = SileroSegmenter(settings, min_silence=min_silence)
            if logger and not quiet:
                logger.info("VAD: Silero (sherpa-onnx)")
            return seg
        except Exception as exc:  # noqa: BLE001
            if backend == "silero":
                raise
            if not quiet:
                if logger:
                    logger.warning(f"Silero VAD 不可用（{exc}），降级为能量 VAD")
                else:
                    print(f"[audio] Silero VAD 不可用（{exc}），降级为能量 VAD", file=sys.stderr)
    if logger and not quiet:
        logger.info("VAD: energy")
    return EnergySegmenter(settings, min_silence=min_silence)


# --------------------------------------------------------------------------- #
# 离线切分（wav 文件转写用）
# --------------------------------------------------------------------------- #
def segment_audio(
    audio: np.ndarray,
    settings: Settings,
    max_seconds: float = 29.0,
    min_seconds: float = 0.12,
) -> list[np.ndarray]:
    """用 VAD 把长音频切成若干语音片段；切不动就按定长兜底。"""
    rate = int(settings.audio.sample_rate)
    frame = int(settings.audio.frame_size)
    data = np.ascontiguousarray(audio, dtype=np.float32)
    if data.size == 0:
        return []

    seg = make_segmenter(settings)
    segments: list[np.ndarray] = []
    for start in range(0, data.size, frame):
        block = data[start : start + frame]
        if block.size < frame:
            block = np.pad(block, (0, frame - block.size))
        utterance = seg.accept(block)
        if utterance is not None:
            segments.append(utterance)
    segments.extend(seg.flush())

    min_len = int(min_seconds * rate)
    segments = [s for s in segments if s.size >= min_len]

    if not segments:
        segments = [data]

    # 二次切分：任何超过窗口上限的片段再按定长拆开
    limit = int(max_seconds * rate)
    out: list[np.ndarray] = []
    for s in segments:
        if s.size <= limit:
            out.append(s)
        else:
            out.extend(s[i : i + limit] for i in range(0, s.size, limit))
    return out


# --------------------------------------------------------------------------- #
# 播放
# --------------------------------------------------------------------------- #
class Speaker:
    """后台线程 + 队列的流式播放器，支持随时打断。"""

    def __init__(self, settings: Settings) -> None:
        require_sounddevice()
        self.device = resolve_device(settings.audio.output_device, want_input=False)
        self.volume = float(settings.audio.playback_volume)
        self._queue: queue.Queue = queue.Queue()
        self._stream = None
        self._rate: int | None = None
        self._speaking = threading.Event()
        self._closing = False
        # 当前正在播的那一小段的 RMS（0~1）。语音打断要靠它区分
        # 「麦克风里是回声」还是「用户在插话」。
        self._level = 0.0
        self._thread = threading.Thread(target=self._worker, daemon=True, name="speaker")
        self._thread.start()

    # ------------------------------------------------------------------ api
    def submit(self, pcm: np.ndarray, rate: int) -> None:
        if pcm is None or len(pcm) == 0:
            return
        self._queue.put((np.asarray(pcm, dtype=np.int16), int(rate)))

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    @property
    def pending(self) -> int:
        return self._queue.unfinished_tasks

    @property
    def current_level(self) -> float:
        """正在播出去的内容的 RMS（0~1）；没在播就是 0。"""
        return self._level

    def join(self, timeout: float | None = None) -> bool:
        """等待队列播放完毕。"""
        deadline = None if timeout is None else time.time() + timeout
        while self._queue.unfinished_tasks > 0:
            if deadline and time.time() > deadline:
                return False
            time.sleep(0.02)
        return True

    def interrupt(self) -> None:
        """清空待播队列并立即停止当前播放。"""
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        stream = self._stream
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
            except Exception:
                pass
        self._level = 0.0
        self._speaking.clear()

    def close(self) -> None:
        self._closing = True
        self.interrupt()
        self._queue.put(None)
        self._thread.join(timeout=3.0)
        self._close_stream()

    # --------------------------------------------------------------- worker
    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._rate = None

    def _ensure_stream(self, rate: int) -> None:
        if self._stream is not None and self._rate == rate:
            return
        self._close_stream()
        self._stream = sd.OutputStream(
            samplerate=rate,
            channels=1,
            dtype="int16",
            device=self.device,
            latency="low",
        )
        self._stream.start()
        self._rate = rate

    def _write_blocks(self, pcm: np.ndarray, rate: int, block_seconds: float = 0.04) -> None:
        """分小块写出去，顺便维护「此刻在播多大声」。

        整块一次性 write 会阻塞到播完，中间拿不到电平；分成 40ms 的小块就
        能随时告诉打断检测器「现在播到哪儿、多大声」。
        """
        stream = self._stream
        step = max(256, int(rate * block_seconds))
        for i in range(0, len(pcm), step):
            block = pcm[i : i + step]
            try:
                self._level = float(
                    np.sqrt(np.mean(np.square(block.astype(np.float32) / 32768.0)))
                )
            except Exception:  # noqa: BLE001
                self._level = 0.0
            stream.write(block)
        self._level = 0.0

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            pcm, rate = item
            try:
                self._ensure_stream(rate)
                self._speaking.set()
                self._write_blocks(pcm, rate)
            except Exception as exc:  # noqa: BLE001
                print(f"[audio] 播放失败：{exc}", file=sys.stderr)
                self._close_stream()
            finally:
                self._level = 0.0
                self._speaking.clear()
                self._queue.task_done()
        self._close_stream()


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def save_wav(path, samples: np.ndarray, rate: int) -> None:
    """把 float32 [-1,1] 写成 16-bit PCM wav。"""
    import wave

    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm.tobytes())


def load_wav(path, target_rate: int = 16000) -> tuple[np.ndarray, int]:
    """读取 wav，必要时线性重采样到 target_rate，返回 (float32, rate)。"""
    import wave

    with wave.open(str(path), "rb") as f:
        channels = f.getnchannels()
        width = f.getsampwidth()
        rate = f.getframerate()
        raw = f.readframes(f.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的位深：{width * 8} bit")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != target_rate and data.size:
        n = int(round(data.size * target_rate / rate))
        data = np.interp(
            np.linspace(0.0, data.size - 1, n, dtype=np.float64),
            np.arange(data.size, dtype=np.float64),
            data,
        ).astype(np.float32)
        rate = target_rate
    return np.ascontiguousarray(data, dtype=np.float32), rate
