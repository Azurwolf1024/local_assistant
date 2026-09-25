"""`scripts/prepare_piper_segments.py` 的离线自测（切段 + 标签对齐，不需要联网/麦克风）。

为什么这些函数值得单独测：它们是「把长独白切成一句一条」的**唯一判据**，
而且两个 bug 都是在这里出的（标签漏字、切点漂到词中间）——
共同特征是「看起来能跑、数据却是错的」，只有断言才能挡住。

    python scripts\\test_prepare_segments.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_piper_segments import label_bounds, mask_to_regions, strip_marks  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print(" 切段脚本自测（标签对齐 / 静音分组）")
    print("=" * 70)

    print("\n[1] strip_marks：去掉标点与空白（只用于对齐）")
    check("中文标点", strip_marks("博士，请坐。"), "博士请坐")
    check("中英混排", strip_marks("Mon3tr，采集 样本！"), "Mon3tr采集样本")
    check("引号与括号", strip_marks("“凯尔希”（医疗部）"), "凯尔希医疗部")

    print("\n[2] label_bounds：★不变量★ 各段标签拼起来必须等于原文")
    text = "你会质疑自己存在的意义吗，博士？我会。大地上的生命十分顽强。"
    for seconds in ([1.0, 1.0, 1.0], [0.5, 2.0, 0.5], [3.0], [0.1, 0.1, 8.0]):
        labels = label_bounds(text, seconds)
        check(f"{len(seconds)} 段拼起来 == 原文", "".join(labels), text)

    print("\n[3] label_bounds：切点要落在标点后面（一句话一条）")
    labels = label_bounds("甲乙丙，丁戊己，庚辛壬。", [1.0, 1.0, 1.0])
    check("每段都以标点结尾", all(label.endswith(("，", "。")) for label in labels),
          True, detail=str(labels))
    check("段数不变", len(labels), 3)

    print("\n[4] label_bounds：边界情况")
    check("单段 = 整条原文", label_bounds("你好，世界。", [2.0]), ["你好，世界。"])
    check("空秒数列表不炸", label_bounds("你好。", []), [])
    check("全是标点 → 返回空标签（不炸）", label_bounds("，。！", [1.0, 1.0]), ["", ""])

    print("\n[5] mask_to_regions：句内小停顿要合并，超长要再切")
    frame = 512                     # Silero 的帧长
    per_frame = frame / 16000.0     # 32 ms
    # 两段语音，中间静音 5 帧（160ms）
    mask = [False] * 40
    for i in range(5, 15):
        mask[i] = True
    for i in range(20, 30):
        mask[i] = True
    mask = [bool(v) for v in mask]

    merged = mask_to_regions(mask, frame, min_silence_s=0.35, max_seconds=30.0, min_seconds=0.1)
    check("160ms 的停顿被合并成 1 段", len(merged), 1, detail=str(merged))
    split = mask_to_regions(mask, frame, min_silence_s=0.1, max_seconds=30.0, min_seconds=0.1)
    check("阈值降到 100ms 就分成 2 段", len(split), 2, detail=str(split))

    long_mask = [True] * 200        # 200 帧 × 32ms = 6.4s
    cut = mask_to_regions(long_mask, frame, min_silence_s=0.35, max_seconds=2.0, min_seconds=0.1)
    check("6.4s 被 2.0s 上限切开", len(cut) >= 3, True, detail=f"{len(cut)} 段 {cut}")
    check("切出来的都 <= 2.0s", all((b - a) / 16000.0 <= 2.01 for a, b in cut), True)

    short_mask = [True] * 5         # 160ms，短于 min_seconds
    check("太短的段被丢掉", mask_to_regions(short_mask, frame, 0.35, 30.0, 1.0), [])
    check("全静音返回空", mask_to_regions([False] * 100, frame, 0.35, 30.0, 0.1), [])

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
