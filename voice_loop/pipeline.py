"""会话编排：VAD 断句 → ASR → 技能/LLM → 分句 TTS → 播放。

三种运行方式
    chat(mode="vad"|"ptt")   普通对话，听到说话就回答
    service()                唤醒词常驻服务：平时只等唤醒词，唤醒后进入连续对话窗口，
                             并在后台运行提醒调度器（闹钟、会议/课程提醒）
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .asr import AsrResult, AsrRouter
from .audio import BaseSegmenter, MicReader, MicRecorder, Speaker, make_segmenter, save_wav
from .llm import OllamaClient
from .scheduler import ReminderScheduler
from .settings import Settings
from .skills import Skills
from .text import SpeechChunker, is_meaningful, prepare_for_reading
from .toast import VisualNotifier
from .tts import create_tts
from .wake import WakeSession, WakeWordMatcher


# 未唤醒状态下滤掉纯语气词，否则「嗯。」「哎。」会淹没日志里有用的行
_NOISE_STRIP = re.compile(r"[\s，。、,.!！？?…~～\-—]+")
_FILLER_WORDS = {
    "嗯", "哎", "啊", "哦", "呃", "唉", "呀", "诶", "咦", "额", "唔", "嘿", "哈",
    "噢", "喔", "对", "是", "的", "了", "好", "嗯嗯", "哈哈", "呵呵", "好的",
    "谢谢", "对对", "嗯嗯嗯", "search", "try", "the", "you", "is",
}


@dataclass
class TurnStats:
    turn: int = 0
    user_text: str = ""
    asr_engine: str = ""
    asr_seconds: float = 0.0
    audio_seconds: float = 0.0
    llm_first_token: float = 0.0
    first_audio: float = 0.0
    total_seconds: float = 0.0
    answer: str = ""
    interrupted: bool = False
    extra: dict = field(default_factory=dict)


class VoiceLoop:
    """把各个组件串成一条可交互的语音链路。

    ``lazy_whisper=True`` 时进入「两级加载」模式（唤醒服务用）：
    待唤醒状态只保留 SenseVoice 与 VAD，Whisper / TTS / Ollama 模型
    都在被唤醒后才加载，空闲超时后再释放。
    """

    def __init__(
        self,
        settings: Settings,
        logger=None,
        preload: bool = True,
        enable_skills: bool = True,
        enable_listening: bool = True,
        lazy_whisper: bool = False,
    ) -> None:
        import logging

        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop")
        self.cfg = settings

        # 两级加载：待唤醒时只跑 SenseVoice
        self.lazy = bool(lazy_whisper)

        # 纯文本模式（调试技能 / 调音色）不需要麦克风与 ASR，也就不加载它们
        self.enable_listening = bool(enable_listening)
        self.mic = MicReader(settings) if self.enable_listening else None
        self.segmenter: BaseSegmenter | None = (
            make_segmenter(settings, self.log) if self.enable_listening else None
        )
        self._seg_key: bool | None = False
        self.asr: AsrRouter | None = (
            AsrRouter(
                settings, self.log, preload=preload, whisper_enabled=not self.lazy
            )
            if self.enable_listening
            else None
        )
        self.speaker = Speaker(settings)
        self.llm = OllamaClient(settings.llm)
        self.tts = create_tts(settings, self.log, lazy=self.lazy)

        # 技能与提醒
        self.skills: Skills | None = Skills(settings, self.log) if enable_skills else None
        self.scheduler: ReminderScheduler | None = (
            ReminderScheduler(self.skills, settings, self._on_reminder, self.log)
            if self.skills
            else None
        )

        # 可视提醒：提醒响起时在右下角弹一个窗户，没开扬声器也不会错过
        self.toast: VisualNotifier | None = None
        if self.skills and getattr(settings.skills, "visual_alert", False):
            try:
                self.toast = VisualNotifier(
                    enabled=True,
                    timeout=float(settings.skills.visual_timeout),
                    logger=self.log,
                )
                self.toast.start()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"可视提醒初始化失败：{exc}")
                self.toast = None

        # 唤醒词
        self._wake_path = settings.resolve(settings.wake.file)
        WakeWordMatcher.write_default(self._wake_path)
        self.wake = WakeWordMatcher(self._wake_path)
        self._idle_timeout = float(
            self.wake.settings.idle_timeout or settings.wake.idle_timeout
        )
        self._followup_window = float(settings.wake.followup_window)
        self.session = WakeSession(self._idle_timeout)
        self._stop_file = settings.resolve(settings.wake.stop_file)
        self._pid_file = settings.resolve(settings.wake.pid_file)
        self._active = False          # 重型模型是否已加载
        self._warm_thread: threading.Thread | None = None
        self._last_miss = ""          # 上一次没命中的短句（避免刷屏）

        self.tts_enabled = True

        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._muted = threading.Event()      # 播放期间忽略麦克风，避免自我唤醒
        self._speak_lock = threading.Lock()  # 保证同一时刻只有一处发声
        self._in_reply = False
        self._watcher: threading.Thread | None = None
        self._turn = 0
        self._session_file = settings.sessions_dir / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    # ======================================================================
    # 预热
    # ======================================================================
    def warmup(self, warm_llm: bool = True) -> dict:
        info: dict = {"asr": self.asr.available if self.asr else []}
        if warm_llm:
            t0 = time.perf_counter()
            self.llm.ensure_model()
            info["llm_load_seconds"] = self.llm.warmup()
            info["llm_check_seconds"] = time.perf_counter() - t0
        info["tts"] = self.tts.benchmark()
        if self.asr:
            info["asr_warm_seconds"] = self.asr.warmup()
        if self.skills:
            info["skills"] = self.skills.stats()
        return info

    def _flush_mic(self) -> None:
        if self.mic is not None:
            self.mic.flush()

    # ======================================================================
    # 采集
    # ======================================================================
    def _segmenter_for(self, quick: bool) -> BaseSegmenter:
        """待唤醒状态用更灵敏的断句器（静音判定更短）。"""
        key = bool(quick)
        if self._seg_key != key or self.segmenter is None:
            min_silence = self.wake.settings.min_silence if key else None
            self.segmenter = make_segmenter(
                self.settings, self.log, min_silence=min_silence, quiet=True
            )
            self._seg_key = key
            self.log.debug(f"断句器切换为 {'唤醒' if key else '对话'} 模式")
        return self.segmenter

    def listen_once(self, quick: bool = False, timeout: float | None = None) -> np.ndarray | None:
        """等一句话说完并返回音频；等待超时返回 None。

        ``timeout`` 可覆盖配置里的 ``listen_timeout``（用于唤醒后的短暂跟随窗口）。
        """
        if self.mic is None:
            raise RuntimeError("当前是纯文本模式，没有麦克风")
        audio_cfg = self.settings.audio
        rate = int(audio_cfg.sample_rate)
        frame_size = int(audio_cfg.frame_size)
        max_frames = int(audio_cfg.max_record_seconds * rate / frame_size)
        wait = float(audio_cfg.listen_timeout if timeout is None else timeout)

        self.mic.open()
        self.mic.flush()  # 丢掉播放期间积压的旧音频，避免自我唤醒
        segmenter = self._segmenter_for(quick)
        segmenter.reset()

        started = False
        frames = 0
        t_start = time.time()
        noticed = False
        while not self._stop.is_set():
            frame = self.mic.read()
            if self._muted.is_set():
                # 正在播放：丢弃这段音频并重置断句状态（否则会自己唤醒自己）
                if not noticed and float(np.max(np.abs(frame))) > 0.05:
                    noticed = True
                    print(
                        "  [提示] 我刚在说话，这段语音被忽略了；想让我听，等我说完或按回车打断",
                        flush=True,
                    )
                segmenter.reset()
                started = False
                frames = 0
                t_start = time.time()
                continue
            noticed = False
            if started:
                frames += 1
            else:
                frames = 0
            utterance = segmenter.accept(frame)
            if utterance is not None:
                return utterance
            if getattr(segmenter, "speech_detected", False):
                if not started:
                    started = True
                frames = max(frames, 1)
                t_start = time.time()
            if not started and wait and (time.time() - t_start) > wait:
                return None
            if started and frames > max_frames:
                # 只有真正开始说话后才按「单轮最长时长」截断
                self.log.warning("单轮录音超长，强制切断")
                parts = segmenter.flush()
                segmenter.reset()
                started = False
                frames = 0
                t_start = time.time()
                if parts:
                    return parts[0]
                continue
        return None

    def record_ptt(self) -> np.ndarray:
        """按键模式：回车开始、回车结束。"""
        print("  [回车] 按回车开始说话…", end="", flush=True)
        input()
        rec = MicRecorder(self.mic)
        rec.start()
        print("  ● 录音中… 再按回车结束", flush=True)
        input()
        return rec.stop()

    # ======================================================================
    # 发声
    # ======================================================================
    def _submit_chunks(self, text: str) -> None:
        tts_cfg = self.settings.tts
        chunker = SpeechChunker(
            max_chars=int(tts_cfg.max_chunk_chars),
            first_min_chars=int(tts_cfg.first_chunk_min_chars),
            min_chunk_chars=int(tts_cfg.min_chunk_chars),
            max_hold_seconds=0.0,
        )
        for chunk in chunker.feed(text) + chunker.flush():
            for rate, pcm in self.tts.synth(chunk):
                self.speaker.submit(pcm, rate)

    def speak_text(self, text: str, wait: bool = True) -> float:
        """直接朗读一段文字（技能回答、提醒播报）。"""
        text = prepare_for_reading(text)
        if not text or not self.tts_enabled:
            return 0.0
        t0 = time.perf_counter()
        self._muted.set()
        try:
            with self._speak_lock:
                self._submit_chunks(text)
                if wait:
                    self.speaker.join()
        finally:
            self._muted.clear()
            self._flush_mic()
        return time.perf_counter() - t0

    def _on_reminder(self, text: str) -> None:
        """调度器线程回调：弹窗 + 播报提醒（会等当前回答播完再说话）。"""
        print(f"\n\n【提醒】{text}\n", flush=True)
        if self.toast is not None:
            title = "日程提醒" if ("日程" in text or "课" in text or "会议" in text) else "提醒"
            self.toast.show(title, text)
        self.speak_text(text)
        # 待唤醒状态下播报完就把 TTS 释放掉，别让它一直占内存
        if self.lazy and not self._active:
            unload = getattr(self.tts, "unload", None)
            if callable(unload):
                unload()

    # ======================================================================
    # 两级加载：唤醒 → 加载重型模型；空闲超时 → 释放
    # ======================================================================
    def _activate(self, reason: str = "唤醒") -> None:
        """进入活跃状态：加载应答要用的模型，重型模型放后台预热。"""
        if self._active:
            return
        self._active = True
        t0 = time.perf_counter()
        if self.asr is not None:
            self.asr.set_whisper_enabled(True)
        if self.lazy:
            # 要马上应答，TTS 前台加载；Whisper / LLM 放后台，不阻塞回复
            try:
                load = getattr(self.tts, "load", None)
                if callable(load):
                    load()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"TTS 加载失败：{exc}")
            self._warm_thread = threading.Thread(
                target=self._warm_heavy, daemon=True, name="warm"
            )
            self._warm_thread.start()
        self.log.info(f"[{reason}] 进入活跃状态（{time.perf_counter() - t0:.2f}s）")

    def _warm_heavy(self) -> None:
        """后台预热 Whisper 与 Ollama 模型，让第一句话更快得到回答。"""
        if self.asr is not None and self.lazy and not self.asr.whisper_loaded:
            t0 = time.perf_counter()
            try:
                if self.asr.load_whisper():
                    self.asr.warmup()
                    self.log.info(f"Whisper 已就绪（后台加载 {time.perf_counter() - t0:.1f}s）")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"Whisper 后台加载失败：{exc}")
        try:
            self.llm.ensure_model()
            t0 = time.perf_counter()
            self.llm.warmup()
            self.log.info(f"LLM 已就绪（预热 {time.perf_counter() - t0:.1f}s）")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"LLM 预热失败：{exc}")

    def _deactivate(self) -> bool:
        """回到待唤醒：卸载重型模型。返回 True 表示应该结束进程。"""
        if not self._active:
            return False
        self._active = False
        freed: list[str] = []
        if self.lazy:
            # 加锁：确保没有正在进行的播报被中途抽掉
            with self._speak_lock:
                if self.asr is not None and self.asr.whisper_loaded:
                    self.asr.set_whisper_enabled(False)
                    freed.append("Whisper")
                unload = getattr(self.tts, "unload", None)
                if callable(unload) and getattr(self.tts, "loaded", False):
                    unload()
                    freed.append("TTS")
            if self.settings.wake.unload_llm and self.llm.release():
                freed.append(f"Ollama/{self.settings.llm.model}")
        if freed:
            print(f"\n[待唤醒] 已释放：{'、'.join(freed)}", flush=True)

        if (self.settings.wake.idle_action or "standby").lower() == "exit":
            print(
                f"[退出] 空闲超过 {self._idle_timeout:.0f} 秒（{self._idle_timeout / 60:.1f} 分钟），服务结束。",
                flush=True,
            )
            self._stop.set()
            return True
        words = self.wake.settings.words
        print(f"\n[待唤醒] 请说「{words[0] if words else ''}」…\n", flush=True)
        return False

    def _idle_desc(self) -> str:
        if self._idle_timeout >= 120:
            return f"空闲 {self._idle_timeout / 60:.0f} 分钟"
        return f"空闲 {self._idle_timeout:.0f} 秒"

    def _check_stop_file(self) -> bool:
        """后台运行时用停止文件优雅退出（比 taskkill 干净）。"""
        try:
            if self._stop_file.exists():
                self._stop_file.unlink()
                print("[停止] 收到停止信号，正在退出…", flush=True)
                return True
        except OSError:
            pass
        return False

    # ======================================================================
    # 应答
    # ======================================================================
    def respond(
        self,
        user_text: str,
        on_delta=None,
        asr: AsrResult | None = None,
        asr_seconds: float = 0.0,
    ) -> TurnStats:
        """先尝试本地技能，未命中再走 LLM；两种情况下都是边说边播。"""
        stats = TurnStats(turn=self._turn, user_text=user_text)
        if asr is not None:
            stats.asr_engine = asr.engine
            stats.audio_seconds = asr.audio_seconds
            stats.extra["asr_latency"] = round(asr.latency, 4)
            stats.extra["asr_rtf"] = round(asr.rtf, 4)
            stats.extra["asr_score"] = round(asr.score, 4)
        stats.asr_seconds = asr_seconds

        # ---------------------------------------------------------- 技能路径
        skill = self.skills.handle(user_text) if self.skills else None
        if skill is not None:
            stats.extra["skill"] = skill.action
            stats.answer = skill.reply
            if on_delta is not None:
                on_delta(skill.reply)
            stats.total_seconds = self.speak_text(skill.reply)
            stats.first_audio = stats.total_seconds
            self.llm.commit(user_text, skill.reply)
            self._write_session(stats)
            return stats

        # ------------------------------------------------------------ LLM 路径
        tts_cfg = self.settings.tts
        chunker = SpeechChunker(
            max_chars=int(tts_cfg.max_chunk_chars),
            first_min_chars=int(tts_cfg.first_chunk_min_chars),
            min_chunk_chars=int(tts_cfg.min_chunk_chars),
            max_hold_seconds=float(tts_cfg.max_hold_seconds),
        )
        self._interrupt.clear()
        self._in_reply = True
        self._muted.set()
        t0 = time.perf_counter()
        pieces: list[str] = []
        first_audio: float | None = None

        def speak(sentence: str) -> None:
            nonlocal first_audio
            if not self.tts_enabled:
                return
            for rate, pcm in self.tts.synth(sentence):
                if first_audio is None:
                    first_audio = time.perf_counter() - t0
                self.speaker.submit(pcm, rate)

        try:
            with self._speak_lock:
                for delta in self.llm.chat_stream(user_text):
                    if stats.llm_first_token == 0.0:
                        stats.llm_first_token = time.perf_counter() - t0
                    pieces.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                    if self._interrupt.is_set():
                        break
                    for sentence in chunker.feed(delta):
                        speak(sentence)
                        if self._interrupt.is_set():
                            break
                    if self._interrupt.is_set():
                        break

                if not self._interrupt.is_set():
                    for sentence in chunker.flush():
                        speak(sentence)
                    self.speaker.join()
        finally:
            self._in_reply = False
            self._muted.clear()
            self._flush_mic()
            self.speaker.join(timeout=30.0)

        answer = "".join(pieces).strip()
        stats.answer = answer
        stats.interrupted = self._interrupt.is_set()
        stats.first_audio = first_audio or 0.0
        stats.total_seconds = time.perf_counter() - t0
        self.llm.commit(user_text, answer)
        self._write_session(stats)
        return stats

    # ======================================================================
    # 交互
    # ======================================================================
    def _start_interrupt_watcher(self) -> None:
        """播放过程中按回车即可打断。后台运行时没有终端，自动跳过。"""
        try:
            if sys.stdin is None or not sys.stdin.isatty():
                self.log.debug("当前没有交互终端，跳过「回车打断」监听")
                return
        except Exception:  # noqa: BLE001
            return

        def watch() -> None:
            while not self._stop.is_set():
                line = sys.stdin.readline()
                if line == "":
                    time.sleep(0.2)
                    continue
                if self._in_reply or self.speaker.speaking or self.speaker.pending:
                    self._interrupt.set()
                    self.speaker.interrupt()
                    print("  [打断] 已打断", flush=True)

        self._watcher = threading.Thread(target=watch, daemon=True, name="interrupt")
        self._watcher.start()

    def use_wake_file(self, path: str | Path) -> None:
        """切换唤醒词文件（供 listen --wake-file 使用）。"""
        self._wake_path = Path(path)
        self.wake.path = self._wake_path
        self.wake.load(force=True)
        self._idle_timeout = float(
            self.wake.settings.idle_timeout or self.settings.wake.idle_timeout
        )
        self.session.timeout = self._idle_timeout

    def _is_exit(self, text: str) -> bool:
        t = text.strip().strip("。！!？?，,、 ")
        return any(p and p in t and len(t) <= len(p) + 4 for p in self.settings.chat.exit_phrases)

    def _transcribe(self, audio: np.ndarray) -> tuple[str, AsrResult, float]:
        rate = int(self.settings.audio.sample_rate)
        t0 = time.perf_counter()
        result = self.asr.transcribe(audio, rate)
        return result.text.strip(), result, time.perf_counter() - t0

    def _process(self, text: str, result: AsrResult | None, asr_seconds: float = 0.0) -> bool:
        """处理一句识别结果，返回 True 表示要退出。"""
        if self._is_exit(text):
            print(f"\n你说：{text}\n助手：再见！\n")
            return True

        tag = ""
        if result is not None:
            tag = (
                f"\n      [ASR {result.engine} {result.latency:.2f}s"
                f" / 音频 {result.audio_seconds:.1f}s / RTF {result.rtf:.2f}"
                f" / 全流程 {asr_seconds:.2f}s]"
            )
        print(f"\n你说：{text}{tag}\n")
        print("助手：", end="", flush=True)
        stats = self.respond(
            text,
            on_delta=lambda d: print(d, end="", flush=True),
            asr=result,
            asr_seconds=asr_seconds,
        )
        if stats.extra.get("skill"):
            print(f"\n      [本地技能 {stats.extra['skill']} / 播报 {stats.total_seconds:.2f}s]")
        else:
            print(
                f"\n      [首字 {stats.llm_first_token:.2f}s / 首音 {stats.first_audio:.2f}s"
                f" / 总耗时 {stats.total_seconds:.2f}s]"
            )
        if stats.answer == "" and not stats.interrupted:
            print("      （没有返回内容，请检查 Ollama 服务）")
        return False

    # ---------------------------------------------------------------- 普通对话
    def chat(self, mode: str | None = None) -> None:
        mode = (mode or self.settings.chat.mode or "vad").lower()
        print(
            f"\n=== 本地语音助手已启动 ===\n"
            f"  ASR : {self.settings.asr.strategy} / {'+'.join(self.asr.available) or '未加载'}\n"
            f"  LLM : {self.settings.llm.model} @ {self.settings.llm.host}\n"
            f"  TTS : piper / {self.settings.tts.voice}\n"
            f"  模式: {'自动断句（直接说话，停顿即发送）' if mode == 'vad' else '回车录制'}"
            + (f"\n  技能: {self.skills.stats()}" if self.skills else "")
            + f"\n  提示: 说「{self.settings.chat.exit_phrases[0]}」退出"
            + ("；回答过程中按回车可打断" if mode == "vad" else "")
            + "\n"
        )

        self.mic.open()
        if mode == "vad":
            self._start_interrupt_watcher()
        if self.scheduler:
            self.scheduler.start()

        try:
            while not self._stop.is_set():
                try:
                    audio = self.listen_once() if mode == "vad" else self.record_ptt()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(f"录音出错（已忽略）：{exc}")
                    time.sleep(0.3)
                    continue
                if audio is None or audio.size == 0:
                    continue
                if self.settings.app.save_audio:
                    save_wav(
                        self.settings.sessions_dir / f"turn{self._turn + 1:03d}.wav",
                        audio,
                        int(self.settings.audio.sample_rate),
                    )

                self._turn += 1
                try:
                    text, result, asr_seconds = self._transcribe(audio)
                    if not is_meaningful(text):
                        continue
                    if self._process(text, result, asr_seconds):
                        break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.error(f"这一轮处理失败（继续对话）：{type(exc).__name__}: {exc}")
                    continue
        except KeyboardInterrupt:
            print("\n已退出。")
        finally:
            self._stop.set()
            if self.scheduler:
                self.scheduler.stop()

    # ---------------------------------------------------------------- 唤醒服务
    def service(self) -> None:
        """唤醒词常驻服务。

        两级加载（``[wake] lazy_load = true``，默认）：
            待唤醒 —— 只跑 Silero VAD + SenseVoice，Whisper / TTS / Ollama 都不占内存
            被唤醒 —— 加载 TTS 应答，后台同时预热 Whisper 与 Ollama
            空闲超时 —— 释放上面那些重型模型，回到待唤醒
        """
        wall = self.wake.settings.words
        words = "、".join(wall) or "（未配置）"
        asr_desc = f"{self.settings.asr.strategy} / SenseVoice" + (
            " 常驻 + Whisper 按需" if self.lazy else " + Whisper"
        )
        print(
            f"\n=== 本地语音助手 · 常驻服务 ===\n"
            f"  ASR : {asr_desc}\n"
            f"  LLM : {self.settings.llm.model} @ {self.settings.llm.host}\n"
            f"  TTS : piper / {self.settings.tts.voice}\n"
            f"  唤醒词: {words}    (可直接编辑 {self._wake_path}，保存即生效)\n"
            f"  空闲回收: {self._idle_desc()}"
            + ("（则释放模型回到待唤醒）" if self.lazy else "（则结束服务）")
            + "\n"
            + (f"  技能: {self.skills.stats()}\n" if self.skills else "")
            + "  提示: Ctrl+C 退出；回答过程中按回车可打断\n"
        )

        if not self.wake.enabled:
            print("[警告] 唤醒词未启用或列表为空，将退化为普通对话模式。\n")

        # 在日志里也记一份 PID，pid 文件被意外删掉时 stop 能靠它恢复
        self.log.info(f"服务已启动 PID={os.getpid()}")

        self.mic.open()
        self._start_interrupt_watcher()
        if self.scheduler:
            self.scheduler.start()
        if not self.lazy:
            # 不分级时模型已经加载好了，直接算活跃
            self._active = True
        print(f"\n[待唤醒] 请说「{wall[0] if wall else '凯尔希'}」…\n")

        try:
            while not self._stop.is_set():
                if self._check_stop_file():
                    break
                try:
                    if self.wake.maybe_reload():
                        self._idle_timeout = float(
                            self.wake.settings.idle_timeout or self.settings.wake.idle_timeout
                        )
                        self.session.timeout = self._idle_timeout
                        print(f"[唤醒词已更新] {'、'.join(self.wake.settings.words)}\n")
                    # 空闲超时 -> 收回资源
                    if self._active and not self.session.active:
                        if self._deactivate():
                            break
                    in_session = self.session.active or not self.wake.enabled
                    # 等待上限压到 5 秒：这样空闲回收、唤醒词热加载、stop 信号
                    # 都能在几秒内生效，而不是被 listen_timeout（默认 30s）拖住。
                    base_wait = float(self.settings.audio.listen_timeout) or 5.0
                    audio = self.listen_once(quick=not in_session, timeout=min(5.0, base_wait))
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(f"录音出错（已忽略）：{exc}")
                    time.sleep(0.3)
                    continue
                if audio is None or audio.size == 0:
                    continue

                self._turn += 1
                try:
                    text, result, asr_seconds = self._transcribe(audio)
                    if not text:
                        continue

                    if in_session:
                        self.session.open()
                        if not is_meaningful(text):
                            continue
                        if self._process(text, result, asr_seconds):
                            break
                        continue

                    hit = self.wake.match(text)
                    if hit is None:
                        # 短句很可能是用户在喊唤醒词但被识别错了，提示一下
                        # （长句就不刷屏了，那是正常说话）
                        plain = text.strip()
                        key = _NOISE_STRIP.sub("", plain)
                        # 单个字的「嗯/哎/啊」以及「好的/谢谢」这类是环境杂音，
                        # 写进日志只会淹没真正有用的行
                        short = (
                            is_meaningful(text)
                            and 2 <= len(key) <= 8
                            and key not in _FILLER_WORDS
                        )
                        if short and plain != self._last_miss:
                            self._last_miss = plain
                            print(
                                f"[未唤醒] 听到：{plain}    "
                                f"若是唤醒词，请把它加进 {self._wake_path.name} 的 aliases",
                                flush=True,
                            )
                        else:
                            self.log.debug(f"[未唤醒] {text}")
                        continue

                    mark = "（模糊匹配）" if hit.fuzzy else ""
                    print(f"\n[已唤醒]{mark} {text}")
                    self.session.open()
                    request = self.wake.strip_word(text, hit)
                    follow_result: tuple | None = None

                    if len(request) < 2 and self._followup_window > 0:
                        # 「凯尔希……现在几点了」这种名字后面带停顿的说法会被 VAD 切成两句。
                        # 注意：必须赶在加载 TTS 之前把下半句收进来，
                        # 否则加载那两三秒里说的话就被 flush 掉了。
                        follow = self.listen_once(quick=False, timeout=self._followup_window)
                        if follow is not None:
                            ftext, fresult, fseconds = self._transcribe(follow)
                            if is_meaningful(ftext):
                                follow_result = (ftext, fresult, fseconds)
                                request = ""

                    # 唤醒后再加载重型模型（Whisper 与 LLM 在后台预热）
                    self._activate("唤醒" + mark)

                    if follow_result is not None:
                        ftext, fresult, fseconds = follow_result
                        self.session.open()
                        if self._process(ftext, fresult, fseconds):
                            break
                        continue

                    if len(request) >= 2:
                        if self._process(request, result, asr_seconds):
                            break
                    else:
                        ack = (self.wake.settings.ack or "").strip()
                        if ack:
                            print(f"助手：{ack}")
                            self.speak_text(ack)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.error(f"这一轮处理失败（继续监听）：{type(exc).__name__}: {exc}")
                    continue
        except KeyboardInterrupt:
            print("\n已退出。")
        finally:
            self._stop.set()
            if self.scheduler:
                self.scheduler.stop()

    # ---------------------------------------------------------------- 文本模式
    def text_repl(self, speak: bool = True) -> None:
        """纯文字交互，走完整的「技能 → LLM → TTS」链路。

        排查语调和技能时非常有用：不用对着麦克风喊，打字即可。
        """
        self.tts_enabled = bool(speak)
        print(
            f"\n=== 文本模式（{'含语音播报' if speak else '仅文字'}）===\n"
            f"  输入内容回车发送；/help 查看命令；exit 退出。\n"
            + (f"  技能: {self.skills.stats()}\n" if self.skills else "")
        )
        if self.scheduler:
            self.scheduler.start()
        try:
            while not self._stop.is_set():
                try:
                    line = input("\n你> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                if line.lower() in ("exit", "quit", "退出", "再见"):
                    break
                if line == "/help":
                    print("  /skills  查看技能数据统计与文件位置")
                    print("  /speak   开关语音播报")
                    print("  exit     退出")
                    continue
                if line == "/skills":
                    if self.skills:
                        print(f"  {self.skills.stats()}")
                        for name, path in (
                            ("闹钟", self.skills.alarms.path),
                            ("备忘", self.skills.memos.path),
                            ("日程", self.skills.schedule.path),
                        ):
                            print(f"  {name}: {path}")
                    continue
                if line == "/speak":
                    self.tts_enabled = not self.tts_enabled
                    print(f"  语音播报已{'开启' if self.tts_enabled else '关闭'}")
                    continue

                try:
                    stats = self.respond(line, on_delta=lambda d: print(d, end="", flush=True))
                    print()
                    if stats.extra.get("skill"):
                        print(f"  [本地技能 {stats.extra['skill']} / 播报 {stats.total_seconds:.2f}s]")
                    else:
                        print(
                            f"  [首字 {stats.llm_first_token:.2f}s / 首音 {stats.first_audio:.2f}s"
                            f" / 总耗时 {stats.total_seconds:.2f}s]"
                        )
                except KeyboardInterrupt:
                    print("\n  [已打断]")
                    self._interrupt.set()
                    self.speaker.interrupt()
                except Exception as exc:  # noqa: BLE001
                    self.log.error(f"处理失败：{type(exc).__name__}: {exc}")
        finally:
            if self.scheduler:
                self.scheduler.stop()

    # ======================================================================
    # 收尾
    # ======================================================================
    def _write_session(self, stats: TurnStats) -> None:
        try:
            with open(self._session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(stats), ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._stop.set()
        if self.scheduler:
            self.scheduler.stop()
        if self.toast is not None:
            try:
                self.toast.stop()
            except Exception:  # noqa: BLE001
                pass
            self.toast = None
        try:
            self.speaker.close()
        finally:
            if self.mic is not None:
                self.mic.close()
            if self.asr is not None:
                self.asr.close()
            if self.lazy:
                unload = getattr(self.tts, "unload", None)
                if callable(unload):
                    unload()
                if self.settings.wake.unload_llm:
                    self.llm.release()
            # 只删属于自己的 pid 文件：
            # 否则 ask / text / 各种测试脚本一退出，就会把后台服务的 pid 文件误删掉
            try:
                if self._pid_file.exists():
                    owner = self._pid_file.read_text(encoding="utf-8").strip()
                    if owner == str(os.getpid()):
                        self._pid_file.unlink()
            except OSError:
                pass
