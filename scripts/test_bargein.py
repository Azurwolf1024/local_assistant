"""语音打断自测：合成「自己的回声 + 用户插话」，看检测器判断得对不对。

    python scripts/test_bargein.py            # 只跑合成测试（不碰硬件，几秒）
    python scripts/test_bargein.py --live     # ★ 额外做一次真机回环（外放一段话，你插一句）

为什么要合成测试：麦克风同时听得到扬声器，能不能分清「回声」和「插话」是这个功能的
全部难点。用真实 TTS 音频按不同泄漏量/信噪比拼出假信号，就能确定性地量一遍，
不用每次都占用扬声器和麦克风。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.bargein import BargeInDetector  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def synth(text: str, rate: int = 22050):
    """用 piper 合成一段语音，当作「播出去的内容」或「用户说的话」。"""
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
    return pcm, rate


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    n = int(round(len(x) * dst / src))
    idx = np.linspace(0, len(x) - 1, n)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


def clip_energy(x: np.ndarray) -> float:
    return float(np.mean(np.square(x)))


def gain_for_snr(echo: np.ndarray, user: np.ndarray, leak_energy: float, snr: float) -> float:
    """要让「用户到达麦克风的能量 / 回声能量 = snr」，用户该乘多大增益。

    这才是决定性参数：现在的判定完全不看参考电平，只看麦克风自己的回声基准，
    所以「用户比自己的回声响多少」直接决定能不能打断。
    """
    e_echo_mic = leak_energy * clip_energy(echo)
    e_user = clip_energy(user)
    if e_user <= 0:
        return 0.0
    return float(np.sqrt(max(0.0, snr) * e_echo_mic / e_user))


def run_case(
    name: str,
    *,
    echo: np.ndarray,
    user: np.ndarray | None,
    user_gain: float,
    leak_energy: float,
    noise: float,
    rate: int,
    frame_size: int,
    expect: bool,
    user_at: float = 2.0,
    det=None,
    strict: bool = True,
):
    """把两条轨道按帧喂给检测器。返回 (是否触发, 相对插话时刻的延迟)。

    ``leak_energy`` 是**能量比**（麦克风收到的回声能量 / 播放能量），只用来决定
    「回声有多大」；判定本身只看麦克风。``user_at`` 默认 2 秒：开头那 1 秒多是
    校准期（这段时间麦克风里只有回声），真实场景也不会一开口就插话。
    ``strict=False``：只报告结果，不计入失败（用于「已知会这样」的场景）。
    """
    cfg = load_settings()
    if det is None:
        det = BargeInDetector(cfg, frame_seconds=frame_size / rate)

    # 回声带一点延迟（扬声器→空气→麦克风），按幅度缩放 sqrt(能量比)
    delay = int(0.02 * rate)
    amp = float(np.sqrt(max(0.0, leak_energy)))
    mic_echo = np.concatenate([np.zeros(delay, dtype=np.float32), echo]) * amp
    total = len(mic_echo)
    mic = mic_echo.copy()
    if user is not None and user_gain > 0:
        start = int(user_at * rate)
        u = user[: max(0, total - start)]
        mic[start : start + len(u)] += u * user_gain
    if noise > 0:
        mic += np.random.default_rng(0).normal(0, noise, len(mic)).astype(np.float32)

    det.begin()
    hit = False
    elapsed = 0.0
    for i in range(0, total - frame_size, frame_size):
        frame = mic[i : i + frame_size]
        ref = echo[i : i + frame_size] if i < len(echo) else np.zeros(1, dtype=np.float32)
        ref_level = float(np.sqrt(np.mean(np.square(ref.astype(np.float32))))) if len(ref) else 0.0
        if det.feed(frame, ref_level):
            hit = True
            elapsed = i / rate
            break
    det.end()

    snr = 0.0
    if user is not None and user_gain > 0:
        snr = user_gain**2 * clip_energy(user) / max(1e-12, leak_energy * clip_energy(echo))
    ok = hit == expect
    latency = elapsed - (user_at if (user is not None and user_gain > 0) else 0.0)
    detail = (
        f"触发={hit}（期望 {expect}）"
        + (f"，插话后 {latency:.2f}s 打断" if hit else "")
        + f"；麦克风里用户/回声 = {snr:.1f} 倍能量"
        + f"；基准={det.baseline:.5f}（RMS {float(np.sqrt(det.baseline)):.4f}）"
    )
    if strict:
        check(name, ok, detail)
    else:
        print(f"  · {name}   {detail}")
    return hit, latency


def test_synthetic() -> None:
    print("\n[1] 合成测试：回声 vs 插话")
    cfg = load_settings()
    rate = int(cfg.audio.sample_rate)
    frame_size = int(cfg.audio.frame_size)

    print("  正在合成两段语音（自己的话 / 用户的话）…")
    echo, echo_rate = synth("好的，我这就把今天的安排念一遍，第一项是上午九点的机器学习课。")
    user, user_rate = synth("等一下，先别念了")
    echo = resample(echo, echo_rate, rate)
    user = resample(user, user_rate, rate)
    echo_peak = float(np.max(np.abs(echo)))
    user_peak = float(np.max(np.abs(user)))
    print(f"  自己说话峰值 {echo_peak:.3f}，用户说话峰值 {user_peak:.3f}")
    if cfg.bargein.seed_seconds:
        print(f"  校准期 {cfg.bargein.seed_seconds:.1f} 秒内不判断（这段时间只攒基准）")

    # 场景 1：只有回声，没人在说话 —— 绝不能触发（否则会自己打断自己）
    for leak in (0.01, 0.05, 0.2):
        run_case(
            f"只有回声（回声能量比 {leak}）：不该打断",
            echo=echo, user=None, user_gain=0.0, leak_energy=leak, noise=0.001,
            rate=rate, frame_size=frame_size, expect=False,
        )

    # 场景 2：耳机（回声几乎听不到）—— 用户一开口就应该打断。
    # 用 SNR 造信号：用户到达麦克风的能量是回声的 30 倍
    run_case(
        "耳机场景（回声很小，用户 30 倍）：该打断",
        echo=echo, user=user,
        user_gain=gain_for_snr(echo, user, 0.002, 30.0),
        leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True,
    )

    # 场景 3：外放，用户盖过回声（4 倍能量 = 2 倍幅度）：该打断
    run_case(
        "外放且用户 4 倍（能量）：该打断",
        echo=echo, user=user,
        user_gain=gain_for_snr(echo, user, 0.05, 4.0),
        leak_energy=0.05, noise=0.002,
        rate=rate, frame_size=frame_size, expect=True,
    )

    # 场景 4：外放，用户比回声还轻 —— 已知局限：物理上分不出来，不触发也不算错
    run_case(
        "外放且用户只有 0.5 倍（能量）：不指望打断",
        echo=echo, user=user,
        user_gain=gain_for_snr(echo, user, 0.05, 0.5),
        leak_energy=0.05, noise=0.002,
        rate=rate, frame_size=frame_size, expect=False,
    )

    # 校准期（开头 1 秒多）里就插话：基准会被你的声音顶高，这一句就打不断了。
    # 这是真实存在、而且下一句就自愈的现象，所以量出来存着，不计入失败。
    print("  · 校准期被打断（已知现象，只报告）：")
    shared = BargeInDetector(cfg, frame_seconds=frame_size / rate)
    run_case(
        "    第 1 句一开口就插话（还在校准）",
        echo=echo, user=user,
        user_gain=gain_for_snr(echo, user, 0.002, 30.0),
        leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True, user_at=0.05, det=shared,
        strict=False,
    )
    run_case(
        "    同一个检测器下一句再插话",
        echo=echo, user=user,
        user_gain=gain_for_snr(echo, user, 0.002, 30.0),
        leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True, det=shared,
    )

    # 短句首轮（「在的」这种）：整段都落在校准期里。以前这一步会估出偏低的门槛，
    # 自己的回声就能把自己打断，于是套娃起来 —— 实测就是这么出的问题。
    # 现在要求：只有回声时整段都不触发，而且基准必须压住这段回声的峰值。
    print("  · 短句首轮（真实踩坑的场景：只有回声就不能触发）：")
    short_echo = echo[: int(0.7 * rate)]
    for leak in (0.01, 0.2):
        det = BargeInDetector(cfg, frame_seconds=frame_size / rate)
        run_case(
            f"    只播了 0.7 秒（回声能量比 {leak}）",
            echo=short_echo, user=None, user_gain=0.0, leak_energy=leak, noise=0.001,
            rate=rate, frame_size=frame_size, expect=False, det=det,
        )
        peak = float(np.sqrt(leak)) * float(np.max(np.abs(short_echo)))
        check(
            "    基准压住了这段回声的峰值",
            det.baseline > 0 and det.baseline <= peak**2 + 1e-9,
            f"基准 {det.baseline:.5f} 应不超过峰值平方 {peak**2:.5f}",
        )


def test_speaker_pacing() -> None:
    """播放必须按实时节奏推进，参考电平才和「此刻在响什么」对齐。"""
    print("\n[2] 播放节奏：参考电平不能跑在声音前面")
    import time as _time

    from voice_loop.audio import Speaker

    cfg = load_settings()
    rate = 22050
    speaker = Speaker(cfg)
    try:
        # 用 1 秒静音量时长（不出声），看它是不是真的花了 1 秒
        t0 = _time.perf_counter()
        speaker.submit(np.zeros(rate, dtype=np.int16), rate)
        while (speaker.pending > 0 or speaker.speaking) and _time.perf_counter() - t0 < 6:
            _time.sleep(0.02)
        elapsed = _time.perf_counter() - t0
        check(
            "1 秒音频大约花 1 秒播完",
            0.8 <= elapsed <= 1.6,
            f"{elapsed:.2f}s（一次塞完的话会是几十毫秒，参考电平就归零了）",
        )

        # 打断要能当场停：提交 3 秒静音，0.3 秒后掐掉
        _time.sleep(0.1)
        speaker.submit(np.zeros(rate * 3, dtype=np.int16), rate)
        _time.sleep(0.3)
        speaker.interrupt()
        t1 = _time.perf_counter()
        while (speaker.pending > 0 or speaker.speaking) and _time.perf_counter() - t1 < 2:
            _time.sleep(0.02)
        stopped = _time.perf_counter() - t1
        check("打断后立刻停下（不等整块播完）", stopped < 0.5, f"{stopped:.2f}s")
    finally:
        speaker.close()


def test_self_guard() -> None:
    """自识别护栏：打断收到的语音如果就是自己刚说的，不能回答。

    这里**不开**真的 VoiceLoop：一个进程里第二个 Tk 解释器会死锁，
    而护栏只用到 ``_recent_spoken`` 和几个纯函数，裸造一个对象就够了。
    """
    print("\n[3] 自识别护栏（防止套娃）")
    import contextlib
    import io as _io
    import logging

    from voice_loop.pipeline import VoiceLoop

    settings = load_settings()
    loop = object.__new__(VoiceLoop)
    loop.settings = settings
    loop.log = logging.getLogger("voice_loop.test")
    loop._recent_spoken = []  # noqa: SLF001
    loop._from_bargein = False  # noqa: SLF001
    check("没说过话时不会误拦", not loop._looks_like_own_voice("在的"))  # noqa: SLF001
    loop._note_spoken("在的")  # noqa: SLF001
    check("完整重复自己的话 → 拦", loop._looks_like_own_voice("在的。"))  # noqa: SLF001
    # 一个字太像巧合（用户完全可能真的就回一个「好」），不做内容比对
    check("一个字不拦（太容易误伤）", not loop._looks_like_own_voice("的"))  # noqa: SLF001
    loop._note_spoken("博士，您又熬夜了，请注意休息。")  # noqa: SLF001
    check(
        "长回答里的片段 → 拦",
        loop._looks_like_own_voice("您又熬夜了"),  # noqa: SLF001
    )
    check(
        "无关的话 → 不拦",
        not loop._looks_like_own_voice("帮我查一下明天的课"),  # noqa: SLF001
    )
    check("不说过的话也过期（不会一直拦）", not loop._looks_like_own_voice("在的", window=0.0))  # noqa: SLF001

    # 端到端：走 _process，应该被拦下且不产生回答
    loop._from_bargein = True  # noqa: SLF001
    loop._note_spoken("在的")  # noqa: SLF001
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        quit_flag = loop._process("在的。", None, 0.0)  # noqa: SLF001
    check("_process 拦下了自识别（不回答）", quit_flag is False)
    check("打印了自识别提示", "[自识别]" in buf.getvalue(), buf.getvalue().strip()[:60])
    check("标记已清除，不会影响下一轮", loop._from_bargein is False)  # noqa: SLF001
    # 「只有打断来的那句才套护栏」这层门在 _process 里，靠 _from_bargein 把门，
    # 而那个标记的交接由下面的接线测试验证（listen_once 里设置/清除）


def test_echo_only() -> None:
    """真机回声自测：只外放、不用你说话，看检测器会不会被自己的回声骗到。

    这是「嵌套自识别」的复现场景——外放 + 麦克风听得见自己。合成测试再像，
    也不如让真实的扬声器和真实的麦克风演一遍：只要这里不误触发，就不会套娃。
    """
    print("\n[5] 真机回声自测（会外放三段话，不用你说话，也请先别说话）")
    cfg = load_settings()
    rate = int(cfg.audio.sample_rate)
    frame_size = int(cfg.audio.frame_size)

    from voice_loop.audio import MicReader, Speaker

    pcm, _ = synth("好的，我这就把今天的安排念一遍，第一项是上午九点的机器学习课，第二项是下午两点的组会。")
    blocks = [pcm[i : i + rate // 4] for i in range(0, len(pcm), rate // 4)]

    mic = MicReader(cfg)
    speaker = Speaker(cfg)
    det = BargeInDetector(cfg, frame_seconds=frame_size / rate)
    det.begin()
    hits = 0
    mic.open()
    mic.flush()
    try:
        for _ in range(3):
            for blk in blocks:
                speaker.submit((blk * 32767).astype(np.int16), rate)
                while speaker.pending > 0 or speaker.speaking:
                    frame = mic.read()
                    if det.feed(frame, speaker.current_level):
                        hits += 1
                        print("  · 这次误触发了，重新开始数（下面会判定失败）")
                        det.begin()
    finally:
        speaker.interrupt()
        speaker.join(timeout=3.0)
        speaker.close()
        mic.close()
        det.end()

    check("外放时没被自己的回声触发（这就是套娃的根因）", hits == 0, f"误触发 {hits} 次")
    check(
        "回声基准校准成功",
        det.baseline > 0 and not det.learning,
        f"基准 {det.baseline:.5f}（RMS {float(np.sqrt(det.baseline)):.4f}，阈值 {det.threshold:.5f}）",
    )
    if hits:
        print("        说明『麦克风自己的回声基准』压不住这台机器的回声，需要把")
        print("        [bargein] margin 调大（或 seed_seconds 调长）后再试。")


def test_live(args) -> None:
    """真机两件事：量出这台机器的回声泄漏量；再试一次真的插话打断。"""
    print("\n[5] 真机回环")
    cfg = load_settings()
    rate = int(cfg.audio.sample_rate)
    frame_size = int(cfg.audio.frame_size)

    from voice_loop.audio import MicReader, Speaker

    mic = MicReader(cfg)
    speaker = Speaker(cfg)
    pcm, _ = synth("好的，我这就把今天的安排念一遍，第一项是上午九点的机器学习课，第二项是下午两点的组会。")
    blocks = [pcm[i : i + rate // 4] for i in range(0, len(pcm), rate // 4)]
    # ---- 第一步：量回声（这段请先别说话）----
    print("  [1/2] 量回声：会外放一段话，请你先保持安静…")
    win = max(1, int(round(float(cfg.bargein.window_seconds) / (frame_size / rate))))
    mic_e: list[float] = []
    ref_e: list[float] = []
    ratios: list[float] = []
    mic.open()
    mic.flush()
    try:
        for blk in blocks:
            speaker.submit((blk * 32767).astype(np.int16), rate)
            while speaker.pending > 0 or speaker.speaking:
                frame = mic.read()
                mic_e.append(float(np.mean(np.square(frame))))
                ref_e.append(float(speaker.current_level) ** 2)
                if len(mic_e) >= win:
                    M = sum(mic_e[-win:]) / win
                    R = sum(ref_e[-win:]) / win
                    if R > 1e-6:
                        ratios.append(M / R)
    finally:
        speaker.interrupt()
    speaker.join(timeout=3.0)

    if ratios:
        leak = float(np.median(ratios))
        amp = float(np.sqrt(max(0.0, leak)))
        print(f"        回声能量/播放能量 ≈ {leak:.4f}（幅度约 {amp:.3f}）")
        print(f"        也就是扬声器的声音传到麦克风还剩约 {amp * 100:.0f}% 的幅度")
        if leak < 0.01:
            print("        → 回声很小（耳机或音量低），打断会很灵敏，用默认参数就行")
        elif leak < 0.1:
            print("        → 回声适中，默认参数可用；嫌不灵敏就把 margin 调到 1.4")
        else:
            print("        → 回声偏大，建议 margin 调到 2.0~2.5，或把播放音量调低一些")
        print(f"        想让它更灵敏：把 [bargein] margin 调小到 1.3；")
        print(f"        想让它更保守：margin 调大到 2.0，或 seed_seconds 调大到 1.5")
    else:
        print(f"        {FAIL} 没采到足够的样本（扬声器没发出声音？）")
        _failures.append("真机回声测量失败")

    # ---- 第二步：真的插一句 ----
    print(f"\n  [2/2] 试打断：会再念一遍（每次约 {args.seconds:.0f} 秒），")
    print("        请在它念的时候对着麦克风插一句「等一下」")
    det = BargeInDetector(cfg, frame_seconds=frame_size / rate)
    det.begin()
    hit_at = None
    t0 = time.perf_counter()
    try:
        for _ in range(6):
            for blk in blocks:
                if time.perf_counter() - t0 > args.seconds:
                    break
                speaker.submit((blk * 32767).astype(np.int16), rate)
                while speaker.pending > 0 or speaker.speaking:
                    frame = mic.read()
                    if det.feed(frame, speaker.current_level):
                        hit_at = time.perf_counter() - t0
                        speaker.interrupt()
                        break
                if hit_at is not None:
                    break
            if hit_at is not None or time.perf_counter() - t0 > args.seconds:
                break
    finally:
        speaker.interrupt()
        speaker.close()
        mic.close()
        det.end()

    if hit_at is None:
        print(f"  {FAIL} {args.seconds:.0f} 秒内没检测到插话")
        print("     用扬声器时属正常（回声和你说话差不多响就分不开）；戴耳机再试一次，")
        print("     或者把 [bargein] margin 调小到 1.3 / min_seconds 调到 0.2")
        _failures.append("真机打断未触发")
    else:
        print(f"  {PASS} 在第 {hit_at:.2f} 秒检测到插话并停止播放")
        print(f"     回声基准 {det.baseline:.5f}（RMS {float(np.sqrt(det.baseline)):.4f}）")


def test_pipeline_wiring() -> None:
    """接线测试：播放中说话 → 掐掉播放 → 那句话被交给下一轮 listen_once。

    用假的麦克风和假的扬声器，不出声、不占用真设备，但走的是真的
    _drain_playback / _segmenter_for / listen_once 代码路径。
    """
    print("\n[4] 管线接线：打断的那句话不能丢")
    import time as _time

    from voice_loop.bargein import BargeInDetector
    from voice_loop.pipeline import VoiceLoop

    settings = load_settings()
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.bargein.enabled = True
    # 接线测试不想等 1 秒校准期（校准本身由前面几节验）
    settings.bargein.seed_seconds = 0.4

    loop = VoiceLoop(settings, enable_listening=True, lazy_whisper=True)
    loop.tts_enabled = False
    loop._warm_heavy = lambda: None  # noqa: SLF001

    rate = int(settings.audio.sample_rate)
    frame_size = int(settings.audio.frame_size)

    user_pcm, user_rate = synth("等一下，先别念了，帮我看一下今天的日程")
    user_pcm = resample(user_pcm, user_rate, rate)
    # 前一段用很小的音量（模拟校准期里「麦克风里只有回声/底噪」），
    # 然后回到正常音量——保证判定结果确定，不受 TTS 能量波动影响
    quiet = (
        user_pcm[: int(1.0 * rate)] * 0.15
        if len(user_pcm) > int(1.0 * rate)
        else user_pcm * 0.15
    )
    loud = np.concatenate(
        [
            np.zeros(frame_size * 2, dtype=np.float32),
            quiet,
            user_pcm,
            np.zeros(frame_size * 20, dtype=np.float32),
        ]
    )

    class FakeMic:
        """按帧喂脚本音频：先静音，再「用户说话」，最后静音让它收尾。"""

        def __init__(self, frames: np.ndarray) -> None:
            self.frames = frames
            self.i = frame_size * 8          # 前 8 帧当静音

        def open(self) -> None: ...
        def flush(self) -> int:
            return 0

        def close(self) -> None: ...

        def read(self) -> np.ndarray:
            _time.sleep(0.002)
            if self.i + frame_size <= len(self.frames):
                out = self.frames[self.i : self.i + frame_size]
                self.i += frame_size
                return out
            return np.zeros(frame_size, dtype=np.float32)

    class FakeSpeaker:
        """假装还有 seconds 秒没播完，播放电平固定，可以被 interrupt。

        ``pending`` 必须跟 ``speaking`` 一起归零：不然判定不触发时
        ``_drain_playback`` 永远等不到「播完了」（这个坑踩过一次）。
        """

        def __init__(self, seconds: float, level: float = 0.25) -> None:
            self.until = _time.monotonic() + seconds
            self._level = level
            self.interrupted = False

        @property
        def pending(self) -> int:
            return 0 if (self.interrupted or _time.monotonic() >= self.until) else 1

        @property
        def speaking(self) -> bool:
            return not self.interrupted and _time.monotonic() < self.until

        @property
        def current_level(self) -> float:
            return 0.0 if self.interrupted or not self.speaking else self._level

        def interrupt(self) -> None:
            self.interrupted = True

        def join(self, timeout: float | None = None) -> bool:
            return True

        def submit(self, pcm, rate) -> None: ...

        def close(self) -> None: ...

    fake_mic = FakeMic(loud)
    fake_speaker = FakeSpeaker(3.0)
    loop.mic = fake_mic          # type: ignore[assignment]
    loop.speaker = fake_speaker  # type: ignore[assignment]
    loop.bargein = BargeInDetector(settings, frame_seconds=frame_size / rate)

    t0 = _time.perf_counter()
    interrupted = loop._drain_playback()  # noqa: SLF001
    cost = _time.perf_counter() - t0

    check("播放中检测到插话并返回打断标记", interrupted)
    check("扬声器已被打断（不再继续播）", fake_speaker.interrupted)
    check("打断标记已置位（LLM 那边也会停）", loop._interrupt.is_set())  # noqa: SLF001
    check("打断得够快（<2 秒）", cost < 2.0, f"{cost:.2f}s")

    captured = loop._barge_audio  # noqa: SLF001
    check(
        "用户那句话被收下来了（没白说）",
        captured is not None and captured.size > rate * 0.5,
        f"{0 if captured is None else captured.size / rate:.2f}s 音频",
    )

    # 关键：这句话必须能被下一轮直接取走，不然就等于丢了
    taken = loop.listen_once(quick=False, timeout=0.1)
    check("下一轮 listen_once 直接拿到它", taken is not None and taken is captured)
    check("带上「打断来源」标记（护栏靠它把门）", loop._from_bargein is True)  # noqa: SLF001
    check("取走后不会重复给", loop._barge_audio is None)  # noqa: SLF001
    # 正常录音（没等到话就说超时）不能带着这个标记，否则下一句真话会被当成自识别
    check("正常录音不带标记", loop.listen_once(timeout=0.05) is None)  # noqa: SLF001
    check("标记已清除", loop._from_bargein is False)  # noqa: SLF001

    loop._stop.set()  # noqa: SLF001
    loop.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外做一次真机回环（会用到扬声器和麦克风）")
    ap.add_argument(
        "--echo",
        action="store_true",
        help="额外做一次真机回声自测：只外放三段话，不用你说话",
    )
    ap.add_argument("--seconds", type=float, default=12.0, help="真机回环的时长上限")
    ap.add_argument(
        "--trace",
        type=float,
        default=0.0,
        help="N 秒后把所有线程的调用栈打出来（卡死时排查用，等于 N 秒超时）",
    )
    args = ap.parse_args()

    if args.trace:
        # 卡住时最需要的就是「现在到底停在哪一行」，这个能直接打出来
        import faulthandler

        faulthandler.dump_traceback_later(args.trace, exit=True)

    test_synthetic()
    test_speaker_pacing()
    test_self_guard()
    test_pipeline_wiring()
    if args.echo:
        test_echo_only()
    if args.live:
        test_live(args)

    print("\n" + "=" * 66)
    print(" 全部通过 √" if not _failures else f" {len(_failures)} 项未通过：{_failures}")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())