"""参考音频净化 + 输出塑形的离线自测（不加载模型、不出声，秒级）。

为什么值得单测（这些数字都是踩出来的）：
* 谱门只能动**幅度**，不能改时长——参考音频和参考文本必须逐字对应，
  改时长就等于「声不对词」（`clone_max_seconds` 截断过，输出直接含糊）；
* STFT→ISTFT 必须**精确重建**，否则「强度设 0」都会悄悄改变音色；
* 增益要有下限：硬门会把气声剪成「一格一格的」怪声，比沙沙声更难听；
* 块间电平对齐（LevelMatcher）只能修「明显的忽大忽小」，不能把语气轻重压平。

    python scripts/test_refclean.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.tts import refclean as rc  # noqa: E402
from voice_loop.tts.pacing import LevelMatcher, trim_silence, voiced_db  # noqa: E402

RATE = 24000
FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def tone(freq: float, seconds: float, amp: float, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds: float, amp: float, rate: int = RATE, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.normal(0, 1, int(rate * seconds))).astype(np.float32)


def band_hf(x: np.ndarray, rate: int = RATE, cut: float = 6000.0) -> float:
    """>cut 的能量占比（%）。"""
    if x.size < 64:
        return float("nan")
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    return float(spec[freqs > cut].sum() / (spec.sum() + 1e-12) * 100)


def db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt((x.astype(np.float64) ** 2).mean()) + 1e-12))


def band_db(x: np.ndarray, lo: float, hi: float, rate: int = RATE) -> float:
    """[lo, hi) 频段的平均功率（dB）——用来验「只动这一段」。"""
    spec = np.abs(np.fft.rfft(np.asarray(x, dtype=np.float64) * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    band = (freqs >= lo) & (freqs < hi)
    if not band.any():
        return float("nan")
    return float(10 * np.log10(spec[band].mean() + 1e-20))


def main() -> int:
    print("=" * 70)
    print(" 参考音频净化 / 输出塑形 自测（纯离线）")
    print("=" * 70)

    print("\n[1] STFT→ISTFT 必须精确重建")
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.1, 44100)
    rec = rc.reconstruct(x)
    check("随机信号重建误差 < 1e-9", float(np.abs(rec - x).max()) < 1e-9, True,
          f"max={float(np.abs(rec - x).max()):.2e}")
    check("长度不变", rec.size, x.size)

    print("\n[2] 强度 0 / 模式 off = 一动不动")
    speech = np.concatenate([tone(180, 0.4, 0.3), noise(0.3, 0.02), tone(180, 0.4, 0.3)])
    off, info = rc.process(speech, RATE, "off")
    check("off 原样返回", bool(np.array_equal(off, speech)), True)
    g0 = rc.gate(speech, RATE, strength=0.0)
    check("strength=0 等价于原样", float(np.abs(g0 - speech).max()), 0.0)

    print("\n[3] 谱门：压掉噪声底、不动语音")
    # 「语音」= 低频音 + 「噪声」= 7kHz 宽带底噪（-45 dBFS）
    mix = np.concatenate([
        tone(180, 0.5, 0.3), noise(0.5, 0.006), tone(180, 0.5, 0.3), noise(0.5, 0.006),
    ])
    gate, info = rc.process(mix, RATE, "gate", strength=0.7)
    gaps_n = slice(int(RATE * 0.5), int(RATE * 1.0))
    check("停顿段高频被压下去 ≥10 dB",
          db(gate[gaps_n]) - db(mix[gaps_n]) < -10, True,
          f"{db(mix[gaps_n]) - db(gate[gaps_n]):.1f} dB")
    voiced_n = slice(int(RATE * 0.05), int(RATE * 0.45))
    check("语音段电平变化 < 0.5 dB", abs(db(gate[voiced_n]) - db(mix[voiced_n])) < 0.5, True,
          f"{db(gate[voiced_n]) - db(mix[voiced_n]):+.2f} dB")
    check("长度不变（声不对词的根因）", gate.size, mix.size)
    check("dtype 保持 float32", str(gate.dtype), "float32")
    again, _ = rc.process(mix, RATE, "gate", strength=0.7)
    check("确定性（同输入同输出）", bool(np.array_equal(gate, again)), True)

    print("\n[4] 高架 tilt：只动高频，且量对得上")
    mix2 = np.concatenate([tone(150, 0.5, 0.3), tone(9000, 0.5, 0.3)])
    t6 = rc.tilt(mix2, RATE, 7000.0, -6.0)
    lf = slice(0, int(RATE * 0.5))
    hf = slice(int(RATE * 0.5), RATE)
    check("低频段不变（<0.3 dB）", abs(db(t6[lf]) - db(mix2[lf])) < 0.3, True)
    check("高频段约 -6 dB", -7.0 < (db(t6[hf]) - db(mix2[hf])) < -5.0, True,
          f"{db(t6[hf]) - db(mix2[hf]):+.2f} dB")
    check("长度不变", t6.size, mix2.size)

    # ★2026-09-25 新增：沙沙声住在 10~12kHz 那一层，所以要能**定向**削它而不动齿音★
    # （实测：微调模型 10-12k 是 -8.0 dB，比它自己的 8-10k 还高 4.3 dB；真人参考是 -16.0）
    white = noise(1.0, 0.2)
    t10 = rc.tilt(white, RATE, 10000.0, -9.0)
    d10 = band_db(t10, 10000, 12000) - band_db(white, 10000, 12000)
    check(f"10kHz/-9：10-12k 段降 {d10:+.2f} dB（目标 -9）", -9.8 < d10 < -8.2, True)
    for lo, hi, name in ((4000, 6000, "4-6k"), (6000, 8000, "6-8k")):
        d = band_db(t10, lo, hi) - band_db(white, lo, hi)
        check(f"10kHz/-9：{name} 段（齿音）基本不动 {d:+.2f} dB", abs(d) < 0.5, True)
    t_hi = rc.tilt(white, RATE, 7000.0, -3.0)
    d_hf = band_db(t_hi, 8000, 10000) - band_db(white, 8000, 10000)
    check(f"旧设定 7kHz/-3 就是图上说的「打偏」：8-10k 被砍 {d_hf:+.2f} dB", d_hf < -2.0, True)

    print("\n[5] 响度归一：够到目标、且有上限")
    quiet = tone(200, 0.3, 0.01)
    norm, gain = rc.rms_normalize(quiet, -20.0, max_gain_db=40.0)
    check("归一后 RMS ≈ -20 dBFS", abs(db(norm) + 20) < 0.2, True, f"{db(norm):.2f} dBFS")
    tiny = tone(200, 0.3, 1e-5)
    _n2, g2 = rc.rms_normalize(tiny, -20.0, max_gain_db=6.0)
    check("增益有上限", abs(g2) <= 6.0, True, f"{g2:+.2f} dB")
    _n3, g3 = rc.rms_normalize(quiet, 0.0)
    check("目标 0 = 不做归一", g3, 0.0)

    print("\n[6] 可闻安静帧指标（新口径，别再拿 -80 dBFS 的帧当底噪）")
    near_silence = np.concatenate([tone(200, 0.4, 0.3), noise(1.2, 1e-5)])
    _whole, quiet_hf, _share = rc.hf_metrics(near_silence, RATE)
    check("近数字静音不会把 HF 指标刷到 90%", quiet_hf < 60.0, True, f"{quiet_hf:.1f}%")

    print("\n[7] 块间电平对齐：只修明显的忽大忽小")
    lm0 = LevelMatcher(0.0)
    loud = np.clip(tone(200, 0.4, 0.5) * 32767, -32768, 32767).astype(np.int16)
    check("max_db=0 = 关", bool(np.array_equal(lm0.apply(loud, RATE), loud)), True)
    lm = LevelMatcher(1.5)
    a = np.clip(tone(200, 0.4, 0.5) * 32767, -32768, 32767).astype(np.int16)
    b = np.clip(tone(200, 0.4, 0.18) * 32767, -32768, 32767).astype(np.int16)
    lm.apply(a, RATE)                      # 第一块定基准
    fixed = lm.apply(b, RATE)
    check("安静的那块被抬高，但不超过上限",
          -1.55 <= voiced_db(fixed, RATE) - voiced_db(b, RATE) <= 1.5, True,
          f"{voiced_db(fixed, RATE) - voiced_db(b, RATE):+.2f} dB")

    print("\n[8] 按比例压长停顿：压掉死气但保留长短对比")
    def block(gap_s: float) -> np.ndarray:
        return np.concatenate([tone(200, 0.4, 0.3), np.zeros(int(RATE * gap_s), np.float32)])
    pauses = np.concatenate([block(1.31), block(0.94), block(0.74), block(0.3), block(0.0)])
    pcm = np.clip(pauses * 32767, -32768, 32767).astype(np.int16)
    prop = trim_silence(pcm, RATE, shrink_pause=0.35, shrink_over_ms=450, min_pause_ms=260,
                        min_gap_ms=240, min_gap_floor_ms=60)
    flat = trim_silence(pcm, RATE, max_pause_ms=450, min_pause_ms=260,
                        min_gap_ms=240, min_gap_floor_ms=60)
    from scripts.ab_clone_model import gaps_ms
    g_prop, g_flat = gaps_ms(prop, RATE), gaps_ms(flat, RATE)
    check("比例压：总时长变短", prop.size < pcm.size, True,
          f"{pcm.size / RATE:.2f}s → {prop.size / RATE:.2f}s")
    check("比例压：长短关系还在（最长 > 次长 > 最短）",
          g_prop[0] > g_prop[1] > g_prop[2], True, str([round(g) for g in g_prop[:3]]))
    check("平压：全被压成一样长（这正是要避免的）",
          len(set(round(g) for g in g_flat[:3])), 1, str([round(g) for g in g_flat[:3]]))

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
