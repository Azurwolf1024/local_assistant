"""掐掉合成音频里模型自己塞的死静音。

为什么需要它（2026-09-21 实测，ZipVoice distill int8 + 中文参考音频）：

* 模型的**发音速率本来就稳**——8 句实测 5.88~7.48 字/有声秒，相对参考音频
  0.91~1.16×，没有"忽快忽慢"；
* 真正不稳的是**每段音频开头纯数字静音的长度**：0.58 / 0.99 / 1.04 / 1.14 /
  1.18 / 1.25 / 1.32 / 1.48 秒，而参考音频自己只有 0.04 秒；
* 于是听感变成"话本身说得挺稳，但两句之间被糊了一秒死气"→ 含白语速
  3.04~3.87 字/秒（长句/短句 = 1.27×），并且首段出声白等 0.5~1.5 秒。

这里只做两件事：掐掉开头的死静音、收掉结尾多余的静音。
**默认不动句内停顿**（`max_pause_ms=0`），要压长停顿再显式打开。

裁剪只在「按句返回的整段音频」上做（sherpa ZipVoice 一次 `generate` 回调
只回一句话的完整音频，实测确认），所以不会切到半句话。
"""

from __future__ import annotations

import numpy as np

FRAME_MS = 10          # 判有声/无声的窗长
THRESHOLD = 0.05       # 峰值 RMS 的这个比例以下算静音
DEFAULT_LEAD_MS = 40   # 开头保留的静音（不是 0，免得第一个字被啃掉）
DEFAULT_TAIL_MS = 80   # 结尾保留的静音
DEFAULT_MAX_PAUSE_MS = 0   # 0 = 不压缩句内停顿
DEFAULT_MIN_PAUSE_MS = 260  # 压停顿时保留的下限
DEFAULT_MIN_GAP_MS = 0      # 0 = 不拉长过短的停顿；>0 = 句内停顿短于它就拉到它
# ★只拉长「本来就是停顿」的空隙★：中文里字与字之间有 10~40ms 的自然音渡
# （塞音成阻、擦音弱段），把那些也撑成 240ms 会让每个字之间都垫一段等长静音，
# 听感就是「一顿一顿」——实测一句 11 个空隙里有 8 个是这种微空隙，
# 当时把 240ms 无差别套上去 = 整句多出 1.84 秒死气。所以低于这个门槛的一律不动。
DEFAULT_MIN_GAP_FLOOR_MS = 60


def _to_int16(pcm) -> np.ndarray:
    x = np.asarray(pcm)
    if x.dtype == np.int16:
        return x
    return (np.clip(x.astype(np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)


def _active_frames(x: np.ndarray, rate: int, frame_ms: int, threshold: float) -> np.ndarray:
    """返回「有声帧」的布尔数组（10ms 一帧，按 RMS 判）。"""
    hop = max(1, int(rate * frame_ms / 1000))
    n = x.size // hop
    if n < 1:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt((x[: n * hop].astype(np.float32).reshape(n, hop) ** 2).mean(axis=1))
    return rms > max(float(rms.max()) * threshold, 1e-4)


def trim_silence(
    pcm,
    rate: int,
    *,
    lead_ms: int = DEFAULT_LEAD_MS,
    tail_ms: int = DEFAULT_TAIL_MS,
    max_pause_ms: int = DEFAULT_MAX_PAUSE_MS,
    min_pause_ms: int = DEFAULT_MIN_PAUSE_MS,
    min_gap_ms: int = DEFAULT_MIN_GAP_MS,
    min_gap_floor_ms: int = DEFAULT_MIN_GAP_FLOOR_MS,
    frame_ms: int = FRAME_MS,
    threshold: float = THRESHOLD,
) -> np.ndarray:
    """掐首尾死静音 + 调句内停顿，返回新的 int16 数组。

    ``min_gap_ms`` 是**反方向**的旋钮：句内停顿短于它就**拉长**到它。
    实测（微调后的凯尔希）：「我在，博士。」的逗号停顿只有 60ms，而人类在逗号要停
    200~400ms —— 听起来就是「太赶」。拉长它不改音高（只是插静音），比变速安全。

    ★但它有个门槛 ``min_gap_floor_ms``★：只有**本来就 ≥ 门槛**的空隙才会被拉长。
    中文里字与字之间有 10~40ms 的自然音渡，无差别撑成 240ms 会让每个字都垫一段
    等长静音（实测一句 11 个空隙里 8 个是这种），整句听起来就是「一顿一顿、卡顿」。
    长句里本来就 300ms+ 的停顿也不动。

    保守原则：**任何异常输入都原样返回**（宁可多一秒静音，也不能把话剪没）。
    另外开头/结尾的补白不会超过原本就有的静音长度——所以对「本来就没有静音」
    的音频是恒等变换，重复调用也不会越来越短。
    """
    x = _to_int16(pcm)
    if x.size == 0 or rate <= 0 or frame_ms <= 0:
        return np.ascontiguousarray(x)
    rate = int(rate)
    hop = max(1, int(rate * frame_ms / 1000))
    if x.size < hop * 4:
        return np.ascontiguousarray(x)

    active = _active_frames(x, rate, frame_ms, threshold)
    idx = np.nonzero(active)[0]
    if idx.size == 0:  # 整段没声：不动它（多半是上游出错了，别掩盖）
        return np.ascontiguousarray(x)

    first, last = int(idx[0]), int(idx[-1])
    head_ms = first * frame_ms
    tail_ms_have = (active.size - 1 - last) * frame_ms
    # 补白只能取「本来就有这么多静音」和「设定值」里的小者 → 幂等
    head_keep = int(rate * min(lead_ms, head_ms) / 1000)
    tail_keep = int(rate * min(tail_ms, tail_ms_have) / 1000)

    pieces: list[np.ndarray] = []
    i = first
    while i <= last:
        if active[i]:
            j = i
            while j <= last and active[j]:
                j += 1
            pieces.append(x[i * hop : j * hop])
            i = j
        else:
            j = i
            while j <= last and not active[j]:
                j += 1
            gap_ms = (j - i) * frame_ms
            if max_pause_ms > 0 and gap_ms > max_pause_ms:
                keep_ms = max(min_pause_ms, 0)
                pieces.append(np.zeros(int(rate * keep_ms / 1000), dtype=np.int16))
            elif min_gap_ms > 0 and min_gap_floor_ms <= gap_ms < min_gap_ms:
                # 本来就是停顿（≥门槛）但太短 → 拉长到 min_gap_ms
                # （人类在逗号处会停，模型的短句常常给得极短）
                pieces.append(np.zeros(int(rate * min_gap_ms / 1000), dtype=np.int16))
            else:
                pieces.append(x[i * hop : j * hop])
            i = j

    body = np.concatenate(pieces) if pieces else x[first * hop : (last + 1) * hop]
    out = x
    if head_keep or tail_keep:
        out = np.concatenate(
            [
                np.zeros(head_keep, dtype=np.int16),
                body,
                np.zeros(tail_keep, dtype=np.int16),
            ]
        )
    else:
        out = body
    return np.ascontiguousarray(out)


class PacingFixer:
    """把配置里的一组裁剪参数收在一起，供后端按段调用。"""

    def __init__(
        self,
        enabled: bool = True,
        lead_ms: int = DEFAULT_LEAD_MS,
        tail_ms: int = DEFAULT_TAIL_MS,
        max_pause_ms: int = DEFAULT_MAX_PAUSE_MS,
        min_pause_ms: int = DEFAULT_MIN_PAUSE_MS,
        min_gap_ms: int = DEFAULT_MIN_GAP_MS,
        min_gap_floor_ms: int = DEFAULT_MIN_GAP_FLOOR_MS,
    ) -> None:
        self.enabled = bool(enabled)
        self.lead_ms = max(0, int(lead_ms))
        self.tail_ms = max(0, int(tail_ms))
        self.max_pause_ms = max(0, int(max_pause_ms))
        self.min_pause_ms = max(0, int(min_pause_ms))
        self.min_gap_ms = max(0, int(min_gap_ms))
        self.min_gap_floor_ms = max(0, int(min_gap_floor_ms))

    @classmethod
    def from_config(cls, cfg) -> "PacingFixer":
        return cls(
            enabled=bool(getattr(cfg, "trim_output_silence", True)),
            lead_ms=int(getattr(cfg, "trim_lead_ms", DEFAULT_LEAD_MS) or 0),
            tail_ms=int(getattr(cfg, "trim_tail_ms", DEFAULT_TAIL_MS) or 0),
            max_pause_ms=int(getattr(cfg, "trim_max_pause_ms", DEFAULT_MAX_PAUSE_MS) or 0),
            min_pause_ms=int(getattr(cfg, "trim_min_pause_ms", DEFAULT_MIN_PAUSE_MS) or 0),
            min_gap_ms=int(getattr(cfg, "trim_min_gap_ms", DEFAULT_MIN_GAP_MS) or 0),
            min_gap_floor_ms=int(
                getattr(cfg, "trim_min_gap_floor_ms", DEFAULT_MIN_GAP_FLOOR_MS)
                if getattr(cfg, "trim_min_gap_floor_ms", None) is not None
                else DEFAULT_MIN_GAP_FLOOR_MS
            ),
        )

    def apply(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        if not self.enabled:
            return np.ascontiguousarray(_to_int16(pcm))
        return trim_silence(
            pcm,
            rate,
            lead_ms=self.lead_ms,
            tail_ms=self.tail_ms,
            max_pause_ms=self.max_pause_ms,
            min_pause_ms=self.min_pause_ms,
            min_gap_ms=self.min_gap_ms,
            min_gap_floor_ms=self.min_gap_floor_ms,
        )

    def describe(self) -> str:
        if not self.enabled:
            return "静音裁剪：关"
        pause = "不动句内停顿" if self.max_pause_ms <= 0 else f"句内停顿压到 {self.min_pause_ms}ms"
        if self.min_gap_ms > 0:
            pause += f"；{self.min_gap_floor_ms}~{self.min_gap_ms}ms 的停顿拉到 {self.min_gap_ms}ms"
        return f"静音裁剪：首 {self.lead_ms}ms / 尾 {self.tail_ms}ms / {pause}"
