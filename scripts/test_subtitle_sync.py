"""字幕与语音的进度同步（不需要真窗口，也不出声）。

要守住的：
- 字幕只显示「已经念到的地方」→ 长回答折叠时折掉的一定是**念过的部分**，
  正在念的那句永远在屏幕上
- 进度是「已播音频秒数」插值出来的 → 段中间也会推进，不用等整句念完才跳
- 句与句之间的小空白算在同一轮里（不然会闪一下全文）
- 这一轮说完 → 放开限制显示全文（方便回头读）
- 关掉语音播报 / 关掉 sync_speech → 不限制，老行为

    python scripts/test_subtitle_sync.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.subtitle import SubtitleOverlay  # noqa: E402

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


# --------------------------------------------------------------------------- #
BODY = "第一句话在这里。第二句话在这里。第三句还在后面没念到呢。"


class FakeSpeaker:
    """够用的扬声器替身：只暴露字幕判断要用的两个量（外加收下音频）。"""

    def __init__(self, pending: int = 0, speaking: bool = False) -> None:
        self.pending = pending
        self.speaking = speaking
        self.submitted: list[int] = []

    def submit(self, pcm, rate: int) -> None:
        self.submitted.append(len(pcm))


def fake_loop(marks, *, playing: bool, anchor_age: float = 0.0, last_submit_age: float = 0.0,
              tts: bool = True):
    """造一个只带进度追踪字段的假 pipeline（不建模型、不碰声卡）。"""
    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)
    loop.tts_enabled = tts
    loop._sync_active = True
    loop._speech_marks = list(marks)
    loop._spoken_chars = marks[-1][0] if marks else 0
    loop._spoken_seconds = marks[-1][1] if marks else 0.0
    loop._play_anchor_audio = 0.0
    loop._play_anchor_time = (time.monotonic() - anchor_age) if playing else 0.0
    loop._last_submit_at = time.monotonic() - last_submit_age
    loop.speaker = FakeSpeaker(pending=2 if playing else 0, speaking=playing)
    return loop


def test_subtitle_slice() -> None:
    print("\n[1] 字幕按「念到哪儿」裁剪（不装 Tk）")
    sub = SubtitleOverlay(enabled=False)  # enabled=False 不建窗口，也能用纯逻辑
    sub._body = BODY

    check(sub._visible_body(), BODY, "没有进度信息 → 显示全文（老行为）")

    sub.set_progress(lambda: 6)
    sub._poll_progress()
    check(sub._visible_body(), BODY[:6], "念到第 6 个字 → 只显示前 6 个字")

    sub.set_progress(lambda: 999)
    sub._poll_progress()
    check(sub._visible_body(), BODY, "进度超出正文长度 → 夹到全文（不越界）")

    sub.set_progress(lambda: 0)
    sub._poll_progress()
    check(sub._visible_body(), "", "还没出声 → 一个字都不显示")

    sub.set_progress(lambda: -5)
    sub._poll_progress()
    check(sub._visible_body(), "", "负值也当 0（不越界）")

    sub.set_progress(None)
    sub._poll_progress()
    check(sub._visible_body(), BODY, "撤掉进度源 → 回到全文")


def test_collapse_keeps_current_sentence() -> None:
    print("\n[2] 折叠发生在念过的部分（这就是用户要的）")
    sub = SubtitleOverlay(enabled=False, max_lines=4)
    long_body = "".join(f"第{i}句话说得比较长一些，用来把行数顶上去。" for i in range(1, 9))
    sub._body = long_body

    # 没同步时：显示的是「末尾」，也就是还没念到的后文
    tail = sub._visible_body()
    check_true(tail == long_body, "不同步时整段都算可见（折叠只留末尾）")

    # 同步后：进度在中间 → 可见内容一定以「当前这句」收尾
    cut = len(long_body) // 2
    sub.set_progress(lambda: cut)
    sub._poll_progress()
    check(sub._visible_body(), long_body[:cut], "同步后只到念过的地方")
    check_true(
        long_body[:cut].endswith(sub._visible_body()[-6:]),
        "结尾就是正在念的那句（不是没念到的后文）",
    )


def test_pipeline_position() -> None:
    print("\n[3] pipeline 的「念到第几个字」估算")
    marks = [(10, 1.0), (20, 2.0)]

    loop = fake_loop(marks, playing=True, anchor_age=0.5)
    pos = loop._spoken_position()
    check_true(pos is not None and 3 <= pos <= 7, f"播了 0.5s / 共 2s → 约四分之一处（得 {pos}）")

    loop = fake_loop(marks, playing=True, anchor_age=1.5)
    pos = loop._spoken_position()
    check_true(pos is not None and 13 <= pos <= 17, f"播了 1.5s → 约四分之三处（得 {pos}）")

    loop = fake_loop(marks, playing=True, anchor_age=9.0)
    check(loop._spoken_position(), 20, "播过头 → 夹在已提交的总字数")

    loop = fake_loop(marks, playing=False, last_submit_age=0.1)
    check(loop._spoken_position(), 20, "句间空白（刚提交过）→ 按已念完算，不放开全文")

    loop = fake_loop(marks, playing=False, last_submit_age=3.0)
    check(loop._spoken_position(), None, "确已说完 → 放开限制，显示全文")
    check(loop._sync_active, False, "同时把这一轮标记为结束")

    loop = fake_loop([], playing=True)
    check(loop._spoken_position(), 0, "声音还没出来 → 一个字都不显示")

    loop = fake_loop(marks, playing=True, tts=False)
    check(loop._spoken_position(), None, "关掉语音播报 → 不限制（字幕是唯一输出）")

    loop = fake_loop(marks, playing=True)
    loop._sync_active = False
    check(loop._spoken_position(), None, "这一轮没在发声（工具轮）→ 不限制")


def test_reset() -> None:
    print("\n[4] 新一句要从头算")
    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)
    loop.tts_enabled = True
    loop._sync_active = True
    loop._speech_marks = [(10, 1.0)]
    loop._spoken_chars = 10
    loop._spoken_seconds = 1.0
    loop._play_anchor_audio = 0.0
    loop._play_anchor_time = 1.0
    loop._last_submit_at = 0.0
    loop.speaker = FakeSpeaker()          # 队列空 + 没在播 → 上一句已放完
    loop.tts = _FakeTts()
    loop._interrupt = _NeverSet()

    loop._speak_chunk("这一句是新的。")
    check(loop._speech_marks[0][0], len("这一句是新的。"), "新的一句：进度从零开始记")
    check(len(loop._speech_marks), 1, "旧记录被清掉（不留上一句的）")
    check_true(loop._sync_active, "标记这一轮在发声")


class _FakeTts:
    def synth(self, text):
        yield 24000, __import__("numpy").zeros(24000, dtype="int16")  # 1 秒


class _NeverSet:
    def is_set(self) -> bool:
        return False


def main() -> int:
    test_subtitle_slice()
    test_collapse_keeps_current_sentence()
    test_pipeline_position()
    test_reset()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    print("EXIT=" + ("0" if FAIL == 0 else "1"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
