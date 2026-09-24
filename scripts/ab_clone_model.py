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


def main() -> int:
    ap = argparse.ArgumentParser(description="精度 × 步数的 A/B（写出 wav + 客观指标）")
    ap.add_argument("--who", default="kaltsit,amiya", help="逗号分隔的角色 id")
    ap.add_argument("--steps", default="4,8,16,32", help="逗号分隔的流匹配步数")
    ap.add_argument("--text", default="short,long", choices=["short", "long", "short,long"])
    ap.add_argument("--precision", default="int8,fp32", help="逗号分隔的精度")
    args = ap.parse_args()

    whos = [w.strip() for w in args.who.split(",") if w.strip()]
    steps_list = [int(s) for s in args.steps.split(",") if s.strip()]
    texts = [t.strip() for t in args.text.split(",") if t.strip()]
    precisions = [p.strip() for p in args.precision.split(",") if p.strip()]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"输出目录：{OUT_DIR}")
    print(f"{'角色':<8} {'模型':<12} {'精度':<5} {'步数':>4} {'文本':<6} "
          f"{'时长':>6} {'合成':>6} {'RTF':>5} {'高频':>7} {'波动':>7} {'头静音':>7}")
    rows: list[str] = []

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
                    line = (
                        f"{who:<8} {kind:<12} {precision:<5} {steps:>4} {text_key:<6} "
                        f"{seconds:5.2f}s {cost:5.2f}s {cost / max(seconds, 1e-6):5.2f} "
                        f"{hf * 100:6.2f}% {swing:6.2f}dB {lead:6.2f}s  {name}"
                    )
                    print(line)
                    rows.append(line)

    report = OUT_DIR / "report.txt"
    report.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n指标表：{report}")
    print(f"试听：{OUT_DIR}（文件名里 <角色>_<模型>_<精度>_s<步数>_<短/长>.wav）")
    print("判读要点：")
    print("  1. 同一精度下 4 → 16 步，「高频 %」应该明显下降、声音更稳；")
    print("  2. 同一步数下 int8 → fp32，如果降幅和①差不多，说明量化才是主因；")
    print("  3. RTF > 1 就是「说多久等多久」，实时对话要看这一列能不能接受。")
    print("     ★但 RTF 这一列对短句天然很夸张：每次 synth() 都要重编码一遍参考音频")
    print("       （≈ 0.36 × 参考秒数，见 docs/ENGINEERING_LOG.md 15.1），这部分是固定开销，")
    print("       跟精度无关——比「精度贵多少」要看第二列「合成」的绝对秒数。★")
    return 0


if __name__ == "__main__":
    sys.exit(main())
