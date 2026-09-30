"""声纹体检：一批 wav 之间（或与某个参考/角色）**像不像同一个人**。

为什么需要这个工具（2026-09-30）：项目里已经能量音区（``pitch_report.py``）、
停顿节奏、沙沙声，但用户最在意的那件事 —— 「这是不是同一个人在说」—— 一直只能靠耳朵。
换参考、换精度、换步数、重训模型，几次调整之间没有共同刻度，就说不清「这次到底好没好」。

用法：

    # 一批音频两两比（找「哪条其实不像她」）
    python scripts/spk_check.py data/personas/kaltsit/*.wav

    # 合成结果 vs 一个角色的素材（质心）—— 这才是「像不像她」的正题
    python scripts/spk_check.py sessions/xxx.wav --ref data/personas/kaltsit

    # 合成结果 vs 它自己用的那条参考音频
    python scripts/spk_check.py sessions/xxx.wav --ref data/personas/kaltsit/干员报到.wav

    # 机器可读（给别的脚本/A/B 用）
    python scripts/spk_check.py a.wav b.wav --json

★怎么读这些数字★（阈值是量出来的，见下）：它是**相对**量，只跟同一套条件下的数字比；
拿它做 A/B 要**配对 + 多遍**（合成是采样的，单次会骗人）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings          # noqa: E402
from voice_loop.tts import speaker                     # noqa: E402

AUDIO_EXTS = (".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus")


def collect(paths: list[str]) -> list[Path]:
    """参数里的文件 + 目录（目录只取音频）。顺序稳定，去重。"""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found = sorted(q for q in p.iterdir()
                           if q.is_file() and q.suffix.lower() in AUDIO_EXTS)
        elif p.is_file():
            found = [p]
        else:
            print(f"· 找不到：{raw}")
            continue
        for q in found:
            if q not in out:
                out.append(q)
    return out


def label(path: Path, root: Path) -> str:
    """显示用的路径：在项目里就给相对路径（正斜杠，好复制好对账）。"""
    p = path.resolve()
    return p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="声纹相似度（像不像同一个人）")
    ap.add_argument("wavs", nargs="*", help="要比的音频（也可以是目录）")
    ap.add_argument("--ref", default="", help="参考：一条音频，或一个角色的素材目录（用质心）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器读）")
    ap.add_argument("--limit", type=int, default=0, help="每边最多取几条（0 = 全要）")
    args = ap.parse_args()

    settings = load_settings()
    bad = speaker.missing(settings)
    if bad:
        print(f"× {bad}")
        return 2
    emb = speaker.load(settings)
    if emb is None:
        print("× 声纹模型加载失败（看 sherpa-onnx 的报错）")
        return 2

    items = collect(args.wavs or [args.ref])
    refs = collect([args.ref]) if args.ref else []
    if args.limit:
        items = items[: args.limit]
        refs = refs[: args.limit]
    if not items:
        print("× 没有可比对的音频。用法见本文件开头。")
        return 2

    vecs = {p: emb.embedding_file(p) for p in items}
    usable = [p for p in items if vecs[p] is not None]
    skipped = [p for p in items if vecs[p] is None]

    print(f"声纹模型：{emb.path.name}（{emb.dim} 维）")
    if skipped:
        print(f"· 算不出向量（太短/读不了）跳过 {len(skipped)} 条：" +
              "、".join(label(p, ROOT) for p in skipped[:4]))

    if refs:
        ref_vecs = {p: emb.embedding_file(p) for p in refs}
        ref_usable = [v for v in ref_vecs.values() if v is not None]
        if not ref_usable:
            print("× 参考音频一个向量都算不出来")
            return 2
        base = (speaker.centroid(ref_usable) if len(ref_usable) > 1
                else ref_usable[0])
        what = (f"素材质心（{len(ref_usable)} 条）" if len(ref_usable) > 1
                else label(refs[0], ROOT))
        rows = []
        for p in usable:
            score = speaker.cosine(vecs[p], base)
            rows.append((label(p, ROOT), score, speaker.verdict(score)))
        if args.json:
            print(json.dumps({"reference": what, "rows": rows}, ensure_ascii=False, indent=2))
            return 0
        print(f"\n对照：{what}")
        print(f"  {'音频':<46} {'相似度':>7}   判读")
        for name, score, what_v in rows:
            print(f"  {name:<46} {score:7.3f}   {what_v}")
        if len(ref_usable) > 1:
            # 素材自己的内部一致性：给个手感基准（同人素材两两中位那档）
            inside = sorted(speaker.cosine(a, b)
                            for a, b in itertools.combinations(ref_usable, 2))
            if inside:
                mid = inside[len(inside) // 2]
                print(f"\n（参考集内部两两相似度中位 {mid:.3f} —— "
                      f"合出来的数**接近它**就算「像她」，明显低于它说明音色跑偏）")
        return 0

    # 没有参考：两两矩阵
    if len(usable) < 2:
        print("× 两两比至少要两条能算出向量的音频（或加 --ref）")
        return 2
    names = [label(p, ROOT) for p in usable]
    print("")
    width = max(len(n) for n in names)
    print(" " * (width + 2) + " ".join(f"{i:>6}" for i in range(len(names))))
    for i, a in enumerate(usable):
        cells = []
        for j, b in enumerate(usable):
            cells.append("     ·" if i == j else f"{speaker.cosine(vecs[a], vecs[b]):6.3f}")
        print(f"{names[i]:<{width}}  " + " ".join(cells))
    print("\n列号：" + "  ".join(f"{i}={n}" for i, n in enumerate(names)))
    if args.json:
        data = {"names": names,
                "matrix": [[speaker.cosine(vecs[a], vecs[b]) for b in usable] for a in usable]}
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
