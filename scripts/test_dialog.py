"""对话链路端到端自测：不需要麦克风，直接喂文本。

会走完整的「技能 → LLM → 分句 TTS」流程，用来验证：
    - 技能是否命中
    - 首次出声 / 首字延迟
    - 输出文本是否符合朗读习惯

用法：
    python scripts/test_dialog.py                 # 默认用例，含语音播报
    python scripts/test_dialog.py --no-tts        # 不出声，只看文本与耗时
    python scripts/test_dialog.py "今天有什么课" "记一下买牛奶"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.pipeline import VoiceLoop  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

DEFAULT_PROMPTS = [
    "现在几点了",
    "今天星期几",
    "十分钟后提醒我喝水",
    "明天早上七点叫我起床",
    "我的提醒有哪些",
    "记一下，明天记得买牛奶",
    "我的备忘有哪些",
    "今天有什么课",
    "下一个会议是什么",
    "取消第1个提醒",
    "你能做什么",
    "用一句话介绍杭州",
    "好的，谢谢",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", default=None)
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--real", action="store_true", help="使用真实的 data/ 目录（默认写临时目录）")
    ap.add_argument("--subtitle", action="store_true", help="顺便把字幕也弹出来看看（默认关闭）")
    args = ap.parse_args()

    settings = load_settings()
    if args.strategy:
        settings.asr.strategy = args.strategy
    # 自测默认不往屏幕上弹字幕，免得刷屏；想看得加 --subtitle
    settings.subtitle.enabled = bool(args.subtitle)

    tmp_dir = None
    if not args.real:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="voiceloop_dialog_"))
        settings.skills.data_dir = str(tmp_dir)
        settings.skills.alarm_file = str(tmp_dir / "alarms.json")
        settings.skills.memo_file = str(tmp_dir / "memos.json")
        settings.skills.schedule_file = str(tmp_dir / "schedule.json")

    prompts = args.text or DEFAULT_PROMPTS
    loop = VoiceLoop(settings, enable_listening=False)
    loop.tts_enabled = not args.no_tts
    loop.llm.ensure_model()

    print("=" * 70)
    print(f" 对话链路自测（{len(prompts)} 轮，{'含语音播报' if loop.tts_enabled else '仅文本'}）")
    print(f" 数据目录：{'临时 ' + str(tmp_dir) if tmp_dir else settings.skills.data_dir}")
    print("=" * 70)

    t_start = time.perf_counter()
    for i, prompt in enumerate(prompts, 1):
        print(f"\n[{i:02d}] 你：{prompt}")
        print("     助手：", end="", flush=True)
        try:
            stats = loop.respond(prompt, on_delta=lambda d: print(d, end="", flush=True))
        except Exception as exc:  # noqa: BLE001
            print(f"\n     × 失败：{type(exc).__name__}: {exc}")
            continue
        print()
        if stats.extra.get("skill"):
            print(f"     [技能 {stats.extra['skill']}]  播报 {stats.total_seconds:.2f}s")
        else:
            print(
                f"     [LLM] 首字 {stats.llm_first_token:.2f}s / 首音 {stats.first_audio:.2f}s"
                f" / 总 {stats.total_seconds:.2f}s"
            )

    loop.close()
    print(f"\n总计 {time.perf_counter() - t_start:.1f}s")
    if tmp_dir:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
