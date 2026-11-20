"""识别评测台：CER / 唤醒命中率 / 误唤醒率 / RTF（★改了什么立刻能量★）。

为什么要有它
    识别优化最容易凭感觉：调个阈值、换个模型、加两条词，"好像变好了"。
    但 CER 可能反而升高、误唤醒可能翻倍。这个脚本就是那把尺子。

用法
    # 先合成一批素材（唤醒词 / 人名 / 领域词，用本地 TTS，零人工）
    python scripts\\eval_asr.py --synth

    # 跑真机录音（你自己录的，放 data/asr_eval/<集合>/*.wav + 同名 .txt）
    python scripts\\eval_asr.py --set wake

    # 对比所有可用后端（一张表看完）
    python scripts\\eval_asr.py --matrix

集合（目录名就是集合名）
    wake      喊唤醒词的短音频      → 唤醒命中率 + 首字延迟
    names     带人名的句子          → CER（词库有没有帮上忙）
    command   普通命令句            → CER
    negative  不含唤醒词的日常话    → ★误唤醒率★（这个高了比不准更烦人）

★两套素材分开看★：`synth/` 是 TTS 合成的（干净、覆盖广，证明"词库和模型对得上"），
真机录音带口音/噪声/麦克风频响，才是最可信的那套。别拿合成音的分数当结论。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.asr import Hearing  # noqa: E402
from voice_loop.asr.eval import (  # noqa: E402
    SETS, load_cases, run, synthesize,
)
from voice_loop.asr.lexicon import build_lexicon  # noqa: E402
from voice_loop.asr.registry import AsrRegistry  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.wake import WakeWordMatcher  # noqa: E402


def _logger():
    import logging

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname).1s %(name)s | %(message)s")
    return logging.getLogger("voice_loop.eval")


def _synthesis_texts() -> dict[str, list[str]]:
    """合成素材的文本：唤醒词 + 人名 + 领域词 + 负样本。"""
    return {
        "wake": ["凯尔希", "白泽", "阿米娅", "能天使", "克莱门莎", "蓝色大肥鱼",
                 "凯尔希，现在几点了", "白泽，帮我记一下"],
        "names": ["凯尔希，明天几点开会", "白泽，把这句话记下来",
                  "阿米娅今天心情不错", "能天使在吗", "克莱门莎说话真慢"],
        "command": ["现在几点了", "明天下午三点提醒我开会", "把屏幕调暗一点",
                    "刚才那句话再说一遍", "帮我查一下后天的天气"],
        "negative": ["今天有点累", "海尔洗衣机坏了", "开尔文温度是多少",
                     "米亚的论文写完了吗", "这个灯有点暗"],
    }


def _matcher(settings, logger) -> WakeWordMatcher:
    """评测用的唤醒词匹配器（★和助手跑的是同一份配置★，不然量的不是同一件事）。"""
    path = settings.resolve(settings.wake.file)
    matcher = WakeWordMatcher(path)
    WakeWordMatcher.write_default(path)
    matcher.load(force=True)
    return matcher


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="识别评测台（CER / 唤醒 / 误唤醒 / RTF）")
    ap.add_argument("--root", default="data/asr_eval", help="素材根目录（默认 data/asr_eval）")
    ap.add_argument("--set", action="append", default=[],
                    help=f"只跑某些集合：{'/'.join(SETS)}（可写多次）")
    ap.add_argument("--synth", action="store_true",
                    help="先用本地 TTS 合成一批素材到 <root>/synth（已存在的跳过）")
    ap.add_argument("--matrix", action="store_true",
                    help="每个就绪的后端都跑一遍，最后给一张对比表")
    ap.add_argument("--engine", default="", help="只跑某个后端（默认按 [asr] 策略）")
    ap.add_argument("--kws", action="store_true", help="同时试 KWS 唤醒判定（需要模型）")
    ap.add_argument("--limit", type=int, default=0, help="每个集合最多跑几条")
    args = ap.parse_args(argv)

    settings = load_settings()
    log = _logger()
    root = Path(args.root)
    if not root.is_absolute():
        root = ROOT / root

    if args.synth:
        from voice_loop.asr.eval import synthesize as _synth
        from voice_loop.tts import create_tts

        # ★TTS 在这里接进来★：asr 模块本身不认识 TTS（这样它能单独拿走、单独评测）
        tts = create_tts(settings, log)

        def speak(text: str):
            return list(tts.synth(text))

        print("=" * 96)
        print(" 合成评测素材（本地 TTS）")
        print("=" * 96)
        made = 0
        try:
            for set_name, texts in _synthesis_texts().items():
                cases = _synth(settings, texts, root, set_name=f"synth/{set_name}",
                               logger=log, speak=speak)
                made += len(cases)
                print(f"  · {set_name:<10}{len(cases)} 条")
        finally:
            close = getattr(tts, "close", None)
            if callable(close):
                close()
        print(f"  合成完成：{made} 条 → {root / 'synth'}")
        if not (args.set or args.matrix):
            print("\n  （加 --set wake 或者 --matrix 开始评测）")
            return 0

    sets = [s for raw in args.set for s in raw.split(",") if s] or None
    cases = load_cases(root, sets=sets)
    if args.limit > 0:
        by_set: dict[str, int] = {}
        kept = []
        for case in cases:
            n = by_set.get(case.set_name, 0)
            if n >= args.limit:
                continue
            by_set[case.set_name] = n + 1
            kept.append(case)
        cases = kept
    if not cases:
        print(f"× 没找到素材：{root}")
        print("  先跑：python scripts\\eval_asr.py --synth")
        print("  或者把你录的 wav（+ 同名 .txt）放到 data/asr_eval/<wake|names|command|negative>/")
        return 2

    lexicon = build_lexicon(settings, log)
    matcher = _matcher(settings, log)
    kws = None
    if args.kws:
        from voice_loop.asr.kws import build_kws

        kws = build_kws(settings, log, lexicon)
        if kws is None or not kws.ready:
            print(f"  （KWS 没开或不可用：{getattr(kws, 'why', '未启用')}，唤醒判定只用文本匹配）")
            kws = None

    engines = [args.engine] if args.engine else []
    if args.matrix:
        registry = AsrRegistry(settings, log)
        registry.discover()
        engines = [p.id for p in registry.providers.values() if registry._ready(p)[0]]  # noqa: SLF001
        if not engines:
            print("× 没有就绪的后端，先下模型：python scripts\\download_models.py")
            return 2
    engines = engines or [""]

    print("=" * 96)
    print(f" 识别评测 · 素材 {len(cases)} 条 · 词库 {lexicon.stats()['total'] if lexicon else 0} 条"
          + (f" · KWS {kws.why or '就绪'}" if args.kws else ""))
    print("=" * 96)

    rows: list[dict] = []
    for engine in engines:
        hearing = Hearing(settings, log, preload=False, lexicon=lexicon)
        try:
            report = run(settings, cases, hearing=hearing, wake=matcher, kws=kws,
                         prefer=engine, logger=log)
        finally:
            hearing.close()
        summary = report.summary()
        label = engine or "（按策略）"
        print(f"\n[{label}]")
        for line in report.lines():
            print("  " + line)
        for err in report.errors[:5]:
            print("  ! " + err)
        print(f"  → CER {summary['cer']:.3f}"
              + (f" · 唤醒命中 {summary['wake_hit']:.1%}" if summary["wake_hit"] is not None else "")
              + (f" · 误唤醒 {summary['false_wake']:.1%}" if summary["false_wake"] is not None else "")
              + f" · RTF {summary['rtf']:.3f} · 平均 {summary['latency']:.2f}s")
        rows.append({"engine": label, **summary})

    if len(rows) > 1:
        print("\n" + "=" * 96)
        print(" 对比")
        print("=" * 96)
        print(f"  {'后端':<16}{'CER':>8}{'唤醒':>8}{'误唤醒':>8}{'RTF':>8}{'延迟 s':>9}")
        for row in rows:
            wake = f"{row['wake_hit']:.1%}" if row["wake_hit"] is not None else "—"
            false = f"{row['false_wake']:.1%}" if row["false_wake"] is not None else "—"
            print(f"  {row['engine']:<16}{row['cer']:>8.3f}{wake:>8}{false:>8}"
                  f"{row['rtf']:>8.3f}{row['latency']:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
