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
    user_at: float = 1.0,
    det=None,
):
    """把两条轨道按帧喂给检测器。返回 (是否触发, 相对插话时刻的延迟)。

    ``leak_energy`` 是**能量比**（麦克风收到的回声能量 / 播放能量），
    和检测器内部的泄漏系数同口径。因为能量是幅度的平方，
    幅度上的泄漏约等于 ``sqrt(leak_energy)``。
    ``user_at``：用户从第几秒开始插话——真实场景都是说到一半才插嘴。
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

    ok = hit == expect
    latency = elapsed - (user_at if (user is not None and user_gain > 0) else 0.0)
    detail = (
        f"触发={hit}（期望 {expect}）"
        + (f"，插话后 {latency:.2f}s 打断" if hit else "")
        + f"；泄漏系数收敛到 {det.leak_estimate:.3f}（真值 {leak_energy:.3f}）"
    )
    check(name, ok, detail)
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

    # 场景 1：只有回声，没人在说话 —— 绝不能触发（否则会自己打断自己）
    for leak in (0.01, 0.05, 0.2):
        run_case(
            f"只有回声（能量比 {leak}）：不该打断",
            echo=echo, user=None, user_gain=0.0, leak_energy=leak, noise=0.001,
            rate=rate, frame_size=frame_size, expect=False,
        )

    # 场景 2：耳机（几乎没回声）—— 用户一开口就应该打断
    hit, latency = run_case(
        "耳机场景（能量比 0.002）：该打断",
        echo=echo, user=user, user_gain=1.0, leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True,
    )
    check("打断够快（插话后 <0.8s）", hit and 0 <= latency < 0.8, f"{latency:.2f}s")

    # 场景 3：外放，用户说话明显大于回声 —— 该打断
    run_case(
        "外放且用户够响（能量比 0.05，用户 1.0）",
        echo=echo, user=user, user_gain=1.0, leak_energy=0.05, noise=0.002,
        rate=rate, frame_size=frame_size, expect=True,
    )

    # 场景 4：外放，用户声音比回声还小 —— 已知局限：分不出来，不触发也不算错
    run_case(
        "外放且用户很轻（能量比 0.2，用户 0.15）",
        echo=echo, user=user, user_gain=0.15, leak_energy=0.2, noise=0.002,
        rate=rate, frame_size=frame_size, expect=False,
    )

    # 学习期（默认 0.5 秒）里就插话：泄漏系数会被你说话的声音带高，
    # 但中位数估计还能工作，而且之后的快速下降分支会把它拉回真实值。
    # 这是一个「真实存在但能自愈」的现象，所以这里量出来存着，不判对错。
    print("  · 学习期被打断（已知现象，只报告）：")
    shared = BargeInDetector(cfg, frame_seconds=frame_size / rate)
    run_case(
        "    第 1 句一开口就插话（还在学习期）",
        echo=echo, user=user, user_gain=1.0, leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True, user_at=0.05, det=shared,
    )
    run_case(
        "    同一个检测器第 2 句再插话",
        echo=echo, user=user, user_gain=1.0, leak_energy=0.002, noise=0.001,
        rate=rate, frame_size=frame_size, expect=True, user_at=0.05, det=shared,
    )
    check(
        "    带偏后能自愈（泄漏系数回落到 0.05 以内）",
        shared.leak_estimate < 0.05,
        f"最终 {shared.leak_estimate:.4f}（真实 0.002）",
    )


def test_live(args) -> None:
    """真机两件事：量出这台机器的回声泄漏量；再试一次真的插话打断。"""
    print("\n[2] 真机回环")
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
        print(f"        回声泄漏系数 ≈ {leak:.4f}（能量比；幅度约 {amp:.3f}）")
        print(f"        也就是扬声器的声音传到麦克风还剩约 {amp * 100:.0f}% 的幅度")
        if leak < 0.01:
            print("        → 回声很小（耳机或音量低），打断会很灵敏，用默认参数就行")
        elif leak < 0.1:
            print("        → 回声适中，默认参数可用；嫌不灵敏就把 margin 调到 1.4")
        else:
            print("        → 回声偏大，建议 margin 调到 2.0~2.5，或把播放音量调低一些")
        print(f"        想固定下来：把 [bargein] leak_init 改成 {leak:.4f}")
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
        print(f"     泄漏系数 {det.leak_estimate:.4f}")


def test_pipeline_wiring() -> None:
    """接线测试：播放中说话 → 掐掉播放 → 那句话被交给下一轮 listen_once。

    用假的麦克风和假的扬声器，不出声、不占用真设备，但走的是真的
    _drain_playback / _segmenter_for / listen_once 代码路径。
    """
    print("\n[3] 管线接线：打断的那句话不能丢")
    import time as _time

    from voice_loop.bargein import BargeInDetector
    from voice_loop.pipeline import VoiceLoop

    settings = load_settings()
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.bargein.enabled = True

    loop = VoiceLoop(settings, enable_listening=True, lazy_whisper=True)
    loop.tts_enabled = False
    loop._warm_heavy = lambda: None  # noqa: SLF001

    rate = int(settings.audio.sample_rate)
    frame_size = int(settings.audio.frame_size)

    user_pcm, user_rate = synth("等一下，先别念了，帮我看一下今天的日程")
    user_pcm = resample(user_pcm, user_rate, rate)
    silence = np.zeros(frame_size, dtype=np.float32)
    loud = np.concatenate(
        [np.zeros(frame_size * 2, dtype=np.float32), user_pcm, np.zeros(frame_size * 20, dtype=np.float32)]
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
        """假装还有 seconds 秒没播完，播放电平固定，可以被 interrupt。"""

        def __init__(self, seconds: float, level: float = 0.25) -> None:
            self.until = _time.monotonic() + seconds
            self._level = level
            self.interrupted = False

        @property
        def pending(self) -> int:
            return 0 if self.interrupted else 1

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
    check("取走后不会重复给", loop._barge_audio is None)  # noqa: SLF001

    loop._stop.set()  # noqa: SLF001
    loop.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外做一次真机回环（会用到扬声器和麦克风）")
    ap.add_argument("--seconds", type=float, default=12.0, help="真机回环的时长上限")
    args = ap.parse_args()

    test_synthetic()
    test_pipeline_wiring()
    if args.live:
        test_live(args)

    print("\n" + "=" * 66)
    print(" 全部通过 √" if not _failures else f" {len(_failures)} 项未通过：{_failures}")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
