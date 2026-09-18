"""麦克风回环测试：用扬声器放一段语音，看麦克风能不能听到并正确识别。

用途
    1. 诊断麦克风电平是否过低（不用对着麦克风喊，可以反复跑）
    2. 验证「唤醒词 + ASR」是不是真的能工作

用法：
    python scripts/test_mic_loopback.py                       # 默认：小助手，现在几点了。
    python scripts/test_mic_loopback.py "小助手，今天有什么课"
    python scripts/test_mic_loopback.py --text "你好呀" --no-play   # 只录音不播放
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.audio import MicReader, make_segmenter  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="小助手，现在几点了")
    ap.add_argument("--text", dest="text_opt", default=None, help="要播放的文本")
    ap.add_argument("--seconds", type=float, default=6.0, help="录音时长")
    ap.add_argument("--no-play", action="store_true")
    args = ap.parse_args()
    text = args.text_opt or args.text

    logging.basicConfig(level=logging.WARNING)
    settings = load_settings()
    import sounddevice as sd

    rate = int(settings.audio.sample_rate)

    # 1) 用 Piper 合成要播放的内容
    from voice_loop.tts import create_tts

    tts = create_tts(settings)
    sr, pcm = tts.synth_bytes(text)
    print(f"要播放：{text!r}  （{len(pcm) / sr:.2f}s，{sr} Hz）")

    # 2) 同时开始录音
    mic = MicReader(settings)
    frames: list[np.ndarray] = []
    stop = threading.Event()

    def record() -> None:
        with mic:
            while not stop.is_set():
                frames.append(mic.read())

    th = threading.Thread(target=record, daemon=True)
    th.start()
    time.sleep(0.4)

    if not args.no_play:
        print("播放中…（请把系统音量保持在 40% 以上，不要静音）")
        sd.play(pcm, sr)
        sd.wait()

    time.sleep(max(0.5, args.seconds - len(pcm) / sr))
    stop.set()
    th.join(timeout=2.0)
    mic.close()

    if not frames:
        print("× 没有录到任何数据")
        return 1
    audio = np.concatenate(frames)
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio**2)))
    print(f"\n录音 {audio.size / rate:.2f}s   峰值 {peak:.4f}   RMS {rms:.4f}")

    if peak < 0.01:
        print("! 麦克风几乎没听到声音。可能原因：")
        print("  1) Windows 设置 > 隐私和安全性 > 麦克风 未允许桌面应用访问")
        print("  2) 系统静音 / 音量过低，或扬声器接的是耳机（麦克风听不到）")
        print("  3) [audio] input_device 选错了设备，试试 python main.py devices 里的编号")
        print("  4) 麦克风阵列首通道增益很低，把 [audio] mic_gain 调到 2.0 试试")
    else:
        print("√ 电平正常，可以继续做识别")

    # 3) 用 VAD + ASR 看识别结果
    seg = make_segmenter(settings)
    utterances: list[np.ndarray] = []
    frame = int(settings.audio.frame_size)
    for i in range(0, audio.size, frame):
        block = audio[i : i + frame]
        if block.size < frame:
            block = np.pad(block, (0, frame - block.size))
        got = seg.accept(block)
        if got is not None:
            utterances.append(got)
    utterances.extend(seg.flush())
    print(f"\nVAD 切出 {len(utterances)} 段" + (f"：{[round(u.size / rate, 2) for u in utterances]}" if utterances else ""))

    if not utterances:
        print("! VAD 没检测到语音，说明电平确实太低或全是噪声")
        return 1

    from voice_loop.asr import AsrRouter

    router = AsrRouter(settings, logging.getLogger("voice_loop"))
    try:
        for i, utt in enumerate(utterances, 1):
            results = router.transcribe_both(utt, rate)
            print(f"\n[片段 {i}] {utt.size / rate:.2f}s")
            for r in results:
                print(f"  {r.engine:11s} {r.latency:.2f}s  {r.text}")
        print("\n如果你看到识别结果接近播放的内容，说明麦克风 + ASR 链路正常。")
    finally:
        router.close()
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
