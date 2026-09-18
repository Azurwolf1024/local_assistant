"""把一段文字用 Piper 读出来并通过扬声器播放。

用途：不想对着麦克风喊时，可以用它来测试唤醒词、语音链路。
    python scripts/say.py "小助手，现在几点了"
    python scripts/say.py "小助手" --repeat 3 --gap 1.5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.audio import resolve_device  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="要朗读的文本；用 | 分隔可以分成几句，句间停顿 --gap 秒")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--gap", type=float, default=1.5, help="句与句 / 重复之间的间隔（秒）")
    args = ap.parse_args()

    import sounddevice as sd

    settings = load_settings()
    from voice_loop.tts import create_tts

    tts = create_tts(settings)
    device = resolve_device(settings.audio.output_device, want_input=False)
    parts = [p.strip() for p in args.text.split("|") if p.strip()]
    print(f"播放 {len(parts)} 句，句间停顿 {args.gap}s，音量 {settings.audio.playback_volume}")
    for i in range(max(1, args.repeat)):
        if i:
            time.sleep(args.gap)
        for j, text in enumerate(parts):
            if j:
                time.sleep(args.gap)
            sr, pcm = tts.synth_bytes(text)
            print(f"  → {text!r}  {len(pcm) / sr:.2f}s")
            sd.play(pcm, sr, device=device)
            sd.wait()
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
