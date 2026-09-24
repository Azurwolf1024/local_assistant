"""同一句话、同一个参考音频，把「模型精度 × 流匹配步数」拉成一张表，顺手写出 wav 试听。

为什么需要它（2026-09 的实测背景，详见 docs/ENGINEERING_LOG.md 第 15、16 节）：
本项目的角色声线是**从非蒸馏基座微调**出来的（`.zipvoice-src/download/zipvoice`），
而出厂模型是**蒸馏 + int8** 的。于是「沙沙声 / 音量波动」有两个嫌疑：

    ① 非蒸馏模型只跑 4 步 —— 4 步是给出厂蒸馏模型调的，非蒸馏要 16~32 步才收敛；
    ② int8 动态量化 —— `onnx_export.py` 默认会额外出两份 `*_int8.onnx`。

这个脚本把两个变量分开量，别混在一起猜：

    同一个参考音频 · 同一段文本 · 同一台机器
    ├─ 精度：int8 / fp32        （角色目录里两份都装了才能比）
    └─ 步数：4 / 8 / 16 / 32

客观指标（只是筛子，最终还得耳朵定）：
    - 高频占比：>6 kHz 能量占全带的比 —— 沙沙声的代理指标
    - 音量波动：有声帧 RMS 的 dB 标准差 —— 忽大忽小的代理指标
    - 开头死静音：ZipVoice 每段开头会垫一段纯静音（实测 0.58~1.48s），也一并量出来
    - RTF：合成耗时 / 音频时长。★短句这一列天然很夸张★——每次 `synth()` 都要重编码
      一遍参考音频（≈ 0.36 × 参考秒数），这部分是**固定开销、跟精度无关**；
      要比「精度贵多少」请看「合成」那一列的绝对秒数。

用法：
    python scripts/ab_clone_model.py                       # 两个角色全网格
    python scripts/ab_clone_model.py --who amiya --steps 4 16
    python scripts/ab_clone_model.py --text short          # 只跑那句「我在，博士。」

输出：`sessions/ab_clone/<角色>_<微调/出厂>_<精度>_s<步数>_<短/长>.wav` + `report.txt`。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.tts.pacing import FRAME_MS, THRESHOLD, _active_frames  # noqa: E402
from voice_loop.tts.zipvoice_tts import PRECISION_FILES  # noqa: E402

PERSONA_DIR = ROOT / "models" / "tts" / "zipvoice" / "personas"
FACTORY_DIR = ROOT / "models" / "tts" / "zipvoice" / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
OUT_DIR = ROOT / "sessions" / "ab_clone"

# 参考音频：就是人格文件里在用的那两条
REFS = {
    "kaltsit": ROOT / "data" / "personas" / "kaltsit" / "干员报到.wav",
    "amiya": ROOT / "data" / "personas" / "amiya" / "交谈1.wav",
}

TEXTS = {
    # 短句：逗号停顿是 trim_min_gap_ms 那套东西在管，最容易听出「一顿一顿」
    "short": "我在，博士。",
    # 长句：多个标点、多处停顿，最容易听出沙沙声和忽大忽小
    "long": "今天的日程已经排好了，上午九点是机器学习课，下午两点还有组会。",
}


# ---------------------------------------------------------------- 客观指标
def hf_ratio(pcm: np.ndarray, rate: int) -> float:
    """高频（>6 kHz）能量占全带的比 —— 沙沙声的代理指标。"""
    x = pcm.astype(np.float32) / 32768.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    total = float((spec**2).sum()) + 1e-12
    return float((spec[freqs > 6000] ** 2).sum()) / total


def rms_swing(pcm: np.ndarray, rate: int) -> tuple[float, float]:
    """有声帧 RMS 的 dB 标准差（音量波动）+ 峰值。"""
    active = _active_frames(pcm, rate, FRAME_MS, THRESHOLD)
    hop = max(1, int(rate * FRAME_MS / 1000))
    n = pcm.size // hop
    if n == 0:
        return 0.0, 0.0
    rms = np.sqrt((pcm[: n * hop].astype(np.float32).reshape(n, hop) ** 2).mean(axis=1))
    voiced = rms[active[:n]] if active[:n].any() else rms
    voiced = voiced[voiced > 1e-6]
    db = 20 * np.log10(voiced / 32768.0) if voiced.size else np.array([-120.0])
    return float(db.std()), float(np.percentile(rms, 99))


def lead_silence(pcm: np.ndarray, rate: int) -> float:
    """开头那段纯静音有多长（秒）。ZipVoice 每段都会垫一段，长度还随机。"""
    active = _active_frames(pcm, rate, FRAME_MS, THRESHOLD)
    first = int(np.argmax(active)) if active.any() else 0
    return first * FRAME_MS / 1000.0


# ---------------------------------------------------------------- 单次合成
def synth_once(model_dir: Path, ref: Path, text: str, precision: str, steps: int):
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    settings = load_settings()
    settings.tts.backend = "zipvoice"
    settings.tts.clone_dir = str(model_dir)
    settings.tts.clone_audio = str(ref)
    settings.tts.clone_text = ""
    settings.tts.clone_precision = precision
    settings.tts.clone_steps = steps
    engine = ZipVoiceTts(settings)
    for _ in engine.synth("预热。"):  # 第一次调用要建会话，别把冷启动算进去
        pass
    t0 = time.perf_counter()
    pcm = np.concatenate([p for _r, p in engine.synth(text)])
    return pcm, engine.sample_rate, time.perf_counter() - t0


def has_precision(model_dir: Path, precision: str) -> bool:
    return all((model_dir / name).is_file() for name in PRECISION_FILES[precision])


# ---------------------------------------------------------------- 结果表
COLUMNS = ["who", "kind", "precision", "steps", "text", "seconds", "synth_s",
           "rtf", "hf", "swing", "lead", "wav"]


def format_row(r: dict) -> str:
    return (
        f"{r['who']:<8} {r['kind']:<12} {r['precision']:<5} {r['steps']:>4} {r['text']:<6} "
        f"{r['seconds']:5.2f}s {r['synth_s']:5.2f}s {r['rtf']:5.2f} "
        f"{r['hf']:6.2f}% {r['swing']:6.2f}dB {r['lead']:6.2f}s  {r['wav']}"
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    lines = [",".join(COLUMNS)]
    for r in rows:
        lines.append(",".join(str(r[c]) for c in COLUMNS))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    header = lines[0].split(",")
    out = []
    for ln in lines[1:]:
        parts = ln.split(",")
        row = dict(zip(header, parts))
        for key in ("steps", "hf", "swing", "seconds", "synth_s", "rtf", "lead"):
            row[key] = float(row[key])
        row["steps"] = int(row["steps"])
        out.append(row)
    return out


def _stats(values: list[float]) -> tuple[float, float, float]:
    """返回 (均值, 最小, 最大)——这三个数就够看出「这一格的噪声有多大」。"""
    arr = np.asarray(values, dtype=np.float64)
    return float(arr.mean()), float(arr.min()), float(arr.max())


def print_summary(rows: list[dict]) -> None:
    """分组统计——目的只有一个：**先把噪声量出来，再谈差异**。

    为什么需要：同一模型、同一精度，只换采样步数（= 换一次随机采样），"高频占比"
    能从 3.89% 跳到 13.61%。如果把步数当成重复测量，就得到了每格的噪声水平；
    组间差比噪声小的时候，那个结论就是假的。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["who"], r["kind"], r["precision"], r["text"]), []).append(r)

    print("\n── 分组汇总（同组内的多个步数当作重复采样；高频占比越小越好）")
    print(f"{'角色':<8} {'模型':<14} {'精度':<5} {'文本':<5} {'n':>2} {'高频均值':>8} "
          f"{'极差':>13} {'±1SEM':>7} {'波动均值':>8} {'合成均值':>8}")
    for (who, kind, precision, text), items in sorted(groups.items()):
        hf = [i["hf"] for i in items]
        mean, lo, hi = _stats(hf)
        sem = float(np.std(hf, ddof=1) / np.sqrt(len(hf))) if len(hf) > 1 else 0.0
        swing = float(np.mean([i["swing"] for i in items]))
        synth = float(np.mean([i["synth_s"] for i in items]))
        print(f"{who:<8} {kind:<14} {precision:<5} {text:<5} {len(hf):>2} {mean:7.2f}% "
              f"{lo:5.2f}~{hi:5.2f}% {sem:6.2f} {swing:7.2f}dB {synth:7.2f}s")

    print("\n── 同一步数下 int8 vs fp32（配对对比；只有这个能回答「量化有没有影响」）")
    paired_found = False
    for (who, kind, text) in sorted({(r["who"], r["kind"], r["text"]) for r in rows}):
        by_steps: dict[int, dict[str, float]] = {}
        for r in rows:
            if r["who"] == who and r["kind"] == kind and r["text"] == text:
                by_steps.setdefault(r["steps"], {})[r["precision"]] = r["hf"]
        deltas = [v["fp32"] - v["int8"] for v in by_steps.values()
                  if "int8" in v and "fp32" in v]
        if not deltas:
            continue
        paired_found = True
        better = sum(1 for d in deltas if d < 0)
        mean_d = float(np.mean(deltas))
        noise = float(np.std(deltas, ddof=1) / np.sqrt(len(deltas))) if len(deltas) > 1 else 0.0
        verdict = "不可区分（差 < 2×SEM）" if abs(mean_d) < 2 * noise else "方向一致"
        print(f"  {who:<8} {kind:<14} {text:<5} n={len(deltas)}  "
              f"fp32 更低 {better}/{len(deltas)} 次，平均 {mean_d:+.2f} 百分点（SEM {noise:.2f}）"
              f"  → {verdict}")
    if not paired_found:
        print("  （本次只跑了一种精度，无法配对对比）")
    print("  ★注意★：n 只有 4，上面这个「方向一致」只能算提示；要把噪声降下来，")
    print("     得把 --steps 换成同一组多点（例如每个步数跑 3 次），让同一格有真重复。")


def main() -> int:
    ap = argparse.ArgumentParser(description="精度 × 步数的 A/B（写出 wav + 客观指标）")
    ap.add_argument("--who", default="kaltsit,amiya", help="逗号分隔的角色 id")
    ap.add_argument("--steps", default="4,8,16,32", help="逗号分隔的流匹配步数")
    ap.add_argument("--text", default="short,long", choices=["short", "long", "short,long"])
    ap.add_argument("--precision", default="int8,fp32", help="逗号分隔的精度")
    ap.add_argument("--summary", default="", help="不重新合成，只对已有的 report.csv 出汇总表")
    args = ap.parse_args()

    if args.summary:
        print_summary(read_csv(Path(args.summary)))
        return 0


    whos = [w.strip() for w in args.who.split(",") if w.strip()]
    steps_list = [int(s) for s in args.steps.split(",") if s.strip()]
    texts = [t.strip() for t in args.text.split(",") if t.strip()]
    precisions = [p.strip() for p in args.precision.split(",") if p.strip()]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"输出目录：{OUT_DIR}")
    print(f"{'角色':<8} {'模型':<12} {'精度':<5} {'步数':>4} {'文本':<6} "
          f"{'时长':>6} {'合成':>6} {'RTF':>5} {'高频':>7} {'波动':>7} {'头静音':>7}")
    rows: list[dict] = []

    for who in whos:
        ref = REFS.get(who)
        if ref is None or not ref.exists():
            print(f"跳过 {who}：参考音频不在（{ref}）")
            continue
        pdir = PERSONA_DIR / who
        for text_key in texts:
            text = TEXTS[text_key]
            cases: list[tuple[str, Path, str]] = []
            if who == "kaltsit":  # 出厂蒸馏 + int8 当「好听」的对照（注意：不是同一个人的音色）
                cases.append(("factory-distill", FACTORY_DIR, "int8"))
            for precision in precisions:
                if has_precision(pdir, precision):
                    cases.append(("finetuned", pdir, precision))
                else:
                    print(f"跳过 {who}/{precision}：目录里没有 {PRECISION_FILES[precision][0]}")
            for kind, model_dir, precision in cases:
                for steps in steps_list:
                    try:
                        pcm, rate, cost = synth_once(model_dir, ref, text, precision, steps)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {kind} {precision} {steps} 步失败：{exc}")
                        continue
                    seconds = pcm.size / rate
                    hf = hf_ratio(pcm, rate)
                    swing, _peak = rms_swing(pcm, rate)
                    lead = lead_silence(pcm, rate)
                    name = f"{who}_{kind}_{precision}_s{steps}_{text_key}.wav"
                    sf.write(str(OUT_DIR / name), pcm.astype(np.float32) / 32768.0, rate)
                    rows.append({
                        "who": who, "kind": kind, "precision": precision, "steps": steps,
                        "text": text_key, "seconds": seconds, "synth_s": cost,
                        "rtf": cost / max(seconds, 1e-6), "hf": hf * 100,
                        "swing": swing, "lead": lead, "wav": name,
                    })
                    print(
                        f"{who:<8} {kind:<12} {precision:<5} {steps:>4} {text_key:<6} "
                        f"{seconds:5.2f}s {cost:5.2f}s {cost / max(seconds, 1e-6):5.2f} "
                        f"{hf * 100:6.2f}% {swing:6.2f}dB {lead:6.2f}s  {name}"
                    )

    report = OUT_DIR / "report.txt"
    report.write_text("\n".join(format_row(r) for r in rows) + "\n", encoding="utf-8")
    csv_path = OUT_DIR / "report.csv"
    write_csv(csv_path, rows)
    print(f"\n指标表：{report}\n原始数据：{csv_path}")
    print(f"试听：{OUT_DIR}（文件名里 <角色>_<模型>_<精度>_s<步数>_<短/长>.wav）")
    # ★单点数字不可信★：同一模型同一精度、只换采样步数，高频占比能差 3 倍以上。
    # 所以先把「同配置的多个采样」当重复测量，算出均值与噪声，再谈精度/步数的影响。
    print_summary(rows)
    print("判读要点：")
    print("  1. ★先看上面那张汇总表的「波动范围」★：如果某组的极差比组间差还大，")
    print("     那一格就**不能下结论**（短句尤其如此——它只有一两个字的音频）。")
    print("  2. 真要看「量化有没有影响」，只用**同一步数**下 int8 与 fp32 的配对对比（见汇总表末）。")
    print("  3. RTF > 1 就是「说多久等多久」，实时对话要看这一列能不能接受。")
    print("     ★但 RTF 这一列对短句天然很夸张：每次 synth() 都要重编码一遍参考音频")
    print("       （≈ 0.36 × 参考秒数，见 docs/ENGINEERING_LOG.md 15.1），这部分是固定开销，")
    print("       跟精度无关——比「精度贵多少」要看「合成」那一列的绝对秒数。★")
    return 0


if __name__ == "__main__":
    sys.exit(main())
