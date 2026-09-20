"""唤醒服务「待机 → 唤醒 → 空闲回收 → 再次唤醒」的可靠性自测。

    python scripts/test_wake_cycle.py

分两段：

[1] 路由层：待唤醒状态绝不能还在跑 Whisper
    这是「待机久了再唤醒很难」的一个真凶。空闲回收时如果后台预热线程正好在加载
    Whisper，`whisper_loaded` 还是 False，老代码就跳过了关闭动作；等它加载完，
    Whisper 就留在内存里并且**仍然是启用状态**——于是待唤醒时每句话都要跑几秒的
    Whisper，又慢又容易被听错。这里连加载中的竞态一起量。

[2] 状态机：用真的 service() 循环 + 真的 ASR，喂合成出来的「凯尔希」音频，
    走一遍「唤醒 → 空闲回收 → 再次唤醒」，确认第二遍照样能唤醒、
    而且回收时 Whisper 确实被关掉了。

[3] 收回唤醒：唤醒状态下说「没事了」，几秒内就要回待唤醒——
    测试里把空闲超时拉到 60 秒，所以「几秒就回去了」只可能是被那句话赶回去的。
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
def test_router_standby() -> None:
    print("\n[1] 待唤醒时绝不能跑 Whisper（含加载竞态）")
    from voice_loop.asr.router import AsrRouter

    settings = load_settings()
    router = AsrRouter(settings, None, preload=False, whisper_enabled=True)

    # 用假引擎替换真的 Whisper：加载要 0.6 秒，方便制造「加载中」的时刻
    fake_loaded = []

    class _FakeWhisper:
        pass

    class _Real(dict):
        pass

    def fake_engine(name):
        if name == "whisper":
            time.sleep(0.6)
            fake_loaded.append(1)
            return _FakeWhisper()
        time.sleep(0.05)
        return _FakeWhisper()

    router._engine = fake_engine  # type: ignore[method-assign]
    router._sv = _FakeWhisper()

    # ---- 竞态：后台正在加载 Whisper 时回到待唤醒 ----
    started = threading.Event()

    def loader():
        started.set()
        router.load_whisper()

    t = threading.Thread(target=loader, daemon=True)
    t.start()
    started.wait(timeout=2.0)
    time.sleep(0.15)  # 确保已经进到加载里

    t0 = time.perf_counter()
    freed = router.set_whisper_enabled(False)
    elapsed = time.perf_counter() - t0
    check("关闭 Whisper 不阻塞（不会卡住服务）", elapsed < 0.3, f"耗时 {elapsed:.3f}s")
    check("加载中关不掉时返回 False（交给加载线程收尾）", freed is False)

    t.join(timeout=5.0)
    check("加载线程结束后 Whisper 已自己卸载", router.whisper_loaded is False)
    check("whisper_enabled 保持关闭", router.whisper_enabled is False)
    check("确实加载过一次（证明确实存在这个竞态窗口）", len(fake_loaded) == 1)

    # ---- 待唤醒时 transcribe 不该用 Whisper ----
    print("  · 待唤醒状态转写一段音频（只该跑 SenseVoice）")
    calls: list[str] = []

    def fake_engine2(name):
        calls.append(name)
        return _FakeWhisper()

    router._engine = fake_engine2  # type: ignore[method-assign]
    router._wh = None
    router._wh_enabled = False
    router.transcribe(np.zeros(16000, dtype=np.float32), 16000)
    check("只调用了 SenseVoice", calls == ["sensevoice"], str(calls))

    # ---- 对照：唤醒后（开启 Whisper）长音频会走 Whisper 复核 ----
    router._wh = _FakeWhisper()
    router._wh_enabled = True
    calls.clear()
    router.transcribe(np.zeros(int(16000 * 8), dtype=np.float32), 16000)
    check("唤醒后长音频会复核（调用链正常）", "whisper" in calls, str(calls))
    router.close()


# --------------------------------------------------------------------------- #
def synth_wake(text: str, rate: int = 16000) -> np.ndarray:
    """用 piper 合成一句唤醒词，当作「用户说的话」。"""
    from voice_loop.tts import create_tts

    settings = load_settings()
    tts = create_tts(settings, lazy=False)
    try:
        chunks = [pcm for _r, pcm in tts.synth(text)]
    finally:
        close = getattr(tts, "close", None)
        if callable(close):
            close()
    pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
    if int(tts.sample_rate) != rate:
        n = int(round(len(pcm) * rate / int(tts.sample_rate)))
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(
            np.float32
        )
    return pcm


def test_state_machine(idle_seconds: float = 4.0) -> None:
    print(f"\n[2] 状态机：唤醒 → 空闲回收（{idle_seconds:.0f}s）→ 再次唤醒")
    import json

    from voice_loop.pipeline import VoiceLoop
    from voice_loop.wake import is_standby, normalize

    settings = load_settings()
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.bargein.enabled = False          # 本测试不碰扬声器/麦克风
    settings.wake.idle_action = "standby"
    settings.wake.idle_timeout = idle_seconds

    loop = VoiceLoop(settings, enable_listening=True, lazy_whisper=True)
    loop.tts_enabled = False                  # 只验证状态机，不出声
    loop._idle_timeout = idle_seconds
    loop.session.timeout = idle_seconds
    loop._flush_mic = lambda: None            # 不碰真麦克风  # noqa: SLF001

    # 后台预热 Whisper 太重（1 GB / 几十秒），这里换成记账
    warmed: list[str] = []
    loop._warm_heavy = lambda: warmed.append("warm")  # noqa: SLF001

    wake_audio = synth_wake("凯尔希")
    print(f"  合成唤醒词音频：{wake_audio.size / 16000:.2f}s")
    standby_audio = synth_wake("没事了")

    # 合成音色每次都不一样，ASR 可能听成「开儿戏」之类。先让它真的识别一次，
    # 把「实际听到的说法」当成别名喂进去 —— 这样测的是状态机本身，
    # 而不是「合成音色能不能被认出来」（那是 test_wake.py 的活）。
    heard, _res, _sec = loop._transcribe(wake_audio)  # noqa: SLF001
    heard = heard.strip()
    check("合成音频能被 ASR 识别出内容", bool(heard), f"听到 {heard!r}")

    # 收回短语同理：合成音色也会把「没事了」听岔，那是 ASR 的事（test_wake.py 的活），
    # 这里把实际听到的说法临时算作收回短语，测的是状态机本身。
    heard_standby, _res2, _sec2 = loop._transcribe(standby_audio)  # noqa: SLF001
    heard_standby = heard_standby.strip()
    print(f"  合成「没事了」被听成：{heard_standby!r}")
    if not is_standby(heard_standby, settings.wake.standby_phrases):
        settings.wake.standby_phrases = [*settings.wake.standby_phrases, heard_standby]
        print("    （已把它临时加进收回短语）")
    check(
        "合成「没事了」会被当成收回短语",
        is_standby(heard_standby, settings.wake.standby_phrases),
        repr(heard_standby),
    )
    tmp_wake = settings.sessions_dir / "_test_wake_cycle.json"
    tmp_wake.write_text(
        json.dumps(
            {
                "enabled": True,
                "words": ["凯尔希"],
                "aliases": {"凯尔希": [normalize(heard)]},
                "ack": "在的",
                "idle_timeout": idle_seconds,
                "min_silence": 0.3,
                "fuzzy_ratio": 0.75,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    loop.use_wake_file(tmp_wake)
    check("别名生效（同一段音频能命中）", loop.wake.match(heard) is not None)

    # 脚本化的「麦克风」：只在待唤醒状态给唤醒词，进了会话就报「没听到」。
    # 这样两次唤醒会分别落在「第一次」和「空闲回收之后」。
    delivered = [0]

    # 脚本化的「麦克风」：只在待唤醒状态、且测试允许时才给唤醒词。
    # 用 allow_second 显式控制第二次唤醒的时机，否则它会在「刚回到待唤醒」
    # 的瞬间插进去，把状态又翻回活跃，测试就观测不到回收过程了。
    # quick=False 是唤醒之后紧接着等下半句的跟随窗口，那时候不该再塞唤醒词
    # （塞进去会被当成用户说的话发给大模型，白白跑一次 LLM）。
    delivered = [0]
    allow_second = threading.Event()
    want_standby = threading.Event()   # 测试准备好之后才递「没事了」
    standby_sent = [0]      # 「没事了」只递一次

    def scripted_listen(quick: bool = False, timeout: float | None = None):
        if quick and not loop._active:  # noqa: SLF001
            if delivered[0] == 0:
                delivered[0] = 1
                return wake_audio
            if delivered[0] == 1 and allow_second.is_set():
                delivered[0] = 2
                return wake_audio
        # 活跃状态下递一句「没事了」（这时是等指令，不是等唤醒词）；
        # 只在测试把空闲超时拉长之后才递，否则分不清「被赶回去」还是「超时回去」
        if (
            not quick
            and loop._active  # noqa: SLF001
            and want_standby.is_set()
            and standby_sent[0] == 0
        ):
            standby_sent[0] = 1
            return standby_audio
        # 模拟「这段没听到话」：睡一小会儿再返回，等价于真实的等待超时
        time.sleep(0.03)
        return None

    loop.listen_once = scripted_listen  # type: ignore[method-assign]

    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    thread = threading.Thread(target=loop.service, daemon=True, name="svc")
    try:
        thread.start()
        time.sleep(1.0)                    # 等第一次唤醒 + TTS 加载
        check("第一次唤醒后进入活跃", loop._active, f"_active={loop._active}")  # noqa: SLF001
        check("唤醒后 Whisper 被打开", loop.asr.whisper_enabled)

        # 等空闲超时：脚本一直在报「没听到」，session 到点就该回收。
        # 注意：_active 会先变 False，打印/卸模型在后面，所以三者都要等。
        print(f"  · 等 {idle_seconds + 3:.0f} 秒看它会不会回到待唤醒…")
        deadline = time.time() + idle_seconds + 8.0
        while time.time() < deadline and (
            loop._active  # noqa: SLF001
            or loop.asr.whisper_enabled
            or "[待唤醒] 已释放" not in buf.getvalue()
        ):
            time.sleep(0.1)
        check("空闲超时后回到待唤醒", not loop._active, f"_active={loop._active}")  # noqa: SLF001
        check("待唤醒时 Whisper 已关闭（不会再跑几秒一次的推理）", not loop.asr.whisper_enabled)
        check("待唤醒时 Whisper 已卸载（内存放掉）", not loop.asr.whisper_loaded)
        released = buf.getvalue().count("[待唤醒] 已释放")
        check("日志里有回收记录", released >= 1, f"{released} 次")

        # 第二次唤醒：这时才允许脚本把唤醒词递进来
        print("  · 现在再喊一次同一个唤醒词…")
        allow_second.set()
        deadline = time.time() + 12.0
        while time.time() < deadline and buf.getvalue().count("[已唤醒]") < 2:
            time.sleep(0.2)
        out_now = buf.getvalue()
        wakes = out_now.count("[已唤醒]")
        check(
            "★ 待机状态下能再次唤醒",
            wakes >= 2 and loop._active,  # noqa: SLF001
            f"日志出现 {wakes} 次「已唤醒」，_active={loop._active}",  # noqa: SLF001
        )
        first_wake = out_now.find("[已唤醒]")
        second_wake = out_now.find("[已唤醒]", first_wake + 1)
        release_at = out_now.find("[待唤醒] 已释放")
        check(
            "顺序正确：唤醒 → 回收 → 再次唤醒",
            0 <= first_wake < release_at < second_wake,
            f"位置 首次唤醒={first_wake} 回收={release_at} 再次唤醒={second_wake}",
        )

        # ---- 3) 收回唤醒：现在在活跃状态，说一句「没事了」 ----
        # 把空闲超时拉到 60 秒并重开窗口：这样「几秒内就回待唤醒」
        # 只可能是被「没事了」赶回去的，而不是超时兜的。
        loop.session.timeout = 60.0  # noqa: SLF001
        loop._idle_timeout = 60.0  # noqa: SLF001
        loop.session.open()  # noqa: SLF001
        released_before = buf.getvalue().count("[待唤醒] 已释放")
        print("  · 现在（活跃状态下）说一句「没事了」…")
        want_standby.set()
        # 窗口给到 20 秒：这一步要跑真 ASR + 卸载模型，机器同时在跑别的测试时
        # 10 秒会不够（偶发失败过两次）；而这里的空闲超时是 60 秒，
        # 20 秒内回到待唤醒仍然只可能是被「没事了」赶回去的。
        deadline = time.time() + 20.0
        while time.time() < deadline and loop._active:  # noqa: SLF001
            time.sleep(0.1)
        while time.time() < deadline and "[待唤醒] 已释放" not in buf.getvalue():
            time.sleep(0.1)
        out_now = buf.getvalue()
        check(
            "★ 说「没事了」立刻回待唤醒（空闲超时可是 60 秒）",
            not loop._active,  # noqa: SLF001
            f"_active={loop._active}",  # noqa: SLF001
        )
        check("收回时没有退出服务", not loop._stop.is_set())  # noqa: SLF001
        check("待唤醒时 Whisper 又被关掉", not loop.asr.whisper_enabled)
        check(
            "日志里有这次收回（应答 + 再次回收）",
            settings.wake.standby_reply in out_now
            and out_now.count("[待唤醒] 已释放") > released_before,
            f"回收次数 {out_now.count('[待唤醒] 已释放')}",
        )
    finally:
        allow_second.set()
        loop._stop.set()  # noqa: SLF001
        thread.join(timeout=8.0)
        sys.stdout = real_stdout

    for line in buf.getvalue().splitlines():
        if "[已唤醒]" in line or "[待唤醒] 已释放" in line:
            print(f"      {line.strip()[:90]}")
    loop.close()
    try:
        tmp_wake.unlink()
    except OSError:
        pass


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    test_router_standby()
    test_state_machine()

    print("\n" + "=" * 66)
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for f in _failures:
            print(f"   - {f}")
    else:
        print(" 全部通过 √")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
