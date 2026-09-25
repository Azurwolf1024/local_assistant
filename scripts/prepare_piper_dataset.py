"""为 **Piper 微调**准备数据集（22050 Hz 单声道 + ``metadata.csv``）。

为什么单独写一个（而不是给 ``prepare_tts_dataset.py`` 加参数）：
那个脚本的产物是 ZipVoice 要的「TSV + 24 kHz」，而且有 24 条测试盯着它；
Piper 这边要的是**另一套格式**（``metadata.csv`` 里 ``文件名|文本``，音频放 ``wav/``，
22050 Hz —— 必须和底模 ``zh_CN-huayan-medium`` 的采样率一致，否则训练出来就是坏的）。

用到的两个净化动作（都复用现成的）：
* ``pacing.trim_silence``：掐掉首尾死静音（素材每段都有 0.04~0.5 秒的空白，
  留着会让模型学会「先说一段空白」）；
* 文本按清单逐条对应（``manifest_pairs``）——声不对词是这类合成最隐蔽的坑。

用法（默认试运行，加 ``--apply`` 才写文件）：

    python scripts/prepare_piper_dataset.py --dir data/personas/kaltsit --out data/piper/kaltsit --apply
    # 然后（在 .venv-piper 里）：
    python -m piper_train.preprocess --language zh --sample-rate 22050 ^
        --input-dir data/piper/kaltsit --output-dir data/piper/kaltsit/training --dataset-format ljspeech ^
        --single-speaker --max-workers 4
"""

from __future__ import annotations

import argparse
import sys
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_tts_dataset import lookup, resolve_texts  # noqa: E402
from voice_loop.tts.pacing import trim_silence  # noqa: E402

# ★必须和底模一致★：zh_CN-huayan-medium 就是 22050 Hz（见它的 .onnx.json）
TARGET_RATE = 22050


def load_at(path: Path, rate: int) -> np.ndarray:
    """读成目标采样率单声道 float32（多相重采样，比线性插值干净）。"""
    samples, src = sf.read(str(path), dtype="float32", always_2d=False)
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim > 1:
        x = x[:, 0]
    if src != rate:
        g = gcd(int(src), rate)
        x = resample_poly(x, rate // g, int(src) // g).astype(np.float32)
    return x


def main() -> int:
    ap = argparse.ArgumentParser(description="给 Piper 微调准备数据集（22050Hz + metadata.csv）")
    ap.add_argument("--dir", required=True, help="角色素材目录（含 wav + 清单 txt）")
    ap.add_argument("--out", required=True, help="输出目录（会写 metadata.csv + wav/）")
    ap.add_argument("--manifest", default="", help="清单 txt（默认用源目录里的 .txt）")
    ap.add_argument("--min-seconds", type=float, default=0.8, help="短于这个的丢掉（默认 0.8s）")
    ap.add_argument("--lead-ms", type=int, default=40, help="掐头保留多少 ms")
    ap.add_argument("--tail-ms", type=int, default=80, help="掐尾保留多少 ms")
    ap.add_argument("--apply", action="store_true", help="真写文件（默认只试运行）")
    ap.add_argument("--force", action="store_true", help="输出目录非空时覆盖")
    args = ap.parse_args()

    src = Path(args.dir).resolve()
    if not src.is_dir():
        raise SystemExit(f"源目录不存在：{src}")
    wavs = sorted(p for p in src.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"{src} 里没有 wav")
    texts = resolve_texts(src, Path(args.manifest).resolve() if args.manifest else None)

    out = Path(args.out).resolve()
    wav_dir = out / "wav"
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} 非空（加 --force 才覆盖）")

    rows: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    total_seconds = 0.0
    for wav in wavs:
        text = lookup(texts, wav)
        if not text.strip():
            skipped.append((wav.name, "清单里没有对应文本"))
            continue
        x = load_at(wav, TARGET_RATE)
        pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
        trimmed = trim_silence(pcm, TARGET_RATE, lead_ms=args.lead_ms, tail_ms=args.tail_ms)
        seconds = trimmed.size / TARGET_RATE
        if seconds < args.min_seconds:
            skipped.append((wav.name, f"太短（{seconds:.2f}s < {args.min_seconds}s）"))
            continue
        rows.append((wav.stem, " ".join(text.split())))
        total_seconds += seconds
        if not args.apply:
            continue
        wav_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(wav_dir / f"{wav.stem}.wav"), trimmed, TARGET_RATE, subtype="PCM_16")

    if not rows:
        raise SystemExit("没有可用的（音频, 文本）对，什么都没写")

    print(f"源目录 {src}")
    print(f"可用 {len(rows)} 对，共 {total_seconds:.1f} 秒（{total_seconds / 60:.1f} 分钟）；丢掉 {len(skipped)} 条")
    for name, why in skipped:
        print(f"  - {name}：{why}")

    if not args.apply:
        print("\n（试运行：没写文件。加 --apply 真写）")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    meta = out / "metadata.csv"
    meta.write_text("".join(f"wav/{stem}.wav|{text}\n" for stem, text in rows), encoding="utf-8")
    print(f"\n√ 写好 {meta}（{len(rows)} 行）+ {len(rows)} 个 wav @ {TARGET_RATE}Hz")
    print("  下一步（在 .venv-piper 里跑）：")
    rel = out.relative_to(ROOT)
    print(f"  python -m piper_train.preprocess --language zh --sample-rate {TARGET_RATE} "
          f"--input-dir {rel} --output-dir {rel}/training --dataset-format ljspeech "
          f"--single-speaker --max-workers 4")
    print("  ★preprocess 之后要抽查 hparams 里的 sample_rate / num_symbols，再开训★")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
