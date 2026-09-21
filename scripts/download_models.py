"""下载本地方案所需的全部模型权重。

用法：
    python scripts/download_models.py            # 下载全部缺失模型
    python scripts/download_models.py --only vad # 只下载某个
    python scripts/download_models.py --force    # 强制重新下载

模型清单
    - vision/asr/sensevoice : SenseVoiceSmall (sherpa-onnx int8, 约 230 MB)
    - vad/silero_vad.onnx   : Silero VAD (约 2 MB, 可选，缺失时自动降级为能量 VAD)
    - tts/piper/zh_CN-huayan-medium : Piper 中文女声 (约 63 MB)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

# 国内优先使用 hf-mirror.com，失败再回退到官方源
HF_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]

SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
PIPER_REPO = "rhasspy/piper-voices"

# Silero VAD 的可选来源（按顺序尝试）
SILERO_VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
]

# 零样本音色克隆（ZipVoice，中英双语，sherpa-onnx 跑）
ZIPVOICE_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2",
    "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2",
]
ZIPVOICE_VOCODER_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos_24khz.onnx",
    "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos_24khz.onnx",
]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download(url: str, dest: Path, force: bool = False) -> Path:
    """带进度条与断点续传的下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force and dest.stat().st_size > 0:
        print(f"  [跳过] 已存在 {dest.name} ({human(dest.stat().st_size)})")
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        t0 = time.time()
        with open(tmp, "wb") as f:
            while True:
                block = resp.read(1 << 18)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if total:
                    pct = done * 100 / total
                    speed = done / max(time.time() - t0, 1e-6) / 1024 / 1024
                    sys.stdout.write(
                        f"\r  下载 {dest.name}: {pct:5.1f}%  {human(done)}/{human(total)}  {speed:5.2f} MB/s"
                    )
                else:
                    sys.stdout.write(f"\r  下载 {dest.name}: {human(done)}")
                sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.replace(dest)
    return dest


def download_with_fallback(urls: list[str], dest: Path, force: bool = False, optional: bool = False) -> bool:
    if dest.exists() and not force and dest.stat().st_size > 0:
        print(f"  [跳过] 已存在 {dest.name} ({human(dest.stat().st_size)})")
        return True
    for url in urls:
        try:
            download(url, dest, force=force)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {url}\n         {type(exc).__name__}: {exc}")
    if optional:
        print(f"  [警告] {dest.name} 全部来源均不可用，将继续（功能会自动降级）")
        return False
    raise RuntimeError(f"无法下载 {dest.name}，请手动放置到 {dest}")


def hf_urls(repo: str, path: str) -> list[str]:
    return [f"{m}/{repo}/resolve/main/{path}" for m in HF_MIRRORS]


# --------------------------------------------------------------------------- #
# 各模型
# --------------------------------------------------------------------------- #
def fetch_sensevoice(force: bool = False) -> None:
    print("[1/3] SenseVoiceSmall (sherpa-onnx int8)")
    out = MODELS / "asr" / "sensevoice-small"
    out.mkdir(parents=True, exist_ok=True)
    files = [
        ("model.int8.onnx", True),
        ("tokens.txt", True),
        ("test_wavs/zh.wav", False),
        ("test_wavs/en.wav", False),
    ]
    for name, required in files:
        dest = out / Path(name).name
        download_with_fallback(hf_urls(SENSEVOICE_REPO, name), dest, force=force, optional=not required)


def fetch_silero_vad(force: bool = False) -> None:
    print("[2/3] Silero VAD")
    out = MODELS / "vad" / "silero_vad.onnx"
    download_with_fallback(SILERO_VAD_URLS, out, force=force, optional=True)


def fetch_piper(voice: str = "zh_CN-huayan-medium", force: bool = False) -> None:
    print(f"[3/3] Piper 语音包 {voice}")
    # voice 形如 zh_CN-huayan-medium -> zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
    locale, name, quality = voice.split("-")
    lang = locale.split("_")[0]
    base = f"{lang}/{locale}/{name}/{quality}/{voice}"
    out = MODELS / "tts" / "piper"
    for suffix in (".onnx", ".onnx.json"):
        download_with_fallback(hf_urls(PIPER_REPO, base + suffix), out / (voice + suffix), force=force)


def fetch_zipvoice(force: bool = False) -> None:
    """ZipVoice 音色克隆：模型包（104 MB）+ 声码器（52 MB）。"""
    print("[4/4] ZipVoice 音色克隆（中英双语零样本克隆）")
    out = MODELS / "tts" / "zipvoice"
    out.mkdir(parents=True, exist_ok=True)
    tarball = out / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2"
    target = out / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
    if target.exists() and not force:
        print(f"      已解包，跳过：{target.name}")
    else:
        download_with_fallback(ZIPVOICE_URLS, tarball, force=force)
        print("      解包中…")
        import tarfile

        with tarfile.open(tarball, "r:bz2") as tar:
            tar.extractall(out)  # noqa: S202 - 官方发布包
        tarball.unlink(missing_ok=True)
    download_with_fallback(
        ZIPVOICE_VOCODER_URLS, out / "vocos_24khz.onnx", force=force
    )
    print("      完成。用法见 README 第 14 节，试听：python scripts/tts_clone_probe.py --help")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载本地语音链路模型")
    ap.add_argument(
        "--only",
        nargs="*",
        choices=["sensevoice", "vad", "piper", "zipvoice"],
        help="只下载指定模型",
    )
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    args = ap.parse_args()

    targets = set(args.only) if args.only else {"sensevoice", "vad", "piper"}
    if "sensevoice" in targets:
        fetch_sensevoice(args.force)
    if "vad" in targets:
        fetch_silero_vad(args.force)
    if "piper" in targets:
        fetch_piper(force=args.force)
    if "zipvoice" in targets:
        fetch_zipvoice(force=args.force)

    print("\n完成。模型目录：")
    for p in sorted(MODELS.rglob("*")):
        if p.is_file() and p.suffix in {".onnx", ".bin", ".xml", ".txt", ".json"} and ".cache" not in str(p):
            print(f"  {p.relative_to(ROOT)}  ({human(p.stat().st_size)})")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
