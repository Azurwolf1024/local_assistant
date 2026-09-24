"""音质 A/B：去掉沙沙声 / 保住语气连贯，**配对 + 重复**量一遍，并写出试听 wav。

为什么必须配对与重复（2026-09-25 实测，这条推翻了旧记录）：
ZipVoice 是**采样生成**的，同一配置连跑三次，输出
    时长 6.85 / 6.41 / 6.61 s
    整条高频 8.22% / 4.63% / 8.63%
——**不是确定性的**（旧笔记里「同配置跑 4 次时长极差 0.00s」是错的，别再当依据）。
所以单点对比能把噪声当成效果（上一轮的「加步数有效/无效」就这么翻过两次）。
本脚本：每个变体跑 `--repeats` 次，**轮转交织**（v0,v1,…vN,v0,…）让机器负载变化对大家一样，
报中位数 + 配对差，并把每一遍的 wav 都留下（耳朵是最终判据）。

两种模式：

    --hiss     沙沙声：整条高频 / ★可闻安静帧高频★ / 停顿底噪 p10
               变体 = 参考音频净化 × 输出去嘶声（模型与参考固定两条角色声线）
    --rhythm   语气连贯：句内停顿列表 / 每块有声电平的离散度 / 字/有声秒
               变体 = 后处理参数（在**同一段音频**上过不同参数 → 天然配对）

用法：
    python scripts/ab_voice.py --hiss            # 默认，约 4 分钟（24 次合成）
    python scripts/ab_voice.py --hiss --repeats 2
    python scripts/ab_voice.py --rhythm
    python scripts/ab_voice.py --who amiya --hiss
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ab_clone_model import (  # noqa: E402
    audible_quiet_hf,
    floor_db,
    gaps_ms,
    hf_ratio,
    lead_silence,
    rms_swing,
    voiced_seconds,
)
from voice_loop.text import SpeechChunker  # noqa: E402
from voice_loop.tts.pacing import LevelMatcher, trim_silence  # noqa: E402

OUT = ROOT / "sessions" / "voice_ab2"
MODELS = {
    "kaltsit": ROOT / "models" / "tts" / "zipvoice" / "personas" / "kaltsit",
    "amiya": ROOT / "models" / "tts" / "zipvoice" / "personas" / "amiya",
}
REFS = {
    "kaltsit": ROOT / "data" / "personas" / "kaltsit" / "干员报到.wav",
    "amiya": ROOT / "data" / "personas" / "amiya" / "交谈1.wav",
}
LONG = "今天的日程已经排好了，上午九点是机器学习课，下午两点还有组会。"
REPLY = "博士，我在。今天的日程已经排好了，上午九点是机器学习课，下午两点还有组会。报告我看过了。"


def measure(pcm: np.ndarray, rate: int, chars: int = 0) -> dict:
    """一次合成的全部客观指标（新口径在前）。"""
    q_hf, q_share = audible_quiet_hf(pcm, rate)
    swing, _peak = rms_swing(pcm, rate)
    vo = voiced_seconds(pcm, rate)
    return {
        "seconds": round(pcm.size / rate, 3),
        "hf": round(hf_ratio(pcm, rate) * 100, 2),
        "quiet_hf": round(q_hf, 2),
        "quiet_share": round(q_share, 1),
        "floor": round(floor_db(pcm, rate), 1),
        "swing": round(swing, 2),
        "lead": round(lead_silence(pcm, rate), 2),
        "gaps": [round(g) for g in gaps_ms(pcm, rate)],
        "rate": round(chars / vo, 2) if (chars and vo > 0.2) else 0.0,
    }


def build(settings, who: str, clean: str, tilt: tuple[int, float] | None, level: float):
    """按变体配置造一个引擎。"""
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    settings.tts.backend = "zipvoice"
    settings.tts.clone_dir = str(MODELS[who])
    settings.tts.clone_audio = str(REFS[who])
    settings.tts.clone_text = ""
    settings.tts.ref_clean = clean
    settings.tts.ref_clean_strength = 0.7
    settings.tts.ref_tilt_hz = 7000.0
    settings.tts.ref_tilt_db = -3.0
    settings.tts.out_tilt_hz = float(tilt[1]) if tilt else 0.0
    settings.tts.out_tilt_db = float(tilt[0]) if tilt else 0.0
    settings.tts.chunk_level_db = level
    return ZipVoiceTts(settings)


def variants(who: str) -> list[tuple[str, dict]]:
    return [
        ("原样", dict(clean="off", tilt=None, level=0.0)),
        ("参考谱门", dict(clean="gate", tilt=None, level=0.0)),
        ("参考谱门+高架3dB", dict(clean="gate+tilt", tilt=None, level=0.0)),
        ("输出去嘶3dB", dict(clean="off", tilt=(-3, 7000.0), level=0.0)),
        ("输出+参考", dict(clean="gate+tilt", tilt=(-3, 7000.0), level=0.0)),
    ]


# --------------------------------------------------------------------- hiss
def run_hiss(whos: list[str], repeats: int) -> int:
    from voice_loop.settings import load_settings

    OUT.mkdir(parents=True, exist_ok=True)
    for who in whos:
        table: dict[str, list[dict]] = {}
        engines: dict[str, object] = {}
        cfgs = variants(who)
        for label, kw in cfgs:                     # 预建引擎（模型加载不进测量）
            settings = load_settings()
            engines[label] = build(settings, who, **kw)
        for label, _kw in cfgs:                    # 预热，第一次要编译图
            for _ in engines[label].synth("预热。"):
                pass
        for r in range(repeats):                   # ★轮转交织★
            for label, _kw in cfgs:
                eng = engines[label]
                t0 = time.perf_counter()
                rate, pcm = eng.synth_bytes(LONG)
                dt = time.perf_counter() - t0
                m = measure(pcm, rate, chars=len(LONG))
                m["rtf"] = round(dt / (pcm.size / rate), 2)
                m["ref_used"] = eng.reference
                table.setdefault(label, []).append(m)
                sf.write(str(OUT / f"{who}_{label}_r{r}.wav"), pcm, rate)
                print(f"  {who} {label} r{r}: 整条HF {m['hf']:.2f}% 可闻安静 {m['quiet_hf']:.2f}% "
                      f"底噪 {m['floor']:.1f} RTF {m['rtf']:.2f}")
        for label, _kw in cfgs:
            engines[label].close()

        base = table[cfgs[0][0]]
        print(f"\n{'=' * 116}\n{who}：{repeats} 遍，中位数（括号里是范围）\n{'=' * 116}")
        print(f"{'变体':<20}{'整条HF':>14}{'可闻安静HF':>16}{'底噪p10':>14}{'波动dB':>12}"
              f"{'时长':>12}{'RTF':>8}")
        for label, _kw in cfgs:
            rows = table[label]
            def med(k: str, fmt: str = "{:.2f}") -> str:
                vals = [r[k] for r in rows if isinstance(r[k], (int, float))]
                if not vals:
                    return "n/a"
                return (fmt.format(statistics.median(vals)) + " ("
                        + fmt.format(min(vals)) + "~" + fmt.format(max(vals)) + ")")
            print(f"{label:<20}{med('hf'):>14}{med('quiet_hf'):>16}{med('floor', '{:.1f}'):>14}"
                  f"{med('swing'):>12}{med('seconds', '{:.2f}'):>12}{med('rtf'):>8}")
        print("\n配对差（同一遍，减去「原样」；负 = 更干净）：")
        for label, _kw in cfgs[1:]:
            d_hf = [r["hf"] - b["hf"] for r, b in zip(table[label], base)]
            d_q = [r["quiet_hf"] - b["quiet_hf"] for r, b in zip(table[label], base)]
            print(f"  {label:<20} Δ整条HF {statistics.median(d_hf):+6.2f}   Δ可闻安静HF {statistics.median(d_q):+6.2f}")
        (OUT / f"hiss_{who}.json").write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n试听（每个变体每一遍都在）：{OUT}")
    return 0


# ------------------------------------------------------------------- rhythm
def run_rhythm(whos: list[str], repeats: int) -> int:
    from voice_loop.settings import load_settings
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    tts_cfg = load_settings().tts
    chunker = SpeechChunker(
        max_chars=int(tts_cfg.max_chunk_chars), first_min_chars=int(tts_cfg.first_chunk_min_chars),
        min_chunk_chars=int(tts_cfg.min_chunk_chars), max_hold_seconds=0.0,
        first_chunk_max_chars=int(getattr(tts_cfg, "first_chunk_max_chars", 0) or 0),
    )
    chunks = [c for c in chunker.feed(REPLY) + chunker.flush() if c.strip()]
    print(f"分块（{len(chunks)} 块）：" + " | ".join(chunks))

    for who in whos:
        settings = load_settings()
        eng = build(settings, who, clean="off", tilt=None, level=0.0)
        for _ in eng.synth("预热。"):
            pass
        for r in range(repeats):
            pieces: list[np.ndarray] = []
            rate = eng.sample_rate
            for ch in chunks:
                for rr, pcm in eng.synth(ch):
                    rate = rr
                    pieces.append(pcm)
            full = np.concatenate(pieces)
            sf.write(str(OUT / f"{who}_rhythm_r{r}.wav"), full, rate)

            # 每块的有声电平（跨块忽大忽小就住在这里）
            levels = []
            off = 0
            per: list[tuple[int, int]] = []
            for p in pieces:
                per.append((off, off + p.size))
                off += p.size
            for a, b in per:
                seg = full[a:b]
                from scripts.ab_clone_model import frame_db
                db, _hf, _hop = frame_db(seg, rate)
                if db.size:
                    levels.append(float(np.median(db[db > np.percentile(db, 30)])))
            spread = max(levels) - min(levels) if levels else float("nan")
            print(f"\n{who} r{r}: 块数 {len(pieces)} | 每块有声电平 {[round(v, 1) for v in levels]}"
                  f" | 极差 {spread:.1f} dB | 句内停顿 {[round(g) for g in gaps_ms(full, rate)]}")

            # 后处理变体（★同一段音频换参数 → 天然配对★）
            def show(tag: str, arr: np.ndarray) -> None:
                g = [round(x) for x in gaps_ms(arr, rate)]
                vo = voiced_seconds(arr, rate)
                print(f"    {tag:<34} 时长 {arr.size / rate:5.2f}s 停顿 {str(g):<34}"
                      f" 字/有声秒 {len(REPLY) / vo:4.2f}")

            show("现状（不压停顿）", full)
            show("平压长停顿 450→260", trim_silence(full, rate, max_pause_ms=450, min_pause_ms=260,
                                                min_gap_ms=240, min_gap_floor_ms=60))
            show("比例压 0.35（保住长短）", trim_silence(full, rate, shrink_pause=0.35, min_pause_ms=260,
                                                  shrink_over_ms=450, min_gap_ms=240, min_gap_floor_ms=60))
            lm = LevelMatcher(1.5)
            levels_after = []
            for p in pieces:
                q = lm.apply(p, rate)
                from scripts.ab_clone_model import frame_db
                db, _hf, _hop = frame_db(q, rate)
                if db.size:
                    levels_after.append(float(np.median(db[db > np.percentile(db, 30)])))
            after = max(levels_after) - min(levels_after) if levels_after else float("nan")
            print(f"    {'块间电平对齐 ±1.5dB':<34} 电平极差 {spread:.1f} → {after:.1f} dB"
                  f"（施加 {[round(lm.last_applied_db, 2) for _ in range(0)]}）")
        eng.close()
    print(f"\n试听：{OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="音质 A/B（沙沙声 / 语气连贯）")
    ap.add_argument("--hiss", action="store_true", help="跑沙沙声对比（默认）")
    ap.add_argument("--rhythm", action="store_true", help="跑语气连贯 / 节奏对比")
    ap.add_argument("--who", nargs="*", default=None, help="只跑某个角色（kaltsit / amiya）")
    ap.add_argument("--repeats", type=int, default=3, help="每个变体跑几遍（默认 3，配对用）")
    args = ap.parse_args()
    whos = args.who or ["kaltsit", "amiya"]
    if args.rhythm:
        return run_rhythm(whos, max(1, args.repeats))
    return run_hiss(whos, max(1, args.repeats))


if __name__ == "__main__":
    raise SystemExit(main())
