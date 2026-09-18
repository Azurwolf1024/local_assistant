"""离线自测：分块器 / 中文时间解析 / 生活技能 / 唤醒词。

不依赖麦克风、ASR 模型和 Ollama，几秒就能跑完：
    python scripts/test_offline.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.nlp_time import (  # noqa: E402
    cn_number,
    cn_quantity,
    humanize,
    parse_datetime,
    parse_duration,
)
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.skills import Skills  # noqa: E402
from voice_loop.text import SpeechChunker, prepare_for_reading, transplant_punctuation  # noqa: E402
from voice_loop.wake import WakeWordMatcher  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, expect=None, contains: str | None = None) -> None:
    ok = True
    if expect is not None:
        ok = got == expect
    elif contains is not None:
        ok = contains in str(got)
    print(f"  {PASS if ok else FAIL} {name}: {got!r}" + ("" if ok else f"   (期望 {expect or contains!r})"))
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
def test_chunker() -> None:
    print("\n[1] 流式分块（语调自然的关键）")
    c = SpeechChunker(max_chars=60, first_min_chars=8, min_chunk_chars=14, max_hold_seconds=99)
    text = "好的。今天天气不错，要不要出去走走？我觉得可以，那就下午三点吧。"
    out: list[str] = []
    for ch in text:
        out.extend(c.feed(ch))
    out.extend(c.flush())
    print(f"    输入: {text}")
    for i, s in enumerate(out, 1):
        print(f"    块{i}: {s}")
    check("句末切分（不在逗号处切）", all(not b.endswith("，") for b in out), True)
    check("短句被合并", out[0].startswith("好的。今天"), True)

    c2 = SpeechChunker(max_chars=20, min_chunk_chars=1)
    long_sentence = "这是一句很长很长的话" * 4
    chunks = c2.feed(long_sentence) + c2.flush()
    check("超长句兜底切分", len(chunks) >= 2, True)
    check("每块都有内容", all(s.strip() for s in chunks), True)
    check("不丢字", "".join(chunks).replace("。", "") == long_sentence, True)

    c3 = SpeechChunker(max_chars=60, min_chunk_chars=14, max_hold_seconds=0.0)
    held = c3.feed("好的。")
    check("超时后会先送出去", len(held) >= 0, True)
    check("标点移植", transplant_punctuation("开放时间早上九点", "开饭时间早上9点。"), contains="。")
    check("Markdown 清理", prepare_for_reading("**你好**，这是 `代码`"), contains="你好")
    check("开头语气词不被删", prepare_for_reading("好的，我知道了"), contains="好的")


# --------------------------------------------------------------------------- #
def test_time() -> None:
    print("\n[2] 中文时间解析")
    now = datetime(2026, 9, 17, 22, 30)  # 周四晚上十点半
    cases = [
        ("十分钟后提醒我喝水", "2026-09-17 22:40"),
        ("半小时后提醒我", "2026-09-17 23:00"),
        ("一个半小时后提醒我", "2026-09-18 00:00"),
        ("明天早上七点叫我起床", "2026-09-18 07:00"),
        ("下午三点开会", "2026-09-18 15:00"),
        ("后天中午十二点半提醒我", "2026-09-19 12:30"),
        ("今晚九点提醒我吃药", "2026-09-17 21:00"),
        ("19:30 提醒我", "2026-09-18 19:30"),
    ]
    for text, expect in cases:
        got = parse_datetime(text, now)
        check(f"{text}", got.strftime("%Y-%m-%d %H:%M") if got else None, expect)
    check("时长解析", str(parse_duration("2小时15分钟")), "2:15:00")
    check("时间口语化", humanize(datetime(2026, 9, 18, 7, 0), now), "明天早上七点")
    check("月日读法", humanize(datetime(2026, 9, 24, 9, 0), now), "九月二十四日上午九点")
    check("中文数字", cn_number(153), "一百五十三")
    check("量词两", cn_quantity(2), "两")


# --------------------------------------------------------------------------- #
def test_skills() -> None:
    print("\n[3] 生活技能")
    settings = load_settings()
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_test_"))
    settings.skills.alarm_file = str(tmp / "alarms.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.skills.schedule_file = str(tmp / "schedule.json")
    settings.skills.data_dir = str(tmp)
    skills = Skills(settings)

    # 写入一条每周三 09:00 的课
    skills.schedule.save(
        [
            {"title": "AIAA3102 机器学习", "repeat": "weekly", "weekday": 3, "time": "09:00",
             "location": "A302", "remind_before": 15, "duration_minutes": 90},
            {"title": "组会", "repeat": "weekly", "weekday": 3, "time": "14:00", "duration_minutes": 60},
        ]
    )
    now = datetime(2026, 9, 17, 22, 30)  # 周四

    def run(q: str) -> str:
        r = skills.handle(q)
        if r is None:
            return "（未命中技能 → 交给 LLM）"
        return f"[{r.action}] {r.reply}"

    for q in [
        "现在几点了",
        "今天星期几",
        "十分钟后提醒我喝水",
        "明天早上七点叫我起床",
        "我的提醒有哪些",
        "记一下，明天买牛奶",
        "记以下，明天要买牛奶",    # 模拟语音识别的听错
        "提醒我买牛奶",          # 没时间 -> 存备忘
        "我的备忘有哪些",
        "今天有什么课",
        "这周有什么安排",
        "下一个会议是什么",
        "你能做什么",
        "给我讲个笑话",          # 应该走到 LLM
    ]:
        r = skills.handle(q)
        label = "（交给 LLM）" if r is None else f"[{r.action}] {r.reply}"
        print(f"    {q:24s} -> {label}")

    check("闹钟被创建", len(skills.alarms.load()) >= 2, True)
    check("备忘被创建", len(skills.memos.load()) >= 2, True)
    check("闲聊不抢话", skills.handle("给我讲个笑话"), None)
    check("时间类命中", skills.handle("现在几点了") is not None, True)
    check("今天能查到课", "课程" in skills.handle("今天有什么课").reply or "安排" in skills.handle("今天有什么课").reply, True)

    # 单条取消（放到最后，避免影响前面的计数）
    r = skills.handle("取消第1个提醒")
    print(f"    {'取消第1个提醒':24s} -> [{r.action}] {r.reply}")
    check("按序号取消单条", r.action, "alarm_cancel")
    r = skills.handle("清空所有提醒")
    check("清空全部", r.action, "alarm_clear")

    # 到点检测
    due = skills.due_alarms(datetime.now())
    print(f"    到点闹钟：{[d.get('what') for d in due]}")
    tasks = []
    for item in skills.schedule.load():
        nxt = skills.next_occurrence(item, datetime.now())
        tasks.append((item["title"], nxt.strftime("%Y-%m-%d %H:%M") if nxt else None))
    print(f"    下次日程：{tasks}")
    check("日程能算出下次时间", all(t[1] for t in tasks), True)

    # 数据文件是带注释的包装格式，写入后注释要保留
    raw = json.loads((tmp / "alarms.json").read_text(encoding="utf-8"))
    print(f"    写入后的 alarms.json：{json.dumps(raw, ensure_ascii=False)[:120]}")

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_wake() -> None:
    print("\n[4] 唤醒词匹配")
    settings = load_settings()
    path = settings.resolve(settings.wake.file)
    matcher = WakeWordMatcher(path)
    print(f"    配置：{'、'.join(matcher.settings.words)}  ack={matcher.settings.ack!r}")
    print(f"    idle_timeout={matcher.settings.idle_timeout:.0f}s  fuzzy_ratio={matcher.settings.fuzzy_ratio}")
    cases = [
        ("凯尔希", True),
        ("凯尔希，现在几点了", True),
        ("凯尔西", True),
        ("凯尔希医生在吗", True),
        ("凯尔惜我明天要开会", True),
        ("开尔希", True),
        ("卡尔希", True),
        ("今天天气不错", False),
        ("帮我查一下明天的课", False),
        ("开尔文是热力学温标", False),
        ("凯旋而归", False),
        ("希尔顿酒店", False),
    ]
    ok = True
    for text, expect_hit in cases:
        hit = matcher.match(text)
        got = hit is not None
        mark = PASS if (expect_hit is None or got == expect_hit) else FAIL
        if expect_hit is not None and got != expect_hit:
            ok = False
        rest = matcher.strip_word(text, hit) if hit else "-"
        print(f"    {mark} {text:18s} 命中={got} 词={hit.word if hit else '-'} 剩余={rest!r}")
    check("唤醒判定符合预期", ok, True)


# --------------------------------------------------------------------------- #
def test_lifecycle() -> None:
    """验证「唤醒加载 / 空闲回收」状态机（不碰硬件，不加载模型）。"""
    print("\n[5] 唤醒服务生命周期")
    from voice_loop.pipeline import VoiceLoop

    settings = load_settings()
    settings.wake.idle_action = "standby"

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        check("懒加载模式已开启", loop.lazy, True)
        check("初始为待唤醒", loop._active, False)  # noqa: SLF001
        check("TTS 初始未加载", getattr(loop.tts, "loaded", None), False)
        check("空闲超时来自唤醒词 json", loop._idle_timeout, 180.0)  # noqa: SLF001
        check("唤醒后进入活跃", (loop._activate("测试"), loop._active)[1], True)  # noqa: SLF001
        loop._deactivate()  # noqa: SLF001
        check("回待唤醒后释放", loop._active, False)  # noqa: SLF001

        # idle_action = exit 时应该结束进程
        settings.wake.idle_action = "exit"
        loop._activate("测试")  # noqa: SLF001
        check("idle_action=exit 会置停止位", loop._deactivate(), True)  # noqa: SLF001
        check("停止事件已设置", loop._stop.is_set(), True)  # noqa: SLF001
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 66)
    print(" 离线自测（不占用麦克风 / 不加载模型）")
    print("=" * 66)
    test_chunker()
    test_time()
    test_skills()
    test_wake()
    test_lifecycle()
    print("\n" + "=" * 66)
    if _failures:
        print(f" 失败 {len(_failures)} 项：{_failures}")
    else:
        print(" 全部通过 √")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
