"""TTS 调试工具：查看音素、对比不同停顿/参数下的音频与静音分布。

用法：
    python scripts/tts_probe.py                       # 默认用例
    python scripts/tts_probe.py "你好[[,]]世界"        # 指定文本
    python scripts/tts_probe.py --sweep               # 扫描 length_scale / noise_w_scale
    python scripts/tts_probe.py --out demo.wav        # 保存最后一个用例
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.audio import save_wav  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

DEFAULT_CASES = [
    "你好世界",
    "你好[[,]]世界",
    "你好[[,,]]世界",
    "你好[[,,,]]世界",
    "好的。",
    "好的[[,,]]",
    "我们下午一起去西湖边散步吧[[,]]顺便买两杯奶茶[[,,]]",
    "我们下午一起去西湖边散步吧，顺便买两杯奶茶。",
]


def analyze(voice, text: str, syn) -> tuple[float, float, list[tuple[int, float]]]:
    parts: list[np.ndarray] = []
    t0 = time.perf_counter()
    for chunk in voice.synthesize(text, syn_config=syn):
        parts.append(np.asarray(chunk.audio_int16_array))
    synth_s = time.perf_counter() - t0
    pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    rate = voice.config.sample_rate
    audio_s = len(pcm) / rate

    win = int(rate * 0.01)
    n = len(pcm) // win
    if n == 0:
        return audio_s, synth_s, []
    energy = np.array(
        [np.sqrt(np.mean(pcm[i * win : (i + 1) * win].astype(np.float32) ** 2) + 1e-9) for i in range(n)]
    )
    quiet = np.where(energy < energy.max() * 0.05)[0]
    groups: list[list[int]] = []
    cur: list[int] = []
    for a, b in zip(quiet, quiet[1:]):
        if b - a <= 2:
            cur.append(b)
        else:
            if cur:
                groups.append(cur)
            cur = [b]
    if cur:
        groups.append(cur)
    gaps = [(len(g) * 10, round(g[0] * 0.01, 2)) for g in groups if len(g) >= 4]
    return audio_s, synth_s, gaps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", default=None)
    ap.add_argument("--sweep", action="store_true", help="扫描 length_scale / noise_w_scale")
    ap.add_argument("--compare", action="store_true", help="生成「按逗号切块」与「整句合成」的对比音频")
    ap.add_argument("--out", default=None, help="把最后一个用例保存为 wav")
    ap.add_argument("--length-scale", type=float, default=None)
    ap.add_argument("--noise-w", type=float, default=None)
    args = ap.parse_args()

    settings = load_settings()
    from piper import PiperVoice, SynthesisConfig

    model = settings.resolve(settings.tts.model)
    voice = PiperVoice.load(str(model))
    print(f"语音：{settings.tts.voice}  espeak={voice.config.espeak_voice}  {voice.config.sample_rate} Hz")

    def make(length_scale, noise_w):
        return SynthesisConfig(
            length_scale=length_scale, noise_scale=0.667, noise_w_scale=noise_w
        )

    cases = args.text or DEFAULT_CASES
    syn = make(
        args.length_scale if args.length_scale is not None else settings.tts.length_scale,
        args.noise_w if args.noise_w is not None else settings.tts.noise_w_scale,
    )

    print("\n=== 音素（[[...]] 内为原始音素，',' '.' 会产生停顿）===")
    for t in cases[:3]:
        print(f"  {t!r}\n     {''.join(voice.phonemize(t)[0])}")

    print("\n=== 停顿分析 ===")
    for t in cases:
        audio_s, synth_s, gaps = analyze(voice, t, syn)
        gap_str = "  ".join(f"{ms}ms@{pos}s" for ms, pos in gaps) or "—"
        print(f"  {t[:38]:40s} 音频 {audio_s:5.2f}s  合成 {synth_s:4.2f}s  静音 {gap_str}")

    if args.sweep:
        probe = "我们下午一起去西湖边散步吧[[,]]顺便买两杯奶茶[[,,]]今天天气不错"
        print(f"\n=== 参数扫描  ({probe[:20]}…) ===")
        for ls in (0.9, 0.95, 1.0, 1.05, 1.1):
            for nw in (0.7, 0.8, 0.9, 1.0):
                audio_s, synth_s, _ = analyze(voice, probe, make(ls, nw))
                print(
                    f"  length_scale={ls:<5} noise_w={nw:<4} -> 音频 {audio_s:5.2f}s "
                    f"合成 {synth_s:4.2f}s 语速 {len(probe) / audio_s:4.2f} 字/秒"
                )

    if args.out:
        t = cases[-1]
        d, s, _ = analyze(voice, t, syn)
        pcm = np.concatenate([np.asarray(c.audio_int16_array) for c in voice.synthesize(t, syn_config=syn)])
        save_wav(args.out, pcm.astype(np.float32) / 32768.0, voice.config.sample_rate)
        print(f"\n已保存 {args.out}（{d:.2f}s）")

    if args.compare:
        compare(settings, voice, syn, args.text)
    return 0


def compare(settings, voice, syn, texts) -> None:
    """把「按逗号切块 + 每块补静音」和「整句合成」各生成一份，方便对比语调。"""
    reply = texts[0] if texts else (
        "好的。今天天气不错，要不要出去走走？"
        "我觉得挺合适的，那就下午三点在西湖边碰面吧，顺便买两杯奶茶。"
    )
    rate = voice.config.sample_rate
    out_dir = settings.sessions_dir

    def render_old(text: str) -> np.ndarray:
        """旧做法：遇到逗号也切，每块单独合成，块间补 0.15 秒静音。"""
        import re

        pieces = [p for p in re.split(r"(?<=[，。！？；])", text) if p.strip()]
        gap = np.zeros(int(rate * 0.15), dtype=np.int16)
        out = []
        for piece in pieces:
            for chunk in voice.synthesize(piece.strip(), syn_config=syn):
                out.append(np.asarray(chunk.audio_int16_array))
            out.append(gap)
        return np.concatenate(out)

    def render_new(text: str) -> np.ndarray:
        """新做法：整句（可多句）一次交给 Piper，让它自己安排韵律。"""
        gap = np.zeros(int(rate * 0.08), dtype=np.int16)
        out = []
        for chunk in voice.synthesize(text, syn_config=syn):
            out.append(np.asarray(chunk.audio_int16_array))
        out.append(gap)
        return np.concatenate(out)

    old = render_old(reply)
    new = render_new(reply)
    p_old = out_dir / "prosody_old_按逗号切块.wav"
    p_new = out_dir / "prosody_new_整句合成.wav"
    save_wav(p_old, old.astype(np.float32) / 32768.0, rate)
    save_wav(p_new, new.astype(np.float32) / 32768.0, rate)

    print("\n=== 语调对比 ===")
    print(f"  文本：{reply}")
    print(f"  旧做法（按逗号切块，块间 150ms 静音）：{len(old) / rate:.2f}s  -> {p_old}")
    print(f"  新做法（整句合成，块间 80ms）：        {len(new) / rate:.2f}s  -> {p_new}")
    print("  两个文件已经生成，直接播放听听差别。")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
