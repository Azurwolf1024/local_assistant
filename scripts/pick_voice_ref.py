"""声线体检：毛不毛，先看**参考音频和素材**，而不是先怀疑模型/量化/步数。

    python scripts/pick_voice_ref.py                       # 所有角色的素材体检 + 候选推荐
    python scripts/pick_voice_ref.py --who amiya           # 只看一个角色
    python scripts/pick_voice_ref.py --cross kaltsit amiya # 模型×参考 2×2 交叉实验

为什么是这么个工具（2026-09-24 的实测链，详见 docs/ENGINEERING_LOG.md 16.7）：

1. 先量「整条高频占比」（>6 kHz 能量占全带）——沙沙声的代理指标；
2. 但整条指标**受文本影响**（「是/四/十/s/x」多的句子天然偏高），而且同一配置换一次
   采样就能差 3 倍（那是噪声，不是差异）。所以加了第二个指标：
   **安静帧高频占比** = 只取能量最低的 25% 帧（停顿、塞音成阻、气口），那里的高频
   只可能是**底噪/气声**，跟句子无关，两组之间才可比。
   实测：凯尔希 7.61% vs 阿米娅 **36.39%**（安静帧占比两边都是 ~25%，同一把尺子）。
   → 「阿米娅听着毛」的根在**素材底噪**，不在模型结构、不在 int8、不在步数。
3. `--cross` 再把「模型」和「参考」分开：同一模型换参考、同一参考换模型，比谁的影响大。
   实测（长句 / 4 步 / int8）：换参考 ×1.8~2.5，换模型 ×1.1~1.6
   → **推理时那条参考是噪声的「载体」**：参考自己只差 1.4 倍，到输出里被放大成 2.5 倍。

所以顺序是：先挑/处理素材与参考，再谈精度和步数。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ab_clone_model import TEXTS, hf_ratio, lead_silence, rms_swing  # noqa: E402

PERSONA_DIR = ROOT / "models" / "tts" / "zipvoice" / "personas"
DATA_DIR = ROOT / "data" / "personas"
OUT_DIR = ROOT / "sessions" / "voice_noise"

# 安静帧的定义：能量最低的这百分之几帧
FRAME_MS = 30.0
QUIET_PCT = 25.0
# 参考音频的舒适区：太短音色学不全，太长每次合成都要多付「参考重编码」的钱（≈0.36×秒数）
MIN_REF_S, MAX_REF_S = 3.5, 8.0


# ----------------------------------------------------------------- 指标
def quiet_hf(path: Path) -> tuple[float, float, float]:
    """返回 (整条高频占比, 安静帧高频占比, 安静帧占比)，单位 %。"""
    x, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = (x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)).astype(np.float64)
    hop = max(1, int(rate * FRAME_MS / 1000))
    n = mono.size // hop
    if n < 4:
        return float("nan"), float("nan"), 0.0
    frames = mono[: n * hop].reshape(n, hop)
    freqs = np.fft.rfftfreq(hop, 1.0 / rate)
    high = freqs > 6000
    rms = np.sqrt((frames**2).mean(axis=1))
    spec = np.abs(np.fft.rfft(frames * np.hanning(hop), axis=1)) ** 2
    total = spec.sum(axis=1) + 1e-12
    per_frame = spec[:, high].sum(axis=1) / total
    quiet = (rms <= np.percentile(rms, QUIET_PCT)) & (rms > 1e-6)
    return (
        float(spec[:, high].sum() / total.sum() * 100),
        float(per_frame[quiet].mean() * 100) if quiet.any() else float("nan"),
        float(quiet.mean() * 100),
    )


def measure(path: Path) -> dict | None:
    """一条音频的全部体检数据。"""
    try:
        x, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  读不了 {path.name}：{exc}")
        return None
    mono = x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)
    pcm = np.clip(mono * 32768.0, -32768, 32767).astype(np.int16)
    whole, quiet, share = quiet_hf(path)
    swing, _peak = rms_swing(pcm, rate)
    return {
        "name": path.name, "path": path, "seconds": pcm.size / rate,
        "hf": whole, "quiet_hf": quiet, "quiet_share": share,
        "swing": swing, "lead": lead_silence(pcm, rate), "rate": rate,
    }


# ----------------------------------------------------------------- 模式一：素材体检
def survey(who: str) -> int:
    files = sorted((DATA_DIR / who).glob("*.wav"))
    if not files:
        print(f"{who}：没有素材（{DATA_DIR / who}）")
        return 1
    rows = [m for m in (measure(p) for p in files) if m]
    quiet = [r["quiet_hf"] for r in rows if r["quiet_hf"] == r["quiet_hf"]]
    print(f"\n{'=' * 92}")
    print(f"{who}：{len(rows)} 条素材　"
          f"安静帧高频占比 中位 {statistics.median(quiet):.2f}%（越小越干净）　"
          f"整条中位 {statistics.median(r['hf'] for r in rows):.2f}%")
    print("=" * 92)
    band = [r for r in rows if MIN_REF_S <= r["seconds"] <= MAX_REF_S]
    print(f"\n  ★ 候选参考（{MIN_REF_S}~{MAX_REF_S} 秒，按安静帧高频占比升序 = 最干净在前）")
    print(f"    {'文件':<22} {'时长':>6} {'安静帧高频':>10} {'整条高频':>9} "
          f"{'波动':>7} {'头静音':>7}")
    for r in sorted(band, key=lambda r: r["quiet_hf"])[:6]:
        print(f"    {r['name']:<22} {r['seconds']:5.2f}s {r['quiet_hf']:9.2f}% "
              f"{r['hf']:8.2f}% {r['swing']:6.2f}dB {r['lead']:6.2f}s")
    print(f"\n  最脏的 3 条（别拿它们当参考）")
    for r in sorted(band, key=lambda r: -r["quiet_hf"])[:3]:
        print(f"    {r['name']:<22} {r['seconds']:5.2f}s {r['quiet_hf']:9.2f}%")
    print("\n  怎么用：把候选填进人格文件的 voice_ref，或者先试听——")
    print("    python scripts/tts_clone_probe.py --ref <wav> --compare")
    print("  ★先听后改★：这几个数字只能筛掉明显毛的，定不了好不好听。")
    return 0


# ----------------------------------------------------------------- 模式二：交叉实验
def cross(model_a: str, model_b: str, steps: int) -> int:
    """模型 × 参考 的 2×2：分清「模型毛」还是「参考毛」。"""
    from voice_loop.settings import load_settings
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    refs: dict[str, Path] = {}
    for who in (model_a, model_b):
        best = None
        for p in sorted((DATA_DIR / who).glob("*.wav")):
            m = measure(p)
            if not m or not (MIN_REF_S <= m["seconds"] <= MAX_REF_S):
                continue
            if best is None or m["quiet_hf"] < best[0]:
                best = (m["quiet_hf"], p, m)
        if best is None:
            print(f"{who}：没有 {MIN_REF_S}~{MAX_REF_S} 秒的素材，跳过")
            return 1
        refs[who] = best[1]
        print(f"{who} 选出的最干净参考：{best[1].name}（安静帧高频 {best[0]:.2f}%，"
              f"{best[2]['seconds']:.2f}s）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = TEXTS["long"]
    print(f"\n模型 × 参考 交叉实验（{steps} 步 / 长句 / 同一台机器）")
    print(f"{'模型':<10} {'参考来自':<10} {'整条高频':>9} {'安静帧高频':>10} {'合成':>7}")
    grid: dict[tuple[str, str], dict] = {}
    for model_who in (model_a, model_b):
        for ref_who in (model_a, model_b):
            settings = load_settings()
            settings.tts.backend = "zipvoice"
            settings.tts.clone_dir = str(PERSONA_DIR / model_who)
            settings.tts.clone_audio = str(refs[ref_who])
            settings.tts.clone_text = ""
            settings.tts.clone_steps = steps
            engine = ZipVoiceTts(settings)
            for _ in engine.synth("预热。"):
                pass
            t0 = __import__("time").perf_counter()
            pcm = np.concatenate([p for _r, p in engine.synth(text)])
            cost = __import__("time").perf_counter() - t0
            name = f"{model_who}_x_{ref_who}ref.wav"
            sf.write(str(OUT_DIR / name), pcm.astype(np.float32) / 32768.0, engine.sample_rate)
            whole, quiet, _share = quiet_hf(OUT_DIR / name)
            grid[(model_who, ref_who)] = {"hf": whole, "quiet": quiet, "cost": cost}
            print(f"{model_who:<10} {ref_who:<10} {whole:8.2f}% {quiet:9.2f}% {cost:6.2f}s  {name}")

    print()
    for ref_who in (model_a, model_b):
        a, b = grid.get((model_a, ref_who)), grid.get((model_b, ref_who))
        if a and b:
            print(f"  同参考（{ref_who}）：换模型 {a['hf']:.2f}% → {b['hf']:.2f}%（×{b['hf'] / a['hf']:.1f}）")
    for model_who in (model_a, model_b):
        a, b = grid.get((model_who, model_a)), grid.get((model_who, model_b))
        if a and b:
            print(f"  同模型（{model_who}）：换参考 {a['hf']:.2f}% → {b['hf']:.2f}%（×{b['hf'] / a['hf']:.1f}）")
    print("\n  判读：换参考的倍数明显大于换模型 → 先把参考/素材弄干净，别急着加步数或换精度。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="声线体检：毛不毛先看参考和素材")
    ap.add_argument("--who", default="", help="只看某个角色（默认全部）")
    ap.add_argument("--cross", nargs=2, metavar=("模型A", "模型B"),
                    help="模型×参考 交叉实验，例如 --cross kaltsit amiya")
    ap.add_argument("--steps", type=int, default=4, help="交叉实验的流匹配步数")
    args = ap.parse_args()

    if args.cross:
        return cross(args.cross[0], args.cross[1], args.steps)
    whos = [args.who] if args.who else sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    rc = 0
    for who in whos:
        rc |= survey(who)
    return rc


if __name__ == "__main__":
    sys.exit(main())
