"""播放中判断「麦克风里是用户在插话，还是我们自己的回声」。

为什么需要它：默认情况下播放语音时会把麦克风整段丢掉（否则会自己唤醒自己），
代价是你在它说话时插嘴它完全听不见。要做到「一说话就打断」，就得把两者分开。

## 为什不拿「播出去的电平」当参考（试过，不行）

最自然的想法是估一个回声泄漏系数：`预计回声 = 泄漏系数 × 在播出去的能量`。
但在本机实测（2026-09，外放一段话、人不出声，逐帧录 1004 帧）不成立：

    mic_e / ref_e： p10=0.0030  p50=0.0184  p90=0.0835  p99=0.43  p100=1.01

麦克风里响的其实是 **50~150ms 之前** 写出去的那段，两个 0.3 秒滑窗的内容
并不总是对得上（一个在唱元音、另一个正好是停顿）；参考很轻的时候麦克风里
只剩底噪，比值更是直接飞起来。一个常量系数根本无法涵盖这种波动：
取中位数会低估到只剩真实回声的 1/3（于是自己把自己打断、套娃起来），
取高分位又会把真插话一起挡掉。加上峰值保持、高分位、滑动修正都试过，
离线回放录下的真人数据：**回声单独回放仍然误触发 41 次**。

## 现在用的办法：只听麦克风自己的「回声基准」

不去建模参考，而是直接看麦克风自己：

1. **开头先校准 ``seed_seconds`` 秒**（默认 1 秒）：取麦克风窗口能量的**峰值**
   当基准。这段时间的麦克风里要么是回声、要么是静音，都不会是你在说话
   （刚开口就插话大概会晚一小下才生效，但不会丢内容——触发前的音频有滚动缓存）。
2. **之后取低于门槛那部分帧的 ``base_percentile`` 分位**（默认 p90）慢慢跟随。
   用 p90 而不是平均值：回声本身波动很大，门槛必须压过它自己的高分位。
3. **高于门槛的帧先不算作回声**（否则你一说话，基准就被你自己顶上去，
   越说越打不断），它们只用来累计时——连续超过 ``min_seconds`` 才算插话。

这样门槛天然带上了「麦克风自己听到的回声有多大」，不依赖两个滑窗对得上，
音量变化、换扬声器、换房间都自己跟得上。本机实测：录下的真人回声回放
**一个都没误触发**，而叠上合成人声（幅度大于回声 4 倍以上）能稳定打断。

已知局限
    扬声器音量很大、你又离麦克风很近时，回声和你说话差不多响，物理上就分不开。
    这种场合用耳机最省心（基准会跟到底噪附近，任何说话都能立刻打断）；
    外放时把 ``margin`` 调小到 1.3 会更灵敏，代价是更容易误触发。
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

from .settings import Settings


def rms(frame: np.ndarray) -> float:
    if frame is None or len(frame) == 0:
        return 0.0
    data = np.asarray(frame, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(data))))


def energy(frame: np.ndarray) -> float:
    """均方值（RMS 的平方），比 RMS 更好统一量纲。"""
    e = rms(frame)
    return e * e


class BargeInDetector:
    """只看麦克风自己的回声基准，判断该不该打断。

    参考电平只用来写日志（方便对着 sessions/listen.log 回查），不参与判定。
    """

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger | None = None,
        frame_seconds: float | None = None,
    ) -> None:
        cfg = settings.bargein
        self.log = logger or logging.getLogger("voice_loop")
        self.min_seconds = max(0.05, float(cfg.min_seconds))
        if frame_seconds is None:
            frame_seconds = float(settings.audio.frame_size) / float(settings.audio.sample_rate)
        self.dt = max(0.001, float(frame_seconds))

        # 门槛用能量表示：margin 是幅度倍数，平方成能量倍数
        self.min_energy = max(0.0, float(cfg.min_rms)) ** 2
        self.margin_e = max(1.0, float(cfg.margin)) ** 2

        self.win = max(1, int(round(float(cfg.window_seconds) / self.dt)))
        self.seed_frames = max(1, int(round(float(cfg.seed_seconds) / self.dt)))
        self.base_frames = max(self.win, int(round(float(cfg.base_seconds) / self.dt)))
        self.base_percentile = min(100.0, max(50.0, float(cfg.base_percentile)))
        self.base_track = min(1.0, max(0.05, float(cfg.base_track)))
        self.base_min_samples = 20      # 样本太少时分位数没意义

        self.baseline = 0.0             # 麦克风里「回声 + 底噪」的基准（能量）
        self.calibrating = False
        self._mic_e: deque[float] = deque(maxlen=self.win)
        self._below: deque[float] = deque(maxlen=self.base_frames)
        self._seed_left = 0
        self._run = 0.0
        self._frames = 0
        # 只用于日志
        self._ref_e = 0.0
        self._ref_peak = 0.0

    # ------------------------------------------------------------------ 生命周期
    def begin(self) -> None:
        """开始播放时调用。每轮都重新校准：音量/设备可能变了。"""
        self._mic_e.clear()
        self._below.clear()
        self.baseline = 0.0
        self._seed_left = self.seed_frames
        self.calibrating = True
        self._run = 0.0
        self._frames = 0
        self._ref_e = 0.0
        self._ref_peak = 0.0

    def end(self) -> None:
        """一轮播放结束。基准保留着，下次 ``begin()`` 会重新校准。"""
        self._mic_e.clear()
        self._below.clear()
        self._seed_left = 0
        self.calibrating = False
        self._run = 0.0

    def note_reference(self, level: float) -> None:
        """记下此刻播出去的电平（只用于日志）。"""
        self._ref_e = max(0.0, float(level)) ** 2
        self._ref_peak = max(self._ref_peak, self._ref_e)

    @property
    def learning(self) -> bool:
        """还在校准期。"""
        return self.calibrating

    @property
    def threshold(self) -> float:
        return max(self.min_energy, self.baseline * self.margin_e)

    # ------------------------------------------------------------------ 判断
    def feed(self, frame: np.ndarray, reference: float | None = None) -> bool:
        """喂一帧麦克风音频。返回 True 表示「用户在插话，该打断」。"""
        self._frames += 1
        if reference is not None:
            self.note_reference(reference)
        self._mic_e.append(energy(frame))
        if len(self._mic_e) < self.win:
            return False        # 滑窗还没满，窗口能量没意义

        mic_e = sum(self._mic_e) / len(self._mic_e)

        # 校准期：取峰值。此刻麦克风里要么是回声要么是静音，不会是你说话
        if self._seed_left > 0:
            self._seed_left -= 1
            self.baseline = max(self.baseline, mic_e)
            if self._seed_left == 0:
                self.calibrating = False
                self.log.info(
                    f"打断检测已校准回声基准：mic_energy={self.baseline:.5f}"
                    f"（相当于 RMS {np.sqrt(self.baseline):.4f}）"
                )
            return False

        threshold = self.threshold
        if mic_e > threshold:
            # 可能是插话，也可能是它自己变响了。先攒时长，不抬高基准，
            # 否则你一说话基准就被自己顶上去，越说越打不断
            self._run += self.dt
            if self._run >= self.min_seconds:
                self.log.info(
                    f"检测到插话：mic_energy={mic_e:.5f} 阈值={threshold:.5f}"
                    f"（基准={self.baseline:.5f} 参考={self._ref_e:.5f}）"
                )
                return True
            return False

        self._run = max(0.0, self._run - self.dt * 1.5)
        self._below.append(mic_e)
        if len(self._below) >= self.base_min_samples and self._frames % 4 == 0:
            # 慢慢跟：基准只能从「看起来是回声」的帧里学，不能被人声带跑
            target = float(np.percentile(self._below, self.base_percentile))
            self.baseline = self.baseline * (1 - self.base_track) + target * self.base_track
        return False
