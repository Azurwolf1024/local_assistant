"""把「目录里的 wav + 清单文本」做成 ZipVoice 微调要的数据集。

ZipVoice 官方微调配方（`egs/zipvoice/run_finetune.sh`）要的东西很简单：

    data/raw/custom_train.tsv   每行：`{id}\t{文本}\t{wav 路径}`
    data/raw/custom_dev.tsv     同上（验证集）

要求音频是 **24 kHz 单声道**（模型在 Emilia 上训练的就是 24k），所以这里负责：
重采样 → 掐掉首尾静音 → 落成 16-bit PCM → 按清单配文本 → 切分 train/dev。

    # 先看看会发生什么
    python scripts/prepare_tts_dataset.py --dir data/personas/kaltsit --out data/finetune/kaltsit

    # 真写出去（WSL 里训练时把路径前缀改成 /mnt/d/local_AI/）
    python scripts/prepare_tts_dataset.py --dir data/personas/kaltsit \
        --out data/finetune/kaltsit --apply --path-prefix /mnt/d/local_AI/
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.manifest import manifest_pairs, parse_manifest_file  # noqa: E402
from voice_loop.tts.pacing import trim_silence  # noqa: E402

TARGET_RATE = 24000


def resolve_texts(directory: Path, manifest: Path | None) -> dict[str, str]:
    """``{名字(小写): 文本}``；给了清单就只用它，否则读目录里所有 .txt。"""
    if manifest is not None:
        if not manifest.exists():
            raise SystemExit(f"清单文件不存在：{manifest}")
        pairs = parse_manifest_file(manifest)
        return {name.lower(): text for name, text in pairs if name and text.strip()}
    return {k.lower(): v for k, v in manifest_pairs(directory).items() if v.strip()}


def lookup(texts: dict[str, str], wav: Path) -> str:
    return texts.get(wav.name.lower()) or texts.get(wav.stem.lower()) or ""


def load_24k(path: Path) -> tuple[np.ndarray, int]:
    """读成 24 kHz 单声道 float32（多相重采样，比线性插值干净）。"""
    samples, rate = sf.read(str(path), dtype="float32", always_2d=False)
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim > 1:
        x = x[:, 0]
    if rate != TARGET_RATE:
        from math import gcd

        g = gcd(int(rate), TARGET_RATE)
        x = resample_poly(x, TARGET_RATE // g, int(rate) // g).astype(np.float32)
    return x, TARGET_RATE


def build(args: argparse.Namespace) -> int:
    src = Path(args.dir).resolve()
    if not src.is_dir():
        raise SystemExit(f"源目录不存在：{src}")
    wavs = sorted(p for p in src.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"{src} 里没有 wav")

    manifest = Path(args.manifest).resolve() if args.manifest else None
    texts = resolve_texts(src, manifest)

    out = Path(args.out).resolve()
    wav_dir = out / "wavs"
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} 非空（加 --force 才覆盖）")

    rows: list[tuple[str, str, Path, float]] = []
    skipped: list[tuple[str, str]] = []
    for wav in wavs:
        text = lookup(texts, wav)
        if not text:
            skipped.append((wav.name, "清单里没有对应文本"))
            continue
        x, rate = load_24k(wav)
        pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
        trimmed = trim_silence(pcm, rate, lead_ms=40, tail_ms=80)
        seconds = trimmed.size / rate
        if seconds < args.min_seconds:
            skipped.append((wav.name, f"太短（{seconds:.2f}s < {args.min_seconds}s）"))
            continue
        rows.append((wav.stem, " ".join(text.split()), trimmed, seconds))

    if not rows:
        raise SystemExit("没有可用的（音频, 文本）对，什么都没写")

    total = sum(r[3] for r in rows)
    dev_n = args.dev_count if args.dev_count is not None else max(1, round(len(rows) * 0.1))
    dev_n = min(dev_n, max(1, len(rows) - 1))
    # 均匀挑 dev_n 条当验证集（用中点取样，别用 step 步进——步进在 step=1 时会全选）
    positions = {min(len(rows) - 1, round((i + 0.5) * len(rows) / dev_n)) for i in range(dev_n)}
    dev_ids = {rows[i][0] for i in positions}

    print(f"源目录 {src}")
    print(f"清单文本 {len(texts)} 条，音频 {len(wavs)} 个 → 可用 {len(rows)} 对")
    for name, why in skipped:
        print(f"  · 跳过 {name}：{why}")
    secs = [r[3] for r in rows]
    print(
        f"可用总时长 {total:.1f}s = {total / 60:.1f} 分钟；"
        f"单条 最短 {min(secs):.2f}s / 中位 {statistics.median(secs):.2f}s / 最长 {max(secs):.2f}s"
    )
    print(f"训练/验证：{len(rows) - len(dev_ids)} / {len(dev_ids)}（验证集：{sorted(dev_ids)}）")

    prefix = args.path_prefix or ""
    tsv_train = out / "custom_train.tsv"
    tsv_dev = out / "custom_dev.tsv"
    if not args.apply:
        print("\n[试运行] 不会写文件。加 --apply 才会真正落盘。")
        for row in rows[:3]:
            print(f"  例：{row[0]}\t{row[1][:24]}\t{prefix}{args.out}/wavs/{row[0]}.wav")
        return 0

    if out.exists() and args.force:
        shutil.rmtree(out)
    wav_dir.mkdir(parents=True, exist_ok=True)

    train_lines: list[str] = []
    dev_lines: list[str] = []
    for uid, text, pcm, _s in rows:
        rel = f"{args.out}/wavs/{uid}.wav".replace("\\", "/")
        target = wav_dir / f"{uid}.wav"
        sf.write(str(target), pcm, TARGET_RATE, subtype="PCM_16")
        line = f"{uid}\t{text}\t{prefix}{rel}"
        (dev_lines if uid in dev_ids else train_lines).append(line)

    tsv_train.write_text("\n".join(train_lines) + "\n", encoding="utf-8")
    tsv_dev.write_text("\n".join(dev_lines) + "\n", encoding="utf-8")
    print(f"\n已写出：{tsv_train}（{len(train_lines)} 行）")
    print(f"        {tsv_dev}（{len(dev_lines)} 行）")
    print(f"        {wav_dir}（{len(rows)} 个 24kHz 单声道 wav）")
    print("\n下一步（ZipVoice 仓库 egs/zipvoice 下，需要 Linux 环境，见 README）：")
    print("  PYTHONPATH=../../ python3 -m zipvoice.bin.prepare_dataset \\")
    print(f"      --tsv-path {args.out}/custom_train.tsv --prefix kalsit \\")
    print("      --subset raw_train --num-jobs 4 --output-dir data/manifests")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="准备 ZipVoice 微调数据集（24kHz TSV）")
    ap.add_argument("--dir", required=True, help="源 wav 目录")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--manifest", default="", help="清单 txt（默认用源目录里的 .txt）")
    ap.add_argument("--dev-count", type=int, default=None, help="验证集条数（默认约 10%）")
    ap.add_argument("--min-seconds", type=float, default=0.8, help="短于这个的丢掉（默认 0.8s）")
    ap.add_argument("--path-prefix", default="", help="写进 TSV 的路径前缀（WSL 用 /mnt/d/...）")
    ap.add_argument("--apply", action="store_true", help="真写文件（默认只试运行）")
    ap.add_argument("--force", action="store_true", help="输出目录非空时覆盖")
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
