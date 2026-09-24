"""量「音区」的 CLI 尺子：哪个 wav 整体偏高/偏低，一眼看表。

用法（不加载 TTS 模型，纯读 wav）：

    python scripts/pitch_report.py sessions\\pitch_runs\\*.wav
    python scripts/pitch_report.py --ref data/personas/amiya/交谈1.wav sessions\\voice_ab2\\*.wav
    python scripts/pitch_report.py                # 不给参数：只量配置里那条参考音频

表里的列：

* ``F0中位``  整段音区（有声帧的中位音高）。★「异常的高亢/低沉」看这一列★
* ``摆幅st``  四分位差（半音）＝一句之内的正常起伏，跟参考音比才有意义
* ``离群%``   偏离**局部**中位 >6 半音的有声帧占比（局部=±0.5 秒）
* ``持续±st`` 持续（≥200ms）偏离的最远半音数：+ 是高亢、− 是低沉
* ``跳变%``   相邻有声帧跨 >3 半音的比例（毛刺感）
* ``vs靶子st`` 相对靶子（``--ref`` 的音区，或 ``--target`` 指定）差多少半音
* ``触发``    = 按配置的 ``pitch_guard_st`` 会被丢掉重采（st = 半音）

★为什么要给你这把尺子★：耳朵说「这句怎么突然高了」时，拿它量一下就知道是**整体音区**
偏了（该重采），还是**句内起伏**正常（该动的是参考音/语气），别凭感觉改参数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _fmt(v: float, width: int, prec: int = 0) -> str:
    return f"{v:>{width}.{prec}f}" if v == v else " " * (width - 1) + "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="量 wav 的音区（F0）报告")
    ap.add_argument("wav", nargs="*", help="要量的 wav（支持通配符由 shell 展开）；不给就量配置里的参考音频")
    ap.add_argument("--ref", default="", help="靶子：拿这条 wav 的音区当基准")
    ap.add_argument("--target", type=float, default=0.0, help="靶子：直接给 Hz")
    ap.add_argument("--limit", type=float, default=-1.0, help="触发阈值（半音）；-1 = 用配置里的 pitch_guard_st")
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf

    from voice_loop.settings import load_settings
    from voice_loop.tts import pitch as pk

    settings = load_settings()
    limit = float(args.limit) if args.limit >= 0 else float(getattr(settings.tts, "pitch_guard_st", 0.0) or 0.0)

    target = float(args.target)
    if args.ref:
        ref = Path(args.ref)
        if not ref.exists():
            print(f"找不到靶子 wav：{ref}", file=sys.stderr)
            return 2
        y, rate = sf.read(str(ref), dtype="int16")
        target = pk.f0_median(y, rate) or 0.0
        print(f"靶子：{ref.name} → {target:.0f}Hz\n")
    elif target <= 0:
        audio = str(getattr(settings.tts, "clone_audio", "") or "").strip()
        if audio:
            path = settings.resolve(audio)
            if path.exists():
                y, rate = sf.read(str(path), dtype="int16")
                target = pk.f0_median(y, rate) or 0.0
                print(f"靶子：{path.name}（配置里的参考音频）→ {target:.0f}Hz\n")
            else:
                print(f"（配置里的参考音频不存在：{path}）\n")

    files: list[Path] = []
    for item in args.wav:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(p.glob("*.wav")))
        elif p.exists():
            files.append(p)
        else:
            print(f"跳过（不存在）：{item}", file=sys.stderr)
    if not files and target > 0:
        ref_audio = str(getattr(settings.tts, "clone_audio", "") or "").strip()
        p = settings.resolve(ref_audio) if ref_audio else None
        files = [p] if p and p.exists() else []
    if not files:
        print("没有要量的文件。用法：python scripts/pitch_report.py <wav...>", file=sys.stderr)
        return 2

    head = f"{'文件':<34}{'时长':>8}{'F0中位':>9}{'摆幅st':>9}{'离群%':>8}{'持续+st':>10}{'持续-st':>10}{'跳变%':>8}{'有声%':>8}{'vs靶子st':>10}"
    if limit > 0:
        head += f"{'触发':>6}"
    print(head)
    print("-" * len(head))
    trips = 0
    for path in files:
        try:
            y, rate = sf.read(str(path), dtype="int16")
        except Exception as exc:  # noqa: BLE001 - 一个坏文件不该中断整表
            print(f"{path.name:<34} 读不了：{exc}", file=sys.stderr)
            continue
        s = pk.stats(pk.f0_track(y, rate)[1])
        line = (f"{path.name[:33]:<34}{y.size / rate:7.2f}s{_fmt(s['f0_med'], 8, 0)}H"
                f"{_fmt(s['iqr_st'], 8, 1)}st{_fmt(s['outlier_pct'], 7, 1)}%"
                f"{_fmt(s['sustained_up'], 9, 1)}st{_fmt(s['sustained_dn'], 9, 1)}st"
                f"{_fmt(s['jump_pct'], 7, 1)}%{_fmt(s['voiced_pct'], 7, 0)}%")
        if target > 0:
            dev = pk.register_st(y, rate, target)
            line += f"{_fmt(dev, 9, 2)}st" if dev is not None else f"{'   -':>11}"
            if limit > 0:
                trip = dev is not None and abs(dev) > limit
                trips += int(trip)
                line += f"{'重采' if trip else '':>6}"
        print(line)
    if target > 0 and limit > 0:
        print(f"\n阈值 {limit:g} 半音（st）：{trips}/{len(files)} 条会被丢掉重采（靶子 {target:.0f}Hz）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
