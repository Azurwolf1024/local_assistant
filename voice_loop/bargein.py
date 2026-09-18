"""播放中判断「麦克风里是用户在插话，还是我们自己的回声」。

为什么需要它：默认情况下播放语音时会把麦克风整段丢掉（否则会自己唤醒自己），
代价是你在它说话时插嘴它完全听不见。要做到「一说话就打断」，就得把两者分开。

难点在于麦克风同时听得到扬声器。这里不做正经的回声消除（太重），
而是拿**正在播出去的电平**当参考，估一个回声泄漏系数（能量比）：

    预计的回声能量 = 泄漏系数 × 参考能量
    只有麦克风能量明显高于这个值（再叠加一个绝对下限），才算你在说话

三个让它靠谱的细节：

1. **两边都按同一个滑窗算能量。** 一开始我拿「当前帧 RMS」比「参考的 250ms 峰值」，
   量纲不一致，泄漏系数会被系统性低估，于是放着自己的回声都能把自己打断。
2. **滑窗（默认 0.3 秒）顺便吸收了扬声器到麦克风的几十毫秒延迟**，不用单独对齐。
3. **第一次播放时先学一小段（默认 0.35 秒）。** 泄漏系数初值只能是猜的：
   猜高了自己说话会被当成插话，猜低了会漏掉真插话。学一小段再判断就稳了；
   而且这段时间就算真被打断也不会丢内容——触发前的音频有 1 秒滚动缓存兜着。

收敛用的是一快一慢：估高了要赶紧降（否则真插话打不断），估低了慢慢抬
（否则一句噪音就把门槛顶上去）。

已知局限
    扬声器音量很大、你又离麦克风很近时，回声和你说话差不多响，物理上就分不开。
    这种场合用耳机最省心（泄漏系数会自己降到接近 0，任何说话都能立刻打断）。
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
    """逐帧喂入麦克风与「此刻播出去的电平」，判断该不该打断。"""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger | None = None,
        frame_seconds: float | None = None,
    ) -> None:
        cfg = settings.bargein
        self.log = logger or logging.getLogger("voice_loop")
        self.min_seconds = max(0.05, float(cfg.min_seconds))
        self.leak_min = max(0.0, float(cfg.leak_min))
        self.leak_max = max(self.leak_min, float(cfg.leak_max))
        self.leak = min(self.leak_max, max(self.leak_min, float(cfg.leak_init)))
        if frame_seconds is None:
            frame_seconds = float(settings.audio.frame_size) / float(settings.audio.sample_rate)
        self.dt = max(0.001, float(frame_seconds))

        # 门槛用能量表示：margin 是幅度倍数，平方成能量倍数
        self.min_energy = max(0.0, float(cfg.min_rms)) ** 2
        self.margin_e = max(1.0, float(cfg.margin)) ** 2

        self.win = max(1, int(round(float(cfg.window_seconds) / self.dt)))
        self.learn_frames = max(0, int(round(float(cfg.learn_seconds) / self.dt)))

        self._mic_e: deque[float] = deque(maxlen=self.win)
        self._ref_e: deque[float] = deque(maxlen=self.win)
        self._run = 0.0
        self._learned = False       # 整个进程只学一次，之后一直复用这台的泄漏系数
        self._learn_left = 0
        self._learn_ratios: list[float] = []
        self._learn_ref_max = 0.0   # 学习期参考能量的峰值（用来跳过起音斜坡）
        self._frames = 0

    # ------------------------------------------------------------------ 生命周期
    def begin(self) -> None:
        """开始播放时调用。"""
        self._mic_e.clear()
        self._ref_e.clear()
        self._run = 0.0
        self._frames = 0
        if not self._learned:
            self._learn_left = self.learn_frames
            self._learn_ratios = []
            self._learn_ref_max = 0.0

    def end(self) -> None:
        self._mic_e.clear()
        self._ref_e.clear()
        self._run = 0.0

    def note_reference(self, level: float) -> None:
        """只更新参考电平，不做判断（流式生成阶段偶尔用）。"""
        self._ref_e.append(max(0.0, float(level)) ** 2)

    @property
    def leak_estimate(self) -> float:
        return self.leak

    @property
    def learning(self) -> bool:
        return self._learn_left > 0

    # ------------------------------------------------------------------ 判断
    def feed(self, frame: np.ndarray, reference: float | None = None) -> bool:
        """喂一帧麦克风音频。返回 True 表示「用户在插话，该打断」。"""
        self._frames += 1
        if reference is not None:
            self.note_reference(reference)
        self._mic_e.append(energy(frame))

        mic_e = sum(self._mic_e) / len(self._mic_e)
        ref_e = sum(self._ref_e) / len(self._ref_e) if self._ref_e else 0.0
        threshold = max(self.min_energy, self.leak * ref_e * self.margin_e)

        # 学习期：只校准泄漏系数，不判断
        if self._learn_left > 0:
            self._learn_left -= 1
            self._learn_ref_max = max(self._learn_ref_max, ref_e)
            # 只采信「参考已经足够响」的帧
            if ref_e > self.min_energy and ref_e >= 0.5 * self._learn_ref_max:
                self._learn_ratios.append(mic_e / ref_e)
            if self._learn_left == 0:
                self._learned = True
                if self._learn_ratios:
                    # 取中位数：开头几帧回声还没传到麦克风（比值接近 0），
                    # 取最小值会被它拉得一塔涂地，拿最小值直接导致自己打断自己。
                    self.leak = min(
                        self.leak_max, max(self.leak_min, float(np.median(self._learn_ratios)))
                    )
                self.log.debug(
                    f"打断检测学习完成：泄漏系数 = {self.leak:.4f}"
                    f"（{len(self._learn_ratios)} 个样本）"
                )
            return False

        if mic_e > threshold:
            self._run += self.dt
            if self._run >= self.min_seconds:
                self.log.info(
                    f"检测到插话：mic_energy={mic_e:.5f} 阈值={threshold:.5f}"
                    f"（参考={ref_e:.5f} 泄漏系数={self.leak:.3f}）"
                )
                return True
            # 可能是插话：泄漏系数几乎不动，免得把自己的门槛抬上去
            self._update_leak(mic_e, ref_e, rate=0.005)
        else:
            self._run = max(0.0, self._run - self.dt * 1.5)
            self._update_leak(mic_e, ref_e, rate=0.01)
        return False

    def _update_leak(
        self, mic_e: float, ref_e: float, *, fast: bool = False, rate: float = 0.005
    ) -> None:
        if ref_e <= self.min_energy:
            return          # 参考太小，比例估不出来
        ratio = mic_e / ref_e
        if fast or ratio < self.leak:
            # 估高了要立刻降下来（不然真插话打不断）
            r = 0.4
            self.leak = self.leak * (1 - r) + ratio * r
        else:
            # 估低了要慢慢抬：抬太快的话，你自己说话反而会把门槛顶高，越说越打不断
            self.leak = self.leak * (1 - rate) + ratio * rate
        self.leak = min(self.leak_max, max(self.leak_min, self.leak))
