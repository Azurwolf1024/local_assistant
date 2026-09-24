"""把角色素材**拼起来**当参考音频（可能比单条更稳，见工程日志 21.3）。

为什么会有这个东西（2026-09-25 实测）：
* 参考音频决定**整体音区**；单条素材只有一两种语气时，同一句话重采几遍的整体音区会飘
  1.3~1.9 半音（5 遍极差）。把两三条平静素材拼成一个 8~9 秒的参考后，实测 4 遍极差 **0.59 半音**。
* 但不能乱拼：① 每条都要**掐掉首尾死静音**（否则拼出来的长停顿会被模型学去，说话变拖）；
  ② 拼完的音频必须配**逐条文本拼起来的文本**（声不对词 → 输出含糊，这是老坑）；
  ③ 音区不是越长越稳（12.9 秒的四段拼接反而不如 8.2 秒的两段），所以拼完要量。

用法：

    python scripts/make_ref_join.py kaltsit 完成高难行动 非3星结束行动 --name ref_join_B
    python scripts/make_ref_join.py amiya 交谈1 交谈2 --name ref_join_B --gap-ms 150

产出 `data/personas/<角色>/<name>.wav` + 同名 `.txt`（引擎会优先读同名 txt 当参考文本）。
拼完会打一行客观指标（时长 / 音区 / 10-12kHz 频段），★音色好不好要耳朵定★：

    python scripts/ab_clone_model.py --profile data\\personas\\kaltsit\\ref_join_B.wav
    # 然后对着听 sessions/ref_ab/ 里同名几条
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="把角色素材拼成一个参考音频（wav + 同名 txt）")
    ap.add_argument("persona", help="角色 id（= data/personas/<id>/ 目录名）")
    ap.add_argument("clips", nargs="+", help="要拼的素材名（可不带 .wav），顺序即拼接顺序")
    ap.add_argument("--name", default="ref_join", help="产出的文件名（默认 ref_join）")
    ap.add_argument("--gap-ms", type=int, default=150, help="素材之间的空白（默认 150ms）")
    ap.add_argument("--lead-ms", type=int, default=30, help="每条掐掉的头（默认 30ms）")
    ap.add_argument("--tail-ms", type=int, default=60, help="每条掐掉的尾（默认 60ms）")
    ap.add_argument("--dry-run", action="store_true", help="只看会拼成什么，不写文件")
    args = ap.parse_args()

    import soundfile as sf

    from voice_loop.manifest import manifest_pairs
    from voice_loop.tts import pitch as pk
    from voice_loop.tts.pacing import trim_silence
    from scripts.ab_clone_model import hf_band_profile

    d = ROOT / "data" / "personas" / args.persona
    if not d.is_dir():
        print(f"找不到角色目录：{d}")
        return 2
    pairs = manifest_pairs(d)

    parts: list[np.ndarray] = []
    texts: list[str] = []
    rate = 0
    for clip in args.clips:
        path = d / (clip if clip.lower().endswith(".wav") else f"{clip}.wav")
        if not path.is_file():
            print(f"素材不存在：{path}")
            return 2
        y, r = sf.read(str(path), dtype="int16")
        if rate and r != rate:
            print(f"采样率不一致（{path.name} 是 {r}，前面是 {rate}）——先统一采样率再拼")
            return 2
        rate = r
        if y.ndim > 1:
            y = y[:, 0]
        trimmed = trim_silence(y, r, lead_ms=args.lead_ms, tail_ms=args.tail_ms)
        text = pairs.get(path.name.lower(), "") or pairs.get(path.stem.lower(), "")
        if not text:
            print(f"⚠️ {path.name} 在清单（{args.persona}.txt）里没有文本——"
                  "声不对词会让输出含糊，建议先在清单里补上")
        parts.append(trimmed)
        texts.append(text)
        print(f"  + {path.name:<22}{y.size / r:5.2f}s → 掐静音后 {trimmed.size / r:5.2f}s  文本 {len(text)} 字")

    gap = np.zeros(int(rate * max(0, args.gap_ms) / 1000), dtype=np.int16)
    joined = parts[0]
    for p in parts[1:]:
        joined = np.concatenate([joined, gap, p])

    prof = hf_band_profile(joined, rate)
    print(f"\n拼起来：{joined.size / rate:.2f}s @ {rate}Hz   音区 {pk.f0_median(joined, rate) or 0:.0f}Hz   "
          f"频段 4-6k {prof[1]:.1f} / 6-8k {prof[2]:.1f} / 8-10k {prof[3]:.1f} / 10-12k {prof[4]:.1f} dB")
    if args.dry_run:
        print("（--dry-run：没写文件）")
        return 0

    wav = d / f"{args.name}.wav"
    txt = d / f"{args.name}.txt"
    sf.write(str(wav), joined, rate)
    txt.write_text("\n".join(texts), encoding="utf-8")
    print(f"\n√ 写好 {wav.name} + {txt.name}")
    print("  换上去：改 data/personas/<id>.json 的两个字段（voice_ref 指这个 wav；"
          "voice_ref_text 留空会读同名 txt）")
    print(f"  对比听：python scripts/ab_clone_model.py --profile {wav.relative_to(ROOT)}")
    print("  ★只信一次测量的结论会翻车（音区抖动 ±1.5 半音是采样噪声）：要定就用 10 遍以上复测★")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
