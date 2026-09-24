"""参考音频净化：在入口把「底噪 → 沙沙声」这条通路掐掉。

为什么要在**参考音频**上下手（2026-09-24 的实测链，详见 docs/ENGINEERING_LOG.md 16.7）：

* 沙沙声的根不在模型结构、不在 int8 量化、更不在采样步数，而在**媒体素材的底噪**；
* 推理时那条参考音频是噪声的**载体**：全因子交叉实测「同模型换参考」把输出的高频
  放大 ×1.8~2.5，而参考自己只差 1.4 倍；
* 实测两条在用参考的差距：

  | 参考 | 时长 | 安静帧高频占比 | 安静帧占比 |
  |---|---|---|---|
  | 凯尔希 `干员报到.wav` | 7.79s | **6.8%** | 19.3% |
  | 阿米娅 `交谈1.wav` | 8.02s | **82.3%** | 25.1% |

  阿米娅那条的「安静帧」几乎全是高频——就是底噪/气声，模型照着它学到了沙沙声。

三步处理（都可关，默认只做第一步）：

1. **谱门**（`gate`）：用它**自己的安静帧**估出每个频点的噪声底，再按信噪比软衰减。
   参考音频里的停顿就是现成的噪声样本，不需要额外采噪。
2. **高架衰减**（`tilt`）：>7 kHz 整体轻降几个 dB。沙沙声住在那一段，但**齿音也住在那一段**，
   所以默认关（`-3 dB` 是「听得出干净一些、又不太闷」的量级，靠耳朵定）。
3. **响度归一**（`gain`）：把 RMS 对齐到目标，免得净化后音量变小、被模型当成弱输入。

三条纪律：

* ★只改幅度谱、不动相位，也不改时长★——参考音频和参考文本必须逐字对应，
  改时长就等于「声不对词」（踩过：`clone_max_seconds` 截断 → 输出含糊）；
* 增益有**下限**（默认最多衰减 18 dB）且做了时/频平滑——硬门会把气声剪成
  「一格一格的」怪声，那比沙沙声更难听；
* 纯 numpy，不引入新依赖（运行时路径上只有 numpy）。
"""

from __future__ import annotations

import numpy as np

# 谱门的默认参数（44.1kHz 的参考音频下调出来的）
WIN = 1024                  # 约 23 ms
HOP = WIN // 4              # 75% 重叠 → 重建误差可忽略（有测试守着）
QUIET_PCT = 25.0            # 「安静帧」= 能量最低的这百分之几帧
HF_HZ = 6000.0              # 高频（沙沙声）与整条能量的分界，与体检脚本同一个口径
FRAME_MS = 30.0             # 体检指标用的帧长（与 pick_voice_ref 对齐）

DEFAULT_MODE = "off"        # off | gate | gate+tilt
MODES = ("off", "gate", "gate+tilt")
DEFAULT_STRENGTH = 0.7      # 0 = 不动，1 = 门开到最大
DEFAULT_MAX_ATTEN_DB = 18.0  # 单个频点最多衰减多少（下限增益）
DEFAULT_TILT_HZ = 7000.0
DEFAULT_TILT_DB = -3.0
DEFAULT_TARGET_DBFS = 0.0   # 0 = 不做响度归一


# --------------------------------------------------------------------------- #
# STFT / ISTFT（numpy 手写，77 行，不引 scipy）
# --------------------------------------------------------------------------- #
def _window(n: int) -> np.ndarray:
    return np.hanning(n + 1)[:n].astype(np.float64)


def _frames(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    """切成 (帧数, win) 的矩阵；不足一帧的部分丢掉（调用方补回去）。"""
    n = 0 if x.size < win else 1 + (x.size - win) // hop
    if n <= 0:
        return np.zeros((0, win), dtype=np.float64)
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _ola(frames: np.ndarray, win: int, hop: int, length: int) -> np.ndarray:
    """重叠相加 + 用「窗的平方和」归一化（hann@75% 重叠可完美重建）。"""
    w = _window(win)
    y = np.zeros(length + win, dtype=np.float64)
    acc = np.zeros(length + win, dtype=np.float64)
    for i in range(frames.shape[0]):
        a, b = i * hop, i * hop + win
        seg = frames[i] * w
        y[a:b] += seg
        acc[a:b] += w * w
    y = y[:length]
    acc = acc[:length]
    # 窗平方和最小处（首尾）不能除，否则会放大噪声；那里按最近的有效值裁掉
    ok = acc > 1e-8
    if not ok.any():
        return np.zeros(length, dtype=np.float64)
    y[ok] /= acc[ok]
    return y


def _stft(x: np.ndarray, win: int = WIN, hop: int = HOP) -> np.ndarray:
    return np.fft.rfft(_frames(x, win, hop) * _window(win), axis=1)


def _istft(spec: np.ndarray, win: int, hop: int, length: int) -> np.ndarray:
    return _ola(np.fft.irfft(spec, n=win, axis=1), win, hop, length)


def _pad(x: np.ndarray, n: int) -> np.ndarray:
    """反射补边：hann 窗的两端权重是 0，信号的头尾几点**数学上不可重建**
    （实测不补边时首尾 74 个样本误差能到 0.24），补一圈就变成完全精确。
    """
    if x.size <= n:
        return np.pad(x, (n, n), mode="edge")
    return np.pad(x, (n, n), mode="reflect")


def reconstruct(x: np.ndarray, win: int = WIN, hop: int = HOP) -> np.ndarray:
    """STFT → ISTFT 往返（带反射补边，所以是精确重建）——测试和自检用。"""
    y = np.asarray(x, dtype=np.float64).reshape(-1)
    if y.size <= 2 * win:
        return y.copy()
    p = _pad(y, win)
    return _istft(_stft(p, win, hop), win, hop, p.size)[win : win + y.size]


def hf_metrics(x: np.ndarray, rate: int) -> tuple[float, float, float]:
    """(整条高频占比, 安静帧高频占比, 安静帧占比)，单位 %——与体检脚本同一口径。

    安静帧 = 能量最低 ``QUIET_PCT`` 的帧（停顿 / 塞音成阻 / 气口），那里的高频只可能
    是底噪与气声，**与说了什么无关**，所以两组音频之间才可比。
    """
    mono = np.asarray(x, dtype=np.float64).reshape(-1)
    hop = max(1, int(rate * FRAME_MS / 1000))
    n = mono.size // hop
    if n < 4:
        return float("nan"), float("nan"), 0.0
    frames = mono[: n * hop].reshape(n, hop)
    freqs = np.fft.rfftfreq(hop, 1.0 / rate)
    high = freqs > HF_HZ
    rms = np.sqrt((frames**2).mean(axis=1))
    spec = np.abs(np.fft.rfft(frames * _window(hop), axis=1)) ** 2
    total = spec.sum(axis=1) + 1e-12
    per_frame = spec[:, high].sum(axis=1) / total
    quiet = (rms <= np.percentile(rms, QUIET_PCT)) & (rms > 1e-9)
    return (
        float(spec[:, high].sum() / total.sum() * 100),
        float(per_frame[quiet].mean() * 100) if quiet.any() else float("nan"),
        float(quiet.mean() * 100),
    )


# --------------------------------------------------------------------------- #
# 三步处理
# --------------------------------------------------------------------------- #
def noise_profile(x: np.ndarray, rate: int, quiet_pct: float = QUIET_PCT) -> np.ndarray:
    """从安静帧估每个频点的噪声幅度（静音段就是现成的噪声样本）。"""
    spec = _stft(x)
    if spec.shape[0] == 0:
        return np.zeros(spec.shape[1] or 0, dtype=np.float64)
    mag = np.abs(spec)
    energy = (mag**2).sum(axis=1)
    thr = np.percentile(energy, quiet_pct)
    quiet = energy <= thr
    if not quiet.any():
        quiet = np.ones_like(energy, dtype=bool)
    # 取中位数：个别帧里混进来的浊音不会把底噪估高
    return np.median(mag[quiet], axis=0)


def gate(
    x: np.ndarray,
    rate: int,
    *,
    strength: float = DEFAULT_STRENGTH,
    max_atten_db: float = DEFAULT_MAX_ATTEN_DB,
    quiet_pct: float = QUIET_PCT,
) -> np.ndarray:
    """软谱门：``g = 1 - α·N/|X|``，钳在 [floor, 1]，再做时/频平滑。

    α = 1 + strength（过减一点，免得噪声在门限附近来回跳）。``strength=0`` 时等价于原样返回。
    """
    strength = float(min(1.0, max(0.0, strength)))
    if strength <= 0.0 or x.size < WIN * 2:
        return np.asarray(x, dtype=np.float32)
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    pad = _pad(x, WIN)                      # 补边 → 首尾也可重建 + 噪声估得稳
    spec = _stft(pad)
    if spec.shape[0] == 0:
        return np.asarray(x, dtype=np.float32)
    noise = noise_profile(pad, rate, quiet_pct)
    mag = np.abs(spec)
    alpha = 1.0 + strength
    floor = 10.0 ** (-abs(max_atten_db) / 20.0)
    ratio = noise[None, :] / np.maximum(mag, 1e-9)
    g = np.clip(1.0 - alpha * ratio, floor, 1.0)
    # 时/频平滑：消掉「音乐噪声」（硬门最典型的怪声）。
    # ★不要再叠一层「向 1 拉」的软化★：那样最大衰减会被锁在 -8 dB 左右，
    #   纯噪声段根本压不下去（实测：想压 ≥10 dB 就做不到，而 max_atten_db 形同虚设）。
    g = _smooth_freq(_smooth_time(g))
    out = _istft(spec * g, WIN, HOP, pad.size)[WIN : WIN + x.size]
    return np.asarray(out, dtype=np.float32)


def tilt(x: np.ndarray, rate: int, cut_hz: float = DEFAULT_TILT_HZ, gain_db: float = DEFAULT_TILT_DB) -> np.ndarray:
    """高架：>``cut_hz`` 整体乘一个系数（线性相位、零延迟，只动幅度）。"""
    if abs(gain_db) < 1e-3 or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    n = np.fft.rfft(np.asarray(x, dtype=np.float64))
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    gain = np.ones_like(freqs)
    band = freqs >= cut_hz
    if band.any():
        # 截止点附近用 500 Hz 过渡，避免开关一样的硬边
        edge = max(1.0, min(500.0, cut_hz * 0.1))
        ramp = np.clip((freqs - (cut_hz - edge)) / edge, 0.0, 1.0)
        gain = 10.0 ** (gain_db / 20.0 * ramp)
    return np.asarray(np.fft.irfft(n * gain, n=x.size), dtype=np.float32)


def rms_normalize(x: np.ndarray, target_dbfs: float = DEFAULT_TARGET_DBFS,
                  max_gain_db: float = 6.0) -> tuple[np.ndarray, float]:
    """把整体 RMS 对齐到 ``target_dbfs``（0 = 不做）；返回 (音频, 实际增益 dB)。"""
    x = np.asarray(x, dtype=np.float32)
    if target_dbfs >= 0 or x.size == 0:
        return x, 0.0
    rms = float(np.sqrt((np.asarray(x, dtype=np.float64) ** 2).mean()))
    if rms <= 1e-9:
        return x, 0.0
    want = 10.0 ** (target_dbfs / 20.0)
    gain_db = float(np.clip(20.0 * np.log10(want / rms), -abs(max_gain_db), abs(max_gain_db)))
    return np.asarray(x * (10.0 ** (gain_db / 20.0)), dtype=np.float32), gain_db


def process(
    x: np.ndarray,
    rate: int,
    mode: str = DEFAULT_MODE,
    *,
    strength: float = DEFAULT_STRENGTH,
    max_atten_db: float = DEFAULT_MAX_ATTEN_DB,
    quiet_pct: float = QUIET_PCT,
    tilt_hz: float = DEFAULT_TILT_HZ,
    tilt_db: float = DEFAULT_TILT_DB,
    target_dbfs: float = DEFAULT_TARGET_DBFS,
) -> tuple[np.ndarray, dict]:
    """按 ``mode`` 处理一条参考音频；返回 (音频, 报告)。

    报告里带**前后对比**（整条高频 / 安静帧高频），因为耳听之前先用数字筛一遍更便宜。
    """
    audio = np.asarray(x, dtype=np.float32).reshape(-1)
    info: dict = {"mode": mode, "before": hf_metrics(audio, rate)}
    if mode == "off" or audio.size == 0 or rate <= 0:
        info["after"] = info["before"]
        return audio, info

    peak = float(np.max(np.abs(audio)))
    if peak <= 1e-6:
        info["after"] = info["before"]
        return audio, info

    if "gate" in mode:
        audio = gate(audio, rate, strength=strength, max_atten_db=max_atten_db, quiet_pct=quiet_pct)
    if "tilt" in mode:
        audio = tilt(audio, rate, tilt_hz, tilt_db)
    audio, gain_db = rms_normalize(audio, target_dbfs)

    peak = float(np.max(np.abs(audio)))
    if peak > 1.0:                      # 别让归一化把样本推出 [-1,1]
        audio = audio / peak
    info["gain_db"] = round(gain_db, 2)
    info["after"] = hf_metrics(audio, rate)
    return np.ascontiguousarray(audio, dtype=np.float32), info


def _smooth_time(g: np.ndarray, radius: int = 1) -> np.ndarray:
    if g.shape[0] <= 2 * radius:
        return g
    out = g.copy()
    for k in range(1, radius + 1):
        out[k:, :] = (out[k:, :] + g[:-k, :]) / 2.0
        out[:-k, :] = (out[:-k, :] + g[k:, :]) / 2.0
    return out


def _smooth_freq(g: np.ndarray, radius: int = 2) -> np.ndarray:
    if g.shape[1] <= 2 * radius:
        return g
    out = g.copy()
    for k in range(1, radius + 1):
        out[:, k:] = (out[:, k:] + g[:, :-k]) / 2.0
        out[:, :-k] = (out[:, :-k] + g[:, k:]) / 2.0
    return out


def describe(info: dict) -> str:
    """一行报告，给人看（写进日志/终端）。"""
    def fmt(v: float) -> str:
        return "n/a" if v != v else f"{v:.1f}%"

    b = info.get("before", (float("nan"),) * 3)
    a = info.get("after", (float("nan"),) * 3)
    extra = f"，响度 {info['gain_db']:+.1f} dB" if "gain_db" in info else ""
    return (
        f"整条高频 {fmt(b[0])} → {fmt(a[0])}，安静帧高频 {fmt(b[1])} → {fmt(a[1])}"
        f"（{info.get('mode', 'off')}{extra}）"
    )
