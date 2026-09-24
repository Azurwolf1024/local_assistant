"""基频（F0）跟踪 + 音区守卫判定的离线自测（不加载模型、不出声，秒级）。

为什么值得单测（全是踩出来的教训）：
* 尺子错一次，结论就错一次：自相关版跟踪器把**真人参考音**量成 150/280Hz 来回翻，
  差点把「量错了」当成「模型真的忽高忽低」。所以验尺子必须用**已知答案**的信号：
  滑音（要跟得上）、二次谐波陷阱（不能锁到高八度）、弱基频（不能丢掉低八度）、
  擦音/静音（必须判成清音）；
* 「停顿」不是「低沉」：句内静音只能降低有声率，**不能**改变中位音高；
* 量不出来就别裁：音区守卫在量不到音高时必须返回 None → 不重采（白重采=白等一遍）。

    python scripts/test_pitch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.tts import pitch as pk  # noqa: E402
from voice_loop.tts.zipvoice_tts import (  # noqa: E402
    pitch_guard_verdict,
    pitch_target,
    register_adoptable,
)

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


def check_close(name: str, got: float, want: float, tol: float) -> None:
    ok = abs(float(got) - float(want)) <= tol
    print(f"  {'√' if ok else '×'} {name}: {got:.2f}（期望 {want:.2f} ±{tol}）")
    if not ok:
        FAILED.append(name)


def tone(freq: float, seconds: float, amp: float = 0.3, rate: int = 24000) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def glide(f_lo: float, f_hi: float, seconds: float, rate: int = 24000) -> np.ndarray:
    """对数滑音（半音均匀变化）——跟踪器的频率精度直接看它。"""
    t = np.arange(int(rate * seconds)) / rate
    f = f_lo * (f_hi / f_lo) ** (t / seconds)
    return 0.3 * np.sin(np.cumsum(2 * np.pi * f / rate))


def noise(seconds: float, amp: float = 0.1, rate: int = 24000, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp * rng.normal(0, 1, int(rate * seconds))


def voiced_pct(sig: np.ndarray, rate: int = 24000) -> float:
    return float(pk.stats(pk.f0_track(sig, rate)[1])["voiced_pct"])


def main() -> int:
    print("=" * 70)
    print(" 基频跟踪 / 音区守卫 自测（纯离线）")
    print("=" * 70)

    print("\n[1] 滑音精度：对数滑音必须跟得上（≤1 半音）")
    for lo, hi, sec in ((120.0, 240.0, 3.0), (90.0, 300.0, 2.0), (200.0, 130.0, 2.0)):
        sig = glide(lo, hi, sec)
        _, f0 = pk.f0_track(sig, 24000)
        t = np.arange(len(f0)) * 0.01 + 0.02
        want = lo * (hi / lo) ** np.clip(t / sec, 0, 1)
        err = np.abs(12 * np.log2(f0 / want))
        err = err[np.isfinite(err)]
        check(f"{lo:.0f}→{hi:.0f}Hz 滑音：最差 {np.max(err):.2f} 半音", float(np.max(err)) <= 1.0)

    print("\n[2] 二次谐波陷阱：基频很弱、二次谐波很强 → 不能锁到高八度")
    t2 = np.arange(24000) / 24000
    trap = 0.10 * np.sin(2 * np.pi * 130 * t2) + 0.30 * np.sin(2 * np.pi * 260 * t2)
    check("0.10×130Hz + 0.30×260Hz → 130Hz",
          round(pk.stats(pk.f0_track(trap, 24000)[1])["f0_med"]), 130)
    weak = 0.08 * np.sin(2 * np.pi * 90 * t2) + 0.20 * np.sin(2 * np.pi * 180 * t2) \
        + 0.10 * np.sin(2 * np.pi * 270 * t2)
    check("0.08×90Hz + 0.20×180Hz → 90Hz",
          round(pk.stats(pk.f0_track(weak, 24000)[1])["f0_med"]), 90)

    print("\n[3] 清音 / 静音：擦音和数字静音都必须判成清音（有声率 ≈ 0）")
    fric = np.convolve(noise(2.0), [1.0, -1.0], "same")  # 高通白噪 ≈ 擦音
    check(f"擦音有声率 {voiced_pct(fric):.0f}%", voiced_pct(fric) < 5.0)
    check(f"静音有声率 {voiced_pct(np.zeros(24000)):.0f}%", voiced_pct(np.zeros(24000)) < 1.0)
    quiet = pk.f0_median(np.zeros(24000), 24000)
    check("静音的 f0_median 必须是 None（量不出来不裁）", quiet is None, detail=str(quiet))

    print("\n[4] 采样率与量纲：24k / 22.05k / 44.1k 与 int16/float 都要同答案")
    for rate in (22050, 24000, 44100):
        sig = tone(180.0, 1.0, 0.3, rate)
        got = pk.stats(pk.f0_track(sig, rate)[1])["f0_med"]
        check(f"{rate}Hz 采样：180Hz 音（误差 {abs(pk.semitone(got, 180.0)):.2f} 半音）",
              abs(pk.semitone(got, 180.0)) <= 0.2)
    pcm16 = (tone(200.0, 1.0, 0.3, 24000) * 32767).astype(np.int16)
    check("int16 输入：200Hz 音", round(pk.stats(pk.f0_track(pcm16, 24000)[1])["f0_med"]), 200)
    check("to_float(int16) 落在 [-1,1]", float(np.max(np.abs(pk.to_float(pcm16)))) <= 1.0)
    check("to_float 对已经是浮点的输入不重复缩放",
          abs(float(np.max(np.abs(pk.to_float(tone(200.0, 0.5, 0.3))))) - 0.3) < 1e-6)

    print("\n[5] 停顿不是低沉：句内插静音只降有声率，不改中位音高")
    a = tone(190.0, 0.8)
    gap = np.zeros(24000)
    b = tone(190.0, 0.8)
    joined = np.concatenate([a, gap, b])
    s_a = pk.stats(pk.f0_track(a, 24000)[1])
    s_j = pk.stats(pk.f0_track(joined, 24000)[1])
    check_close("插 1 秒静音后中位音高不变", s_j["f0_med"], s_a["f0_med"], 1.0)
    check(f"有声率下降（{s_a['voiced_pct']:.0f}% → {s_j['voiced_pct']:.0f}%）",
          s_j["voiced_pct"] < s_a["voiced_pct"])

    print("\n[6] 指标能不能抓到「异常的高亢 / 低沉」")
    steady = tone(200.0, 2.0)
    jumpy = np.concatenate([tone(200.0, 0.8), tone(300.0, 1.2)])   # 大半句拔高 = 应该抓到
    s_steady = pk.stats(pk.f0_track(steady, 24000)[1])
    s_jumpy = pk.stats(pk.f0_track(jumpy, 24000)[1])
    check(f"平直音的摆幅很小（{s_steady['iqr_st']:.2f} 半音）", s_steady["iqr_st"] < 1.0)
    # ★注意 IQR 是四分位差★：只拔高 25% 的时长的句子，IQR 仍会是 0（分位点恰好落在边界上），
    # 所以「一段拔高」要看 sustained/离群，不能只看摆幅。
    check(f"拔高音的摆幅很大（{s_jumpy['iqr_st']:.2f} 半音）", s_jumpy["iqr_st"] > 3.0)
    check(f"拔高音的中位差 +{pk.semitone(s_jumpy['f0_med'], s_steady['f0_med']):.1f} 半音",
          pk.semitone(s_jumpy["f0_med"], s_steady["f0_med"]) > 6.0)

    print("\n[7] 音区守卫的量尺：register_st 要量得准、量不到要给 None")
    flat = tone(200.0, 1.5)
    check_close("200Hz 相对靶子 200Hz = 0 半音", pk.register_st(flat, 24000, 200.0), 0.0, 0.2)
    check_close("200Hz 相对靶子 178.18Hz = +2 半音",
                pk.register_st(flat, 24000, 200.0 / 2 ** (2 / 12)), 2.0, 0.2)
    check("静音 → None", pk.register_st(np.zeros(24000), 24000, 200.0) is None)
    check("白噪 → None", pk.register_st(noise(1.5), 24000, 200.0) is None)
    check("靶子给 0 或负数 → None", pk.register_st(flat, 24000, 0.0) is None)

    print("\n[8] 守卫判定（纯函数）：只在该重采时重采")
    check("偏 1.4 / 限 1.5 / 还有下一遍 → 不重采", pitch_guard_verdict(1.4, 1.5, 0, 2), False)
    check("偏 1.6 / 限 1.5 / 还有下一遍 → 重采", pitch_guard_verdict(1.6, 1.5, 0, 2), True)
    check("-1.9 / 限 1.5 / 还有下一遍 → 重采（低沉也算）", pitch_guard_verdict(-1.9, 1.5, 0, 2), True)
    check("已经是最后一遍 → 不再重采（防死循环）", pitch_guard_verdict(3.0, 1.5, 1, 2), False)
    check("只准采一遍 → 永不重采", pitch_guard_verdict(3.0, 1.5, 0, 1), False)
    check("量不出来（None）→ 不重采", pitch_guard_verdict(None, 1.5, 0, 2), False)
    check("阈值为 0（关）→ 不重采", pitch_guard_verdict(3.0, 0.0, 0, 2), False)

    print("\n[9] 小工具：semitone / median_filter / local_median 的行为")
    check_close("八度 = 12 半音", pk.semitone(200.0, 100.0), 12.0, 1e-9)
    check("semitone 遇 0 → NaN", pk.semitone(0.0, 100.0) != pk.semitone(0.0, 100.0))
    f0 = np.array([100.0, np.nan, np.nan, 300.0, np.nan])
    filt = pk.median_filter(f0, 3)
    check("中位滤波不把 NaN 变成数字", bool(np.isnan(filt[1]) and np.isnan(filt[2]) and np.isnan(filt[4])))
    spike = pk.median_filter(np.array([100.0, 100.0, 200.0, 100.0, 100.0]), 3)
    check("单帧毛刺被中位滤波抹掉", bool(np.all(spike == 100.0)))
    long_hi = pk.median_filter(np.array([100.0, 100.0, 100.0, 100.0, 100.0, 300.0, 300.0, 300.0, 300.0, 300.0]), 3)
    check("持续的高音不会被当成毛刺抹掉", float(long_hi[-1]), 300.0)
    check("全 NaN 输入不炸", pk.stats(np.full(10, np.nan))["voiced_pct"], 0.0)
    # ★靶子怎么选★：实测不同模型/参考的整体音区会系统性偏移 1.5~2.0 半音，
    # 拿参考当靶子会在第一句就误判重采（白慢一倍）；用户听到的「异常」是句与句不一致。
    print("\n[10] 音区基准：第一句放宽、之后用自己的中位、离参考太远不进基准")
    t0, l0 = pitch_target([], 180.0, 1.5)
    check_close("没有样本 → 靶子 = 参考音 180Hz", t0, 180.0, 1e-9)
    check_close("没有样本 → 阈值放宽到 1.5+1.0", l0, 2.5, 1e-9)
    t1, l1 = pitch_target([188.0, 190.0, 186.0], 180.0, 1.5)
    check_close("有样本 → 靶子 = 最近几句的中位", t1, 188.0, 1e-9)
    check_close("有样本 → 阈值回到 1.5", l1, 1.5, 1e-9)
    check_close("中位抗单句离群（掺一个 210 也不会跑）", pitch_target([186.0, 188.0, 190.0, 210.0], 180.0, 1.5)[0], 189.0, 1e-9)
    check("差 1.6 半音的那句会被拦下来重采",
          pitch_guard_verdict(pk.semitone(190.0 * 2 ** (1.6 / 12), 190.0), l1, 0, 2), True)
    check("差 1.4 半音的那句放过",
          pitch_guard_verdict(pk.semitone(190.0 * 2 ** (1.4 / 12), 190.0), l1, 0, 2), False)
    check("离参考 +1.8 半音（拼接参考那种）→ 能进基准", register_adoptable(180.0 * 2 ** (1.8 / 12), 180.0), True)
    check("离参考 +3.1 半音 → 不进基准（防慢漂移）", register_adoptable(180.0 * 2 ** (3.1 / 12), 180.0), False)
    check("没参考音时也能进基准", register_adoptable(190.0, None), True)
    check("量不出来（None）不进基准", register_adoptable(None, 180.0), False)
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
