"""试听 / 测速：ZipVoice 音色克隆（不改配置也能跑）。

用法：
    # 1) 用官方样例自检链路（参考音频和文本都是现成的）
    python scripts/tts_clone_probe.py --selftest

    # 2) 用凯尔希的日语音频试（参考文本留空 = 用本地 ASR 自动转写）
    python scripts/tts_clone_probe.py --ref data/personas/kalsit/问候.wav

    # 3) 手工给参考文本（最准），并指定要说的话
    python scripts/tts_clone_probe.py --ref data/personas/kalsit/任命助理.wav \
        --ref-text "……这段音频的逐字文本……" --text "我在，博士。"

    # 4) 和 Piper 对比同一句话的延迟
    python scripts/tts_clone_probe.py --ref data/personas/kalsit/问候.wav --compare

产物写到 sessions/tts_clone_probe/（已在 .gitignore 里），命令行会打印 RTF
和「第一段音频要等多久」——后者才是对话体感的关键。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings  # noqa: E402

OUT_DIR = ROOT / "sessions" / "tts_clone_probe"
SAMPLE_CLIP = ROOT / "models" / "tts" / "zipvoice" / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia" / "test_wavs" / "leijun-1.wav"
SAMPLE_TEXT = "那还是三十六年前, 一九八七年. 我呢考上了武汉大学的计算机系."
DEFAULT_TEXT = "我在，博士。罗德岛的日程已经排好了，你先去休息吧。"


def _write(path: Path, rate: int, pcm: np.ndarray) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), pcm.astype(np.int16), samplerate=rate, subtype="PCM_16")


def _run(tts, text: str, tag: str) -> dict:
    """跑一遍：记录第一段音频的延迟、总耗时、RTF，并把 wav 落盘。"""
    t0 = time.perf_counter()
    first: float | None = None
    parts: list[np.ndarray] = []
    rate = 24000
    for r, pcm in tts.synth(text):
        if first is None:
            first = time.perf_counter() - t0
        rate = r
        parts.append(pcm)
    elapsed = time.perf_counter() - t0
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    audio_s = audio.size / float(rate) if rate else 0.0
    out = OUT_DIR / f"{tag}.wav"
    _write(out, rate, audio)
    info = {
        "tag": tag,
        "text": text,
        "first_chunk_seconds": first or 0.0,
        "synth_seconds": elapsed,
        "audio_seconds": audio_s,
        "rtf": elapsed / audio_s if audio_s else 0.0,
        "sample_rate": rate,
        "file": out,
    }
    print(
        f"  {tag:22s} 首段 {info['first_chunk_seconds']:5.2f}s  总 {elapsed:6.2f}s  "
        f"音频 {audio_s:5.2f}s  RTF {info['rtf']:5.2f}  -> {out.relative_to(ROOT)}"
    )
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="ZipVoice 音色克隆试听 / 测速")
    ap.add_argument("--ref", default="", help="参考音频（wav）；给 data/personas/kalsit 里的一个")
    ap.add_argument("--ref-text", default="", help="参考音频的逐字文本；留空则自动转写")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="要说的话（中文）")
    ap.add_argument("--steps", type=int, default=0, help="流匹配步数：4 快、8~16 稳（默认用配置值）")
    ap.add_argument("--limit-seconds", type=float, default=0.0, help="参考音频最多用几秒（默认用配置值）")
    ap.add_argument("--selftest", action="store_true", help="用官方样例音频自检链路")
    ap.add_argument("--compare", action="store_true", help="同一句话再跑一遍 Piper 做对比")
    ap.add_argument("--config", default="", help="配置文件（默认 config.toml）")
    args = ap.parse_args()

    settings = load_settings(args.config or None)
    tts_cfg = settings.tts
    tts_cfg.backend = "zipvoice"
    if args.steps:
        tts_cfg.clone_steps = args.steps
    if args.limit_seconds:
        tts_cfg.clone_max_seconds = args.limit_seconds
    ref = args.ref or (str(SAMPLE_CLIP) if args.selftest else "")
    if not ref:
        print("请用 --ref 指定参考音频，或用 --selftest 跑官方样例", file=sys.stderr)
        return 2
    tts_cfg.clone_audio = ref
    tts_cfg.clone_text = args.ref_text or (SAMPLE_TEXT if args.selftest else "")

    print(f"[1/3] 加载 ZipVoice（步数 {tts_cfg.clone_steps}，参考 {Path(ref).name}）")
    t0 = time.perf_counter()
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    engine = ZipVoiceTts(settings)
    print(f"      加载耗时 {time.perf_counter() - t0:.1f}s，输出采样率 {engine.sample_rate} Hz")
    if engine.reference_text:
        print(f"      参考文本：{engine.reference_text[:60]}{'…' if len(engine.reference_text) > 60 else ''}")
    else:
        print("      参考文本为空：音色会明显退化（建议写一个同名 .txt 或手工 --ref-text）")

    print("[2/3] 合成")
    info = _run(engine, args.text, f"zipvoice_{engine._ref_path.stem if engine._ref_path else 'ref'}_{tts_cfg.clone_steps}step")

    if args.compare:
        print("[3/3] Piper 对比（同一句话）")
        from voice_loop.tts import create_tts

        cfg_backend, cfg_voice = settings.tts.backend, settings.tts.voice
        settings.tts.backend = "piper"
        piper = create_tts(settings, lazy=False)
        _run(piper, args.text, "piper")
        settings.tts.backend, settings.tts.voice = cfg_backend, cfg_voice
        print(
            f"\n结论：克隆（{info['first_chunk_seconds']:.2f}s 出声）"
            f" vs Piper（通常 0.05s 量级出声）——RTF {info['rtf']:.2f} vs 0.04 左右。"
            "\n试听：上面两个 wav 直接播来听，音色是否是你要的，只能用耳朵判断。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
