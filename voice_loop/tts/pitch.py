"""基频（F0）跟踪：量「异常的高亢 / 低沉」用的尺子。

为什么需要它（2026-09-25）：用户要的「语气连贯」不是停顿长短，而是
**不要出现异常的高亢和低沉**——也就是音高的离群跳变。停顿那套（pacing）管不了这个，
得先能一条一条地量出音高轨迹，才知道是谁在捣鬼（采样随机？分块？参考音频？）。

做法（2026-09-25 换过算法）：帧级 **YIN**（de Cheveigné & Kawahara 2002，纯 numpy）：
* 45 ms 窗 / 10 ms 跳，能量太低 → 静音（NaN）；
* 累积均值归一化差函数 d'(τ) → 取**第一个**低于阈值 0.15 的谷 → 抛物线插值；
* 没有谷 = 清音（NaN），最后对整条轨迹做**中位滤波**（去单帧毛刺）；
* 音高范围默认 70~420 Hz（男女都罩得住）。

★为什么不能用「归一化自相关」★：实测拿**真人参考音**喂进去，0.25 s 一格的中位 F0
会在 150/280 Hz（差一个八度）之间来回翻——那是尺子错，不是声音错。自相关有 1/f 偏差，
容易锁到二次/三次谐波或共振峰上；YIN 的归一化差函数从算法上压掉这类错。★换尺子之前
不要拿它的数字下结论★：曾经因为尺子病重，把「量错了」当成「模型真的忽高忽低」。

★为什么不能只看中位数★：「偶尔高一下」在中位数里看不见。所以指标里既有
`outlier_pct`（离**局部**中位超过 N 个半音的有声帧占比），也有
`sustained_up/down`（持续偏离的最远半音数）——后者才对应「突然拔高/突然沉下去」这种听感。
"""

from __future__ import annotations

import numpy as np

FMIN = 70.0
FMAX = 420.0
WIN_MS = 45.0          # ≥3 个周期（最低 70 Hz 时一个周期 14 ms）
HOP_MS = 10.0
YIN_TH = 0.15          # 差函数谷低于它 = 浊音（YIN 论文的推荐值）
YIN_TH_MAX = 0.30      # 若整句都没谷低于 YIN_TH（气声重），放宽到这再试一次
MIN_RMS = 3e-4         # 能量太低 = 静音（保留 NaN，别把停顿算成「低沉」）


def to_float(x) -> np.ndarray:
    """统一成 [-1,1] 浮点（int16 直接当浮点算会把电平算错，踩过）。"""
    a = np.asarray(x)
    if a.dtype == np.int16:
        return a.astype(np.float64) / 32768.0
    a = a.astype(np.float64).reshape(-1)
    return a / 32768.0 if np.max(np.abs(a), initial=0.0) > 2.0 else a


def _difference(z: np.ndarray) -> np.ndarray:
    """差函数 d(τ)=Σ_j (z_j − z_{j+τ})²，z 是补零到 2W 的窗（τ ≤ W，精确）。

    用「能量 − 2×自相关」的等价形式算，一条 FFT 就够：
    d(τ) = Σ_{j<W} z_j² + Σ_{j=τ}^{τ+W−1} z_j² − 2·acf(τ)
    """
    nfft = z.size
    acf = np.fft.irfft(np.abs(np.fft.rfft(z)) ** 2, nfft)
    cum = np.concatenate(([0.0], np.cumsum(z * z)))
    w = nfft // 2
    t = np.arange(w + 1)
    return (cum[w] - cum[0]) + (cum[w + t] - cum[t]) - 2.0 * acf[: w + 1]


def _cmndf(d: np.ndarray) -> np.ndarray:
    """累积均值归一化差函数 d'(τ) = d(τ) / [(1/τ)·Σ_{j=1..τ} d(j)]，d'(0)=1。

    ★这一步是 YIN 抗八度错的根本★：归一化后，真周期的谷会明显低于它整数倍的谷。
    """
    out = np.ones_like(d)
    if d.size <= 1:
        return out
    cum = np.cumsum(d[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = d[1:] * np.arange(1, d.size) / cum
    return np.nan_to_num(out, nan=1.0, posinf=1.0)


def _yin_f0(z: np.ndarray, rate: int, fmin: float, fmax: float, th: float) -> float:
    """YIN 单帧：第一个「低于阈值的谷」的倒数是 F0；没有就返回 NaN。"""
    w = z.size // 2
    t_lo = max(2, int(rate / fmax))
    t_hi = min(w, int(rate / fmin))
    if t_hi <= t_lo + 1:
        return float("nan")
    dp = _cmndf(_difference(z))
    seg = dp[t_lo : t_hi + 1]
    below = np.nonzero(seg < th)[0]
    if below.size:
        t = t_lo + int(below[0])
        while t + 1 <= t_hi and dp[t + 1] < dp[t]:    # 走到谷底
            t += 1
    else:
        t = t_lo + int(np.argmin(seg))
        if dp[t] > max(th, YIN_TH_MAX):
            return float("nan")
    if t <= 0:
        return float("nan")
    a, b, c = dp[t - 1], dp[t], dp[t + 1] if t + 1 < dp.size else dp[t]
    denom = a - 2 * b + c
    tau = t + 0.5 * (a - c) / denom if abs(denom) > 1e-12 else float(t)
    return float(rate / tau) if tau > 0 else float("nan")


def f0_track(
    x,
    rate: int,
    *,
    fmin: float = FMIN,
    fmax: float = FMAX,
    win_ms: float = WIN_MS,
    hop_ms: float = HOP_MS,
    th: float = YIN_TH,
    smooth: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (时间秒数组, F0 数组)；清音/静音处是 NaN。``smooth`` = 中位滤波窗（帧）。

    ★算法是 YIN，不是「归一化自相关」★（2026-09-25 换的）。原因见文件头：
    自相关版在真人参考音上就能把 0.25 s 一格的中位 F0 在 150/280 Hz 之间来回翻，
    那是尺子错，不是声音错。★换尺子之前不要拿它的数字下结论★。
    """
    sig = to_float(x)
    if sig.size == 0 or rate <= 0:
        return np.zeros(0), np.zeros(0)
    win = max(8, int(rate * win_ms / 1000.0))
    hop = max(1, int(rate * hop_ms / 1000.0))
    if sig.size < win:
        return np.zeros(0), np.zeros(0)
    n = 1 + (sig.size - win) // hop
    f0 = np.full(n, np.nan)
    for i in range(n):
        seg = sig[i * hop : i * hop + win]
        seg = seg - seg.mean()
        if np.sqrt((seg**2).mean()) < MIN_RMS:
            continue
        z = np.zeros(2 * win)
        z[:win] = seg                     # YIN 不需要加窗（加窗反而伤差函数）
        v = _yin_f0(z, rate, fmin, fmax, th)
        if v == v:
            f0[i] = v
    if smooth and smooth > 1:
        f0 = median_filter(f0, smooth)
    times = (np.arange(n) * hop + win / 2.0) / rate
    return times, f0


def median_filter(f0: np.ndarray, k: int) -> np.ndarray:
    """只在有声帧上做中位滤波（NaN 不参与，**且保持 NaN**）。

    ★不要拿邻近音高去填清音帧★：那会凭空造出「有声」（有声率虚高、停顿变短），
    而且会把「清音段」当成音高证据。
    """
    if k <= 1 or f0.size == 0:
        return f0
    half = k // 2
    out = f0.copy()
    for i in range(f0.size):
        if np.isnan(f0[i]):
            continue
        lo, hi = max(0, i - half), min(f0.size, i + half + 1)
        seg = f0[lo:hi]
        seg = seg[~np.isnan(seg)]
        if seg.size:
            out[i] = float(np.median(seg))
    return out


def semitone(a: float, b: float) -> float:
    """a 相对 b 的半音差。"""
    if not (a > 0 and b > 0):
        return float("nan")
    return 12.0 * np.log2(a / b)


def local_median(f0: np.ndarray, half: int = 50) -> np.ndarray:
    """滑动中位（默认 ±0.5 秒）——「局部正常音高」的基准。"""
    out = np.full(f0.size, np.nan)
    for i in range(f0.size):
        lo, hi = max(0, i - half), min(f0.size, i + half + 1)
        seg = f0[lo:hi]
        seg = seg[~np.isnan(seg)]
        if seg.size:
            out[i] = float(np.median(seg))
    return out


def stats(f0: np.ndarray, *, outlier_st: float = 6.0, half: int = 50, hold: int = 20) -> dict:
    """一条音高轨迹的体检：寄存器 + 摆幅 + 离群。

    ``outlier_pct``：离**局部**中位超过 ``outlier_st`` 半音的有声帧占比。
    ``sustained_up/dn``：★持续偏离★——把轨迹按 ``hold`` 帧（默认 200 ms）取中位后再算
    最远偏离。★这个才是耳朵能听到的「突然拔高/沉下去」★：单帧跳到高八度是跟踪器的
    常见误判（听不出来），而真高亢会持续几十~几百毫秒。
    """
    voiced = f0[~np.isnan(f0)]
    if voiced.size < 5:
        return {"voiced_pct": 0.0, "f0_med": float("nan"), "iqr_st": float("nan"),
                "outlier_pct": float("nan"), "sustained_up": float("nan"),
                "sustained_dn": float("nan"), "jump_pct": float("nan")}
    med = float(np.median(voiced))
    q1, q3 = np.percentile(voiced, [25, 75])
    ref = local_median(f0, half)
    ok = ~np.isnan(f0) & ~np.isnan(ref)
    dev = np.array([semitone(f, r) for f, r in zip(f0[ok], ref[ok])])
    # 持续偏离：按窗取中位，再比局部基准
    hold = max(1, int(hold))
    sus_up = sus_dn = float("nan")
    if voiced.size >= hold:
        win_ref = local_median(median_filter(f0, hold), half)
        m = ~np.isnan(f0) & ~np.isnan(win_ref)
        sdev = np.array([semitone(f, r) for f, r in zip(f0[m], win_ref[m])])
        if sdev.size:
            sus_up, sus_dn = float(np.nanmax(sdev)), float(np.nanmin(sdev))
    # 相邻有声帧之间的跳变（越少越平滑）
    idx = np.nonzero(~np.isnan(f0))[0]
    jumps = []
    for a, b in zip(idx[:-1], idx[1:]):
        if b - a <= 3:                     # 中间没有长静音才算相邻
            jumps.append(abs(semitone(f0[b], f0[a])))
    return {
        "voiced_pct": round(100.0 * voiced.size / f0.size, 1),
        "f0_med": round(med, 1),
        "iqr_st": round(float(semitone(q3, q1)), 2),
        "outlier_pct": round(float(np.mean(np.abs(dev) > outlier_st) * 100), 1) if dev.size else float("nan"),
        "sustained_up": round(sus_up, 1) if sus_up == sus_up else float("nan"),
        "sustained_dn": round(sus_dn, 1) if sus_dn == sus_dn else float("nan"),
        "jump_pct": round(float(np.mean([j > 3.0 for j in jumps]) * 100), 1) if jumps else 0.0,
    }


def describe(s: dict) -> str:
    return (f"F0 中位 {s['f0_med']:.0f}Hz 摆幅(四分位) {s['iqr_st']:.1f}半音 "
            f"离群 {s['outlier_pct']:.1f}% 持续最远 +{s['sustained_up']:.1f}/{s['sustained_dn']:.1f}半音 "
            f"跳变 {s['jump_pct']:.1f}%")


def f0_median(pcm, rate: int, *, min_voiced_pct: float = 15.0) -> float | None:
    """整段音频的「音区」中位（Hz）。有声帧太少 / 量不出来 → ``None``。

    ★这个只用来当裁判，量不出来就别裁★：宁可放过一遍，也不能因为量错而白重采。
    """
    if rate <= 0:
        return None
    _, f0 = f0_track(pcm, rate)
    s = stats(f0)
    med = float(s["f0_med"])
    if med != med or float(s["voiced_pct"]) < float(min_voiced_pct):
        return None
    return med


def register_st(pcm, rate: int, target_hz: float) -> float | None:
    """这一段整体比靶子高（+）/低（−）多少个半音；量不出来 → ``None``。

    ★「异常的高亢 / 低沉」量出来就是这个数★（实测：同一句话重采 6 遍，
    amiya 的整体音区在 240~290 Hz 之间跑，最多差 2.0 半音；参考音本身 258 Hz）。
    """
    if not (target_hz > 0):
        return None
    med = f0_median(pcm, rate)
    if med is None:
        return None
    return float(semitone(med, target_hz))

