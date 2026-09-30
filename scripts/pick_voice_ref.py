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
   ⚠️ 2026-09-25 更正：旧口径选中的帧是 **−78 dBFS 的数字静音**（HF 92%），
   量的是听不见的东西 → 换相对电平口径后两条参考是 **0.37% / 1.06%**，都干净。
   沙沙声其实在**模型侧**，能稳定压下去的是输出侧的高架（见日志 18.3）。
3. `--cross` 再把「模型」和「参考」分开：同一模型换参考、同一参考换模型，比谁的影响大。
   实测（长句 / 4 步 / int8）：换参考 ×1.8~2.5，换模型 ×1.1~1.6
   → **推理时那条参考是噪声的「载体」**：参考自己只差 1.4 倍，到输出里被放大成 2.5 倍。

所以顺序是：先挑/处理素材与参考，再谈精度和步数。
"""

from __future__ import annotations

import argparse
import itertools
import statistics
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ab_clone_model import (  # noqa: E402
    TEXTS,
    audible_quiet_hf,
    hf_ratio,
    lead_silence,
    rms_swing,
)
from voice_loop.tts import pitch as pitchkit  # noqa: E402
from voice_loop.tts import speaker as spk     # noqa: E402

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
    """返回 (整条高频占比, 安静帧高频占比, 安静帧占比)，单位 %。

    ★2026-09-25 改了口径★：旧版取「能量最低 25% 的帧」，实测那些帧是 −78 dBFS 的
    数字静音（HF 92%），量的是耳朵听不到的东西，于是得出「阿米娅素材底噪大 4.5 倍」
    这个**假结论**。现在用相对口径：安静帧 = 比这句自己的语音电平低 45~15 dB，
    且排除数字零（实现在 ab_clone_model.py，与 A/B 脚本共用一把尺子）。
    """
    x, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = (x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)).astype(np.float32)
    n = mono.size // max(1, int(rate * FRAME_MS / 1000))
    if n < 4:
        return float("nan"), float("nan"), 0.0
    whole = hf_ratio(np.clip(mono * 32768.0, -32768, 32767).astype(np.int16), rate) * 100
    quiet, share = audible_quiet_hf(mono, rate)
    return float(whole), float(quiet), float(share)


def measure(path: Path) -> dict | None:
    """一条音频的全部体检数据。

    ★两把尺子，各管一件事★：
    - 干净度（高频/波动/头静音）→ 决定有没有沙沙声；
    - **音区与摆幅**（YIN 量的 f0）→ 决定这条参考会把输出带到哪个「语气」音区，
      以及它自己的情绪起伏大小（摆幅大 = 念白有起伏，适合当「有情绪」的参考）。
    """
    try:
        x, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  读不了 {path.name}：{exc}")
        return None
    mono = x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)
    pcm = np.clip(mono * 32768.0, -32768, 32767).astype(np.int16)
    whole, quiet, share = quiet_hf(path)
    swing, _peak = rms_swing(pcm, rate)
    _, f0 = pitchkit.f0_track(mono, rate)
    pitch = pitchkit.stats(f0)
    return {
        "name": path.name, "path": path, "seconds": pcm.size / rate,
        "hf": whole, "quiet_hf": quiet, "quiet_share": share,
        "swing": swing, "lead": lead_silence(pcm, rate), "rate": rate,
        "f0_med": float(pitch["f0_med"]), "iqr_st": float(pitch["iqr_st"]),
        "sustained_up": float(pitch["sustained_up"]),
        "sustained_dn": float(pitch["sustained_dn"]),
        "voiced_pct": float(pitch["voiced_pct"]),
        "sim": float("nan"),
    }


def style_picks(rows: list[dict], want: int = 4) -> list[tuple[str, dict]]:
    """从素材里挑几条「互相听得出来不一样」的。返回 ``[(风格键, 行)]``。

    判据都是量出来的（不再靠感觉）：
    1. **能当参考**：3.5~8 秒、像不像她 >= SAME_SPEAKER、安静帧高频不过分；
    2. **可分辨**：按**音区中位**排序，低/中/高各取一条 → 这三条会把输出带到不同音区；
    3. 再补一条**摆幅最大**的（摆幅 ≈ 情绪起伏）当「有情绪」那档。
    ★风格名字是占位★：听完之后把 low/mid/high/lively 换成你真听到的语气。
    """
    pool = [r for r in rows
            if MIN_REF_S <= r["seconds"] <= MAX_REF_S
            and r["f0_med"] == r["f0_med"]
            and (r["sim"] != r["sim"] or r["sim"] >= spk.SAME_SPEAKER)]
    if not pool:
        return []
    quiet_ok = statistics.median([r["quiet_hf"] for r in pool
                                  if r["quiet_hf"] == r["quiet_hf"]] or [0.0])
    pool = [r for r in pool if r["quiet_hf"] <= quiet_ok + 3.0] or pool   # 先别挑明显毛的

    by_pitch = sorted(pool, key=lambda r: r["f0_med"])
    picks: list[tuple[str, dict]] = []
    for key, pos in (("low", 0), ("mid", len(by_pitch) // 2), ("high", len(by_pitch) - 1)):
        got = by_pitch[pos]
        if all(got["name"] != r["name"] for _k, r in picks):
            picks.append((key, got))
    taken = {r["name"] for _k, r in picks}
    rest = [r for r in pool if r["name"] not in taken]
    if rest:
        # 摆幅最大的一条从**没选中的**里挑（否则它往往又被 low/high 占掉）
        live = max(rest, key=lambda r: (r["iqr_st"] if r["iqr_st"] == r["iqr_st"] else -1))
        picks.append(("lively", live))
    return picks[:max(1, want)]


# ----------------------------------------------------------------- 模式一：素材体检
def survey(who: str) -> int:
    files = sorted((DATA_DIR / who).glob("*.wav"))
    if not files:
        print(f"{who}：没有素材（{DATA_DIR / who}）")
        return 1
    rows = [m for m in (measure(p) for p in files) if m]

    # ★声纹★：同一套素材的质心当基准，量「每条自己像不像这个角色」。
    # 换参考时最容易踩的坑就是「挑了一条其实不太像她、但很干净的素材」。
    emb = spk.load()
    if emb is None:
        print(f"  · 跳过「像不像她」：{spk.missing().strip().splitlines()[0]}")
    else:
        vecs = {}
        for r in rows:
            got = emb.embedding_file(r["path"])
            if got is not None:
                vecs[r["name"]] = got
        base = spk.centroid(list(vecs.values()))
        for r in rows:
            got = vecs.get(r["name"])
            r["sim"] = spk.cosine(got, base) if got is not None else float("nan")
        if vecs:
            inside = sorted(spk.cosine(a, b) for a, b in itertools.combinations(vecs.values(), 2))
            print(f"  声纹：{len(vecs)}/{len(rows)} 条算出向量；素材内部两两中位 "
                  f"{inside[len(inside) // 2]:.3f}（这是「像她」的手感基准）")

    quiet = [r["quiet_hf"] for r in rows if r["quiet_hf"] == r["quiet_hf"]]
    print(f"\n{'=' * 104}")
    print(f"{who}：{len(rows)} 条素材　"
          f"安静帧高频占比 中位 {statistics.median(quiet):.2f}%（越小越干净）　"
          f"整条中位 {statistics.median(r['hf'] for r in rows):.2f}%")
    print("=" * 104)
    band = [r for r in rows if MIN_REF_S <= r["seconds"] <= MAX_REF_S]
    print(f"\n  ★ 候选参考（{MIN_REF_S}~{MAX_REF_S} 秒，按安静帧高频占比升序 = 最干净在前）")
    print(f"    {'文件':<22} {'时长':>6} {'安静帧高频':>10} {'整条高频':>9} "
          f"{'波动':>7} {'头静音':>7} {'音区':>7} {'像不像她':>8}")
    for r in sorted(band, key=lambda r: r["quiet_hf"])[:8]:
        print(f"    {r['name']:<22} {r['seconds']:5.2f}s {r['quiet_hf']:9.2f}% "
              f"{r['hf']:8.2f}% {r['swing']:6.2f}dB {r['lead']:6.2f}s "
              f"{r['f0_med']:5.0f}Hz {r['sim']:7.3f}")
    print(f"\n  最脏的 3 条（别拿它们当参考）")
    for r in sorted(band, key=lambda r: -r["quiet_hf"])[:3]:
        print(f"    {r['name']:<22} {r['seconds']:5.2f}s {r['quiet_hf']:9.2f}%")

    # ★风格体检★：把「音区 / 摆幅」摆出来，一眼看出素材里有没有可分辨的语气层次
    print(f"\n  ★ 语气层次（按音区中位排序；摆幅 = 情绪起伏）")
    print(f"    {'文件':<22} {'音区':>7} {'摆幅(四分位)':>12} {'持续最远':>10} "
          f"{'有声占比':>8} {'时长':>6}")
    for r in sorted([r for r in rows if r["f0_med"] == r["f0_med"]],
                    key=lambda r: r["f0_med"])[:12]:
        print(f"    {r['name']:<22} {r['f0_med']:5.0f}Hz {r['iqr_st']:11.2f}半音 "
              f"{r['sustained_up']:9.1f}st {r['voiced_pct']:7.1f}% {r['seconds']:5.2f}s")

    picks = style_picks(rows)
    if picks:
        meds = [r["f0_med"] for r in band if r["f0_med"] == r["f0_med"]]
        span = pitchkit.semitone(max(meds), min(meds)) if len(meds) >= 2 else float("nan")
        print(f"\n  ★ 建议先拿这几条当「风格参考」（互相听得出来不一样，且都像她）")
        for key, r in picks:
            print(f"    {key:<7} {r['name']:<22} 音区 {r['f0_med']:.0f}Hz  "
                  f"摆幅 {r['iqr_st']:.2f}半音  像不像她 {r['sim']:.3f}  "
                  f"{r['seconds']:.1f}s")
        if span == span:
            print(f"    音区跨度：候选里最低 {min(meds):.0f}Hz ~ 最高 {max(meds):.0f}Hz "
                  f"= {span:.2f} 个半音")
        if span == span and span < 1.5:
            # ★把「素材本来就没层次」这种情况说出来★：换参考只能给出细微差别，
            # 想要明显不同的语气，得另找/另录带情绪的素材（或换带风格条件的模型）。
            print("    ⚠ 跨度不到 1.5 个半音 —— 这套素材**本身语气偏单一**："
                  "换参考只能给出细微差别，\n      想要明显不同的情绪，得另找带情绪的素材"
                  "（或换支持风格条件的模型）。")
        rel = f"data/personas/{who}/"
        print(f"\n    贴进 data/personas/{who}.json（★名字听完再改成你真听到的语气★）：")
        print("      \"voice_refs\": {")
        for i, (key, r) in enumerate(picks):
            print(f"        \"{key}\": \"{rel}{r['name']}\"{',' if i < len(picks) - 1 else ''}")
        print("      }")
    else:
        print(f"\n  · 挑不出风格参考（{MIN_REF_S}~{MAX_REF_S} 秒、且像她的素材太少）")

    print("\n  怎么用：把候选填进人格文件的 voice_ref，或者先试听——")
    print("    python scripts/tts_clone_probe.py --ref <wav> --compare")
    print("  想量「合出来的像不像她」（声纹）：")
    print(f"    python scripts/spk_check.py <合成的wav> --ref data/personas/{who}")
    print("  ★先听后改★：这些数字能筛掉明显不行的，定不了好不好听。")
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
