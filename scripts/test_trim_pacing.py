"""输出静音裁剪（voice_loop/tts/pacing.py）的离线测试。

要守住的底线：
1. **不把话剪没**——语音帧一个都不能少；
2. **幂等**——本来就没静音的音频，裁一遍跟没裁一样；
3. 该剪的剪到位（开头死静音、结尾多余静音）；
4. 句内停顿**默认不动**，只有显式打开才压；
5. 异常输入（空、纯静音、极短、参数为 0）不能抛也不能乱改。

    python scripts/test_trim_pacing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import TtsConfig  # noqa: E402
from voice_loop.tts.pacing import PacingFixer, trim_silence  # noqa: E402

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


RATE = 24000


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(RATE * ms / 1000), dtype=np.int16)


def tone(ms: int, freq: float = 220.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t) * 32767.0).astype(np.int16)


def ms(samples: np.ndarray) -> float:
    return samples.size / RATE * 1000.0


def leading_silence_ms(x: np.ndarray) -> float:
    """开头连续静音长度（按 10ms RMS 帧判，跟被测代码同一套判据）。"""
    hop = RATE // 100
    n = x.size // hop
    if n == 0:
        return 0.0
    rms = np.sqrt((x[: n * hop].astype(np.float32).reshape(n, hop) ** 2).mean(axis=1))
    idx = np.nonzero(rms > max(float(rms.max()) * 0.05, 1e-4))[0]
    return 0.0 if idx.size == 0 else idx[0] * 10.0


def speech_frames(x: np.ndarray) -> int:
    hop = RATE // 100
    n = x.size // hop
    rms = np.sqrt((x[: n * hop].astype(np.float32).reshape(n, hop) ** 2).mean(axis=1))
    return int((rms > max(float(rms.max()) * 0.05, 1e-4)).sum())


print("== 1. 基本裁剪 ==")
src = np.concatenate([silence(600), tone(500), silence(400)])
out = trim_silence(src, RATE)
check(round(ms(out)), round(ms(src) - 600 - 400 + 40 + 80), "600ms 头 + 400ms 尾 → 只留 40/80ms")
check(round(leading_silence_ms(out)), 40, "开头静音变成 40ms")
check(speech_frames(out), speech_frames(src), "语音帧数不变（没剪到话）")
check_true(ms(out) < ms(src) - 800, f"总时长明显变短（{ms(src):.0f}ms → {ms(out):.0f}ms）")

print("\n== 2. 幂等：本来就没有静音 ==")
plain = tone(500)
same = trim_silence(plain, RATE)
check_true(abs(ms(same) - ms(plain)) <= 10, f"没有静音时长度不变（{ms(plain):.0f} → {ms(same):.0f}ms）")
again = trim_silence(same, RATE)
check(same.size, again.size, "再裁一次长度不变")

print("\n== 3. 幂等：裁过的再裁 ==")
once = trim_silence(src, RATE)
twice = trim_silence(once, RATE)
check_true(abs(ms(twice) - ms(once)) <= 10, f"{ms(once):.0f}ms → {ms(twice):.0f}ms")
check(round(leading_silence_ms(twice)), 40, "第二次裁完开头仍是 40ms（不会越裁越少）")

print("\n== 4. 句内停顿默认不动 ==")
with_pause = np.concatenate([silence(300), tone(300), silence(700), tone(300), silence(200)])
kept = trim_silence(with_pause, RATE)
# 只削首尾：-300-200+40+80
check(round(ms(kept)), round(ms(with_pause) - 380), "句内 700ms 停顿保留")
check_true(abs(ms(kept) - ms(with_pause)) > 300, "确实裁掉了首尾")

print("\n== 5. 句内停顿可以压（显式打开） ==")
squeezed = trim_silence(with_pause, RATE, max_pause_ms=400, min_pause_ms=200)
check(round(ms(squeezed)), round(ms(kept) - 500), "700ms 停顿 → 200ms（少 500ms）")
check(speech_frames(squeezed), speech_frames(with_pause), "压停顿不影响语音帧")

print("\n== 6. 参数边界 ==")
check_true(trim_silence(np.zeros(0, dtype=np.int16), RATE).size == 0, "空输入 → 空输出")
check(trim_silence(tone(20), RATE).size, tone(20).size, "极短输入原样返回")
allsil = silence(500)
check(allsil.size, trim_silence(allsil, RATE).size, "纯静音不裁剪（原样返回，不掩盖上游问题）")
check(src.size, trim_silence(src, 0).size, "采样率为 0 时原样返回")
zero_pad = trim_silence(src, RATE, lead_ms=0, tail_ms=0)
check(round(leading_silence_ms(zero_pad)), 0, "lead_ms=0 时开头不留静音")
check_true(ms(zero_pad) < ms(out), "lead/tail=0 比默认更短")

print("\n== 7. 采样率与 dtype ==")
src22 = np.concatenate([silence(600), tone(500), silence(400)])
out22 = trim_silence(src22, 22050)
check_true(ms(out22) < ms(src22) - 800, "22050Hz 一样能裁")
check(out.dtype, np.dtype("int16"), "输出是 int16")
f = np.concatenate([np.zeros(600 * 24, dtype=np.float32), tone(500).astype(np.float32) / 32768.0])
check(trim_silence(f, RATE).dtype, np.dtype("int16"), "float 输入也能处理")

print("\n== 8. 低电平噪声算静音 ==")
hiss = (np.random.default_rng(0).normal(0, 0.001, 500 * 24) * 32767.0).astype(np.int16)
loud = tone(300)
noisy = np.concatenate([hiss, loud, hiss])
trimmed_noisy = trim_silence(noisy, RATE)
check_true(
    ms(trimmed_noisy) < ms(noisy) - 600,
    f"噪声底被当静音裁掉（{ms(noisy):.0f} → {ms(trimmed_noisy):.0f}ms）",
)

print("\n== 9. PacingFixer ==")
cfg = TtsConfig()
check_true(cfg.trim_output_silence, "配置默认开启裁剪")
check(cfg.trim_lead_ms, 40, "默认 lead = 40ms")
check(cfg.trim_tail_ms, 80, "默认 tail = 80ms")
check(cfg.trim_max_pause_ms, 0, "默认不动句内停顿")
fixer = PacingFixer.from_config(cfg)
check(fixer.enabled, True, "from_config 读到 enabled")
check(fixer.apply(src, RATE).size, out.size, "apply 与 trim_silence 一致")
off = PacingFixer(enabled=False)
check(off.apply(src, RATE).size, src.size, "enabled=False 时原样返回")
check_true("关" in off.describe(), "describe 说明关闭状态")
check_true("首 40ms" in fixer.describe(), "describe 说明当前参数")

print("\n== 10. 过短停顿拉长（trim_min_gap_ms）==")
# 实测背景：「我在，博士。」的逗号停顿只有 60ms，人类在逗号要停 200~400ms
short_gap = np.concatenate([silence(200), tone(300), silence(60), tone(300), silence(200)])
raw = ms(trim_silence(short_gap, RATE))
stretched = ms(trim_silence(short_gap, RATE, min_gap_ms=240))
check(round(stretched - raw), 180, "60ms 停顿 → 240ms（时长 +180ms）")
check(round(ms(trim_silence(short_gap, RATE, min_gap_ms=0))), round(raw), "min_gap_ms=0 时不动（默认）")

long_gap = np.concatenate([silence(200), tone(300), silence(300), tone(300), silence(200)])
check(
    round(ms(trim_silence(long_gap, RATE, min_gap_ms=240))),
    round(ms(trim_silence(long_gap, RATE))),
    "300ms 停顿不会被拉长（只动「过短」的）",
)
check(speech_frames(trim_silence(short_gap, RATE, min_gap_ms=240)), speech_frames(short_gap), "拉长停顿不影响语音帧")

# ★微空隙（字与字之间的自然音渡，10~40ms）绝不能被撑成静音★：
# 无差别撑开会变成「每个字之间都垫一段等长静音」→ 听着卡顿（实测一句话 11 个空隙
# 里 8 个是这种，全撑 = 白加 1.84 秒死气）。
micro = np.concatenate([silence(200), tone(300), silence(30), tone(300), silence(200)])
check(
    round(ms(trim_silence(micro, RATE, min_gap_ms=240))),
    round(ms(trim_silence(micro, RATE))),
    "30ms 微空隙不动（低于门槛 60ms）",
)
check(
    round(ms(trim_silence(micro, RATE, min_gap_ms=240, min_gap_floor_ms=0))) - round(ms(trim_silence(micro, RATE))),
    210,
    "门槛设 0 时才会被撑开（对比：+210ms）",
)
edge = np.concatenate([silence(200), tone(300), silence(70), tone(300), silence(200)])
check(
    round(ms(trim_silence(edge, RATE, min_gap_ms=240))) - round(ms(trim_silence(edge, RATE))),
    170,
    "70ms 刚过门槛 → 拉长到 240ms（+170ms）",
)

print("\n== 11. PacingFixer 带上新参数 ==")
cfg_gap = TtsConfig()
cfg_gap.trim_min_gap_ms = 240
check(PacingFixer.from_config(cfg_gap).min_gap_ms, 240, "from_config 读到 trim_min_gap_ms")
check(PacingFixer.from_config(cfg_gap).min_gap_floor_ms, 60, "from_config 读到门槛默认 60ms")
check_true("拉到 240ms" in PacingFixer.from_config(cfg_gap).describe(), "describe 里说明拉长值")
check_true("60~240ms" in PacingFixer.from_config(cfg_gap).describe(), "describe 里说明门槛")
check(
    round(ms(trim_silence(micro, RATE, min_gap_ms=240, min_gap_floor_ms=90))) ,
    round(ms(trim_silence(micro, RATE))),
    "门槛可以自己调（90ms 时 30ms 空隙仍然不动）",
)

print(f"\n通过 {PASS} / 失败 {FAIL}")
print(f"EXIT={0 if FAIL == 0 else 1}")
sys.exit(0 if FAIL == 0 else 1)
