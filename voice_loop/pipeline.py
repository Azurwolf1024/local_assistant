"""会话编排：VAD 断句 → ASR → 技能/LLM → 分句 TTS → 播放。

三种运行方式
    chat(mode="vad"|"ptt")   普通对话，听到说话就回答
    service()                唤醒词常驻服务：平时只等唤醒词，唤醒后进入连续对话窗口，
                             并在后台运行提醒调度器（闹钟、会议/课程提醒）
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .asr import AsrResult, AsrRouter
from .audio import BaseSegmenter, MicReader, MicRecorder, Speaker, make_segmenter, save_wav
from .bargein import BargeInDetector
from .llm import OllamaClient, OllamaError
from .scheduler import ReminderScheduler
from .settings import Settings
from .skills import Skills
from .subtitle import SubtitleOverlay
from .text import SpeechChunker, is_meaningful, prepare_for_reading
from .toast import VisualNotifier
from .tools import (
    SUSPICIOUS_START,
    TOOL_HINT,
    ToolRegistry,
    describe_calls,
    looks_like_tool_text,
    parse_tool_call_text,
)
from .tts import create_tts
from . import ui
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
        # 工具层：技能没接住的话交给模型时，让它能自己查/记（见 voice_loop/tools.py）
        self.tools: ToolRegistry | None = (
            ToolRegistry(settings, self.skills, self.log) if self.skills else None
        )
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

        # 屏幕底部居中的半透明字幕：关掉声音（或戴着耳机走开了）也能跟着看
        self.subtitle: SubtitleOverlay | None = None
        sub_cfg = getattr(settings, "subtitle", None)
        if sub_cfg is not None and sub_cfg.enabled:
            try:
                self.subtitle = SubtitleOverlay(
                    enabled=True,
                    width=int(sub_cfg.width),
                    alpha=float(sub_cfg.alpha),
                    hold_seconds=float(sub_cfg.hold_seconds),
                    font_size=int(sub_cfg.font_size),
                    max_lines=int(sub_cfg.max_lines),
                    show_user_text=bool(sub_cfg.show_user_text),
                    margin=int(sub_cfg.margin),
                    logger=self.log,
                )
                self.subtitle.start()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"字幕初始化失败：{exc}")
                self.subtitle = None

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

        # 语音打断：它还在说话时你一开口就停下来听你说
        self.bargein: BargeInDetector | None = (
            BargeInDetector(settings, self.log)
            if (self.enable_listening and getattr(settings, "bargein", None) and settings.bargein.enabled)
            else None
        )
        self._barge_seg: BaseSegmenter | None = None
        self._barge_buf: list[np.ndarray] = []
        self._barge_deadline = 0.0
        self._barge_keep = 0
        self._barge_triggered = False
        self._barge_listening = False
        # 最近说过的话（用来识别「自己的声音被麦克风捡回来」）
        self._recent_spoken: list[tuple[float, str]] = []
        self._from_bargein = False
        # 最近几轮对话原文（你说 / 助手答），供技能解析「它、那个、刚才那条」指谁
        self._dialog: deque[str] = deque(maxlen=8)

        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._muted = threading.Event()      # 播放期间忽略麦克风，避免自我唤醒
        self._needs_flush = False            # 播放过之后，下次监听前要清一次回声
        self._speak_lock = threading.Lock()  # 保证同一时刻只有一处发声
        self._in_reply = False
        self._barge_audio: np.ndarray | None = None   # 被语音打断时收到的那句话
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
        """丢掉驱动缓冲区里的陈旧音频（刚播完音、刚被唤醒时用）。"""
        self._needs_flush = False
        if self.mic is not None:
            self.mic.flush()

    def _mark_played(self) -> None:
        """播放时麦克风也在采集，标记「下次监听前先清掉自己的回声」。"""
        self._needs_flush = True

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
        # 上一轮被打断时已经把他的话收下来了，直接交给上层处理，不然就白丢了
        if self._barge_audio is not None:
            audio, self._barge_audio = self._barge_audio, None
            self._from_bargein = True      # 标记来源：这句要靠「自识别护栏」把关
            self.log.debug(f"使用打断时收到的语音：{audio.size} 采样")
            return audio
        # 正常录音的这一句不是打断来的：把标记清掉。不清的话，万一上一轮
        # 打断来的音频没走到 _process（比如待机时被当成没喊唤醒词丢掉了），
        # 这个标记会一直留着，把后面某句真话误判成自识别。
        self._from_bargein = False
        audio_cfg = self.settings.audio
        rate = int(audio_cfg.sample_rate)
        frame_size = int(audio_cfg.frame_size)
        max_frames = int(audio_cfg.max_record_seconds * rate / frame_size)
        wait = float(audio_cfg.listen_timeout if timeout is None else timeout)

        self.mic.open()
        # 只有刚播过音才清缓冲。以前每次进入监听都无条件 flush：
        # 待唤醒时每 5 秒就清一次，万一你正好在那一刻开口，
        # 唤醒词的开头就被丢掉了（这是「待机久了唤醒变难」的一个原因）。
        if self._needs_flush:
            self.mic.flush()
            self._needs_flush = False
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
                if self._interrupt.is_set():
                    return
                self.speaker.submit(pcm, rate)

    def speak_text(self, text: str, wait: bool = True, fresh: bool = False) -> float:
        """直接朗读一段文字（技能回答、提醒播报）。"""
        text = prepare_for_reading(text)
        if not text:
            return 0.0
        # 字幕先上屏：即使关掉了语音播报（tts_enabled=False）也要看得见回答
        if self.subtitle is not None:
            if fresh:
                # 提醒 / 唤醒应答不属于某一轮对话，别把上一轮的「你说：…」留在屏幕上
                self.subtitle.clear()
            self.subtitle.update(text)
        if not self.tts_enabled:
            return 0.0
        # 这是一句新的发言：先把上一轮遗留的打断标记清掉，
        # 否则它会一开始就被自己掐断（提醒播报尤其明显）
        self._interrupt.clear()
        self._note_spoken(text)
        t0 = time.perf_counter()
        self._muted.set()
        self._mark_played()
        try:
            with self._speak_lock:
                self._submit_chunks(text)
                if wait:
                    self._drain_playback()
        finally:
            self._barge_finish()
            self._muted.clear()
            self._flush_mic()
        return time.perf_counter() - t0

    def _on_reminder(self, text: str) -> None:
        """调度器线程回调：弹窗 + 播报提醒（会等当前回答播完再说话）。"""
        print(f"\n\n【提醒】{text}\n", flush=True)
        if self.toast is not None:
            title = "日程提醒" if ("日程" in text or "课" in text or "会议" in text) else "提醒"
            self.toast.show(title, text)
        self.speak_text(text, fresh=True)
        # 待唤醒状态下播报完就把 TTS 释放掉，别让它一直占内存
        if self.lazy and not self._active:
            unload = getattr(self.tts, "unload", None)
            if callable(unload):
                unload()

    # ======================================================================
    # 播放 + 语音打断
    # ======================================================================
    def _drain_playback(self, step: bool = False) -> bool:
        """等播放队列放完。

        ``step=True`` 时只走一步（流式生成阶段用来顺手听一下有没有人插话），
        返回「是否已经因为用户说话而打断」。

        开打断时，这里会一边等一边读麦克风：一旦判断是用户在说话，
        立刻掐掉播放、把已说出口的那句话收完，存进 ``_barge_audio``，
        下一次 :meth:`listen_once` 会直接把它当成用户这一轮的输入。
        """
        bi = self.bargein
        if bi is None:
            if not step:
                self.speaker.join()
            return self._interrupt.is_set()

        if not self._barge_listening:
            bi.begin()
            self._barge_listening = True
            self._barge_triggered = False
            self._barge_buf = []
            self._barge_seg = None
            audio_cfg = self.settings.audio
            dt = float(audio_cfg.frame_size) / float(audio_cfg.sample_rate)
            self._barge_keep = max(1, int(self.settings.bargein.keep_seconds / dt))
            self._barge_deadline = 0.0

        try:
            while True:
                playing = self.speaker.pending > 0 or self.speaker.speaking
                if not self._barge_triggered:
                    if not playing or self._interrupt.is_set():
                        break
                elif time.time() > self._barge_deadline:
                    break

                frame = self.mic.read()

                if not self._barge_triggered:
                    if bi.feed(frame, self.speaker.current_level):
                        self._barge_triggered = True
                        self._interrupt.set()
                        self.speaker.interrupt()
                        print("\n  [打断] 听到你说话了，先停下听你说", flush=True)
                        self._barge_seg = self._segmenter_for(False)
                        self._barge_seg.reset()
                        self._barge_deadline = time.time() + float(
                            self.settings.bargein.collect_seconds
                        )
                        # 把触发前攒着的音频补进断句器：不然你开口的第一个字会被切掉
                        for old in self._barge_buf:
                            done = self._barge_seg.accept(old)
                            if done is not None:
                                self._barge_audio = done
                                return True
                        self._barge_buf = []
                    else:
                        self._barge_buf.append(frame)
                        if len(self._barge_buf) > self._barge_keep:
                            self._barge_buf.pop(0)
                else:
                    done = self._barge_seg.accept(frame)
                    if done is not None:
                        self._barge_audio = done
                        return True

                if step:
                    break
        except Exception as exc:  # noqa: BLE001
            # 打断检测出错不该影响正常说话，记一笔就好
            self.log.warning(f"打断检测出错（已忽略）：{exc}")
        return self._barge_triggered

    def _barge_finish(self) -> None:
        """一轮发言结束：收拾打断检测的状态，并把回声校准结果定下来。

        必须在一轮发言的所有出口都调到（包括被 step 模式打断提前退出的情况），
        否则 `_barge_triggered` 会留到下一轮，下一轮一开口就被当成「已经打断」。
        """
        if self.bargein is None or not self._barge_listening:
            return
        self._barge_listening = False
        self._barge_buf = []
        if self._barge_triggered and self._barge_seg is not None:
            # 用户还没说完（或断句没等到）就超时了：把已经收到的先用上
            parts = self._barge_seg.flush()
            if parts and self._barge_audio is None:
                self._barge_audio = parts[0]
            self._barge_seg.reset()
        self._barge_seg = None
        self._barge_triggered = False
        self._barge_deadline = 0.0
        self.bargein.end()

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
                    if self._active:
                        self.asr.warmup()
                        self.log.info(
                            f"Whisper 已就绪（后台加载 {time.perf_counter() - t0:.1f}s）"
                        )
                    else:
                        # 加载过程中已经回到待唤醒，load_whisper 会自己收拾，这里不再预热
                        self.log.info("Whisper 加载完成前已回到待唤醒，跳过预热")
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
        self._needs_flush = True   # 刚才在说话，下一轮监听前得把积压的回声丢掉
        freed: list[str] = []
        if self.lazy:
            # 加锁：确保没有正在进行的播报被中途抽掉
            with self._speak_lock:
                if self.asr is not None:
                    # 无条件关（不是「已加载才关」）：后台预热线程可能正在加载 Whisper，
                    # 那样 `whisper_loaded` 还是 False；不关的话它加载完就留在内存里，
                    # 待唤醒状态会变成每句都跑几秒的 Whisper，再次唤醒又慢又不准。
                    if self.asr.set_whisper_enabled(False):
                        freed.append("Whisper")
                unload = getattr(self.tts, "unload", None)
                if callable(unload) and getattr(self.tts, "loaded", False):
                    unload()
                    freed.append("TTS")
            if self.settings.wake.unload_llm and self.llm.release():
                freed.append(f"Ollama/{self.settings.llm.model}")
            # 视觉模型（qwen2.5vl 之类）一个就 6 GB 上下，看完图就该让它走
            vmodel = str(self.settings.vision.model or "")
            if (
                self.settings.wake.unload_llm
                and self.settings.vision.enabled
                and vmodel
                and vmodel != self.settings.llm.model
                and self.llm.release(vmodel)
            ):
                freed.append(f"Ollama/{vmodel}")
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

        # 字幕：先清掉上一轮的内容（否则流式 append 会越滚越长），
        # 再把「你说：…」放上去——听错了一眼就能看出来
        if self.subtitle is not None:
            self.subtitle.clear()
            self.subtitle.show_user(user_text)

        # ---------------------------------------------------------- 技能路径
        # 带上最近几轮对话，技能才能把「它 / 那个 / 刚才那条」对上号
        skill = (
            self.skills.handle(user_text, dialog=list(self._dialog)) if self.skills else None
        )
        if skill is not None and skill.action == "vision":
            return self._respond_vision(stats, skill, user_text, on_delta)
        if skill is not None:
            # 上上轮可能被回车/语音打断过，不清掉的话这一句回答会一开始就被掐断
            self._interrupt.clear()
            stats.extra["skill"] = skill.action
            stats.answer = skill.reply
            self._note_dialog(user_text, skill.reply)
            if on_delta is not None:
                on_delta(skill.reply)
            stats.total_seconds = self.speak_text(skill.reply)
            stats.first_audio = stats.total_seconds
            stats.interrupted = self._interrupt.is_set()
            self.llm.commit(user_text, skill.reply)
            self._write_session(stats)
            return stats

        # ------------------------------------------------------------ LLM 路径
        self._in_reply = True
        self._muted.set()
        self._mark_played()
        self._stream_answer(stats, user_text, on_delta=on_delta)
        return stats

    def _tool_specs(self) -> list[dict] | None:
        """这一轮要不要给模型工具（[llm] router = tools / chat）。"""
        if self.tools is None:
            return None
        if str(self.settings.llm.router or "tools").lower() != "tools":
            return None
        return self.tools.specs()

    def _run_tools(self, stats: TurnStats, calls: list[dict], t0: float) -> str:
        """执行模型选中的工具，返回要念的话。

        只跑一轮（不再把结果喂回去让它改写）：模型容易把「九月二十三日」改说成别的，
        而工具产出的 reply 已经是可以直接念的一句话了。
        """
        assert self.tools is not None
        results = self.tools.call_all(calls)
        stats.extra["tool"] = describe_calls(calls)
        stats.extra["tool_ok"] = all(r.ok for r in results)
        stats.extra["tool_seconds"] = round(time.perf_counter() - t0, 3)
        for r in results:
            if not r.ok:
                self.log.warning(f"[工具] 失败：{r.error or r.reply}")
        reply = " ".join(r.reply for r in results if r.reply).strip()
        if not reply:
            reply = "这件事我没做成，你再说一遍？"
        print(f"  [工具] {describe_calls(calls)} -> {reply[:60]}", flush=True)
        return reply

    def _respond_vision(
        self, stats: TurnStats, skill, user_text: str, on_delta=None
    ) -> TurnStats:
        """看图这一轮：采集到的东西交给模型，再照常边生成边播。"""
        data = dict(skill.data or {})
        images = list(data.get("images") or [])
        shot = str(data.get("shot") or "")
        stats.extra["skill"] = "vision"
        stats.extra["vision"] = str(data.get("what") or "")
        if shot:
            stats.extra["shot"] = shot
            self.log.info(f"[看图] {data.get('what', '画面')}：{shot}")
        if self.toast is not None and shot:
            self.toast.show("看图", f"{data.get('what', '画面')}\n{shot}")

        # 图片必须用视觉模型：纯文本模型看图只会编。先确认装没装，再出声。
        model = None
        if images:
            try:
                model = self.llm.resolve_model(str(self.settings.vision.model))
            except OllamaError as exc:
                hint = f"看图要视觉模型，本机还没有 {self.settings.vision.model}。先在终端跑 ollama pull {self.settings.vision.model}。"
                self.log.warning(f"[看图] {exc}")
                print(f"\n[看图] {exc}\n", flush=True)
                stats.answer = hint
                stats.extra["vision_error"] = str(exc)
                self.speak_text(hint, fresh=True)
                return stats

        # 先答一句「我看一眼。」：拍图 + 编码 + 加载模型加起来好几秒，
        # 一点声音都没有会让人以为它没听见
        note = str(data.get("note") or "").strip()
        if note:
            self.speak_text(note)
        if self.subtitle is not None:
            self.subtitle.show_user(user_text)
        self._interrupt.clear()
        self._in_reply = True
        self._muted.set()
        self._mark_played()
        try:
            self._stream_answer(
                stats,
                str(data.get("prompt") or user_text),
                images=images,
                on_delta=on_delta,
                model=model,
                num_ctx=data.get("num_ctx"),
                commit_text=user_text,
            )
        except OllamaError as exc:
            hint = "看图这一步失败了，具体原因我打在终端里了。"
            self.log.warning(f"[看图] {exc}")
            print(f"\n[看图] {exc}\n", flush=True)
            stats.answer = hint
            stats.extra["vision_error"] = str(exc)
            self.speak_text(hint, fresh=True)
        return stats

    def _stream_answer(
        self,
        stats: TurnStats,
        prompt: str,
        images: list[str] | None = None,
        on_delta=None,
        model: str | None = None,
        num_ctx: int | None = None,
        commit_text: str | None = None,
    ) -> None:
        """把 LLM 的回答边生成边播出（可带图片）。

        ``commit_text``：写进对话历史的用户话（看图时用原始那句，
        而不是塞了文件内容的那一大段 prompt）。
        """
        tts_cfg = self.settings.tts
        chunker = SpeechChunker(
            max_chars=int(tts_cfg.max_chunk_chars),
            first_min_chars=int(tts_cfg.first_chunk_min_chars),
            min_chunk_chars=int(tts_cfg.min_chunk_chars),
            max_hold_seconds=float(tts_cfg.max_hold_seconds),
        )
        self._interrupt.clear()
        t0 = time.perf_counter()
        pieces: list[str] = []
        first_audio: float | None = None
        calls: list[dict] = []
        # 有些模型会把工具调用**写成一段 JSON 文字**（而不是真的调工具）。
        # 这种文字绝对不能念出来：先攒着，看清了再决定是当工具调用还是当正常回答。
        held: list[str] = []
        holding = False
        tool_text = False
        # 看图那一轮不给工具（它有图要描述）
        tools = self._tool_specs() if (not images and self.tools is not None) else None
        prefix = [{"role": "system", "content": TOOL_HINT}] if tools else None

        def speak(sentence: str) -> None:
            nonlocal first_audio
            if not self.tts_enabled:
                return
            for rate, pcm in self.tts.synth(sentence):
                if self._interrupt.is_set():
                    return
                if first_audio is None:
                    first_audio = time.perf_counter() - t0
                self.speaker.submit(pcm, rate)

        try:
            with self._speak_lock:
                for ev in self.llm.chat_events(
                    prompt,
                    images=images,
                    model=model,
                    num_ctx=num_ctx,
                    tools=tools,
                    prefix_messages=prefix,
                ):
                    if "tool_calls" in ev:
                        calls.extend(ev["tool_calls"])
                        continue
                    if calls:
                        continue          # 已经在调工具了，后面的解释性文字不念
                    delta = ev["delta"]
                    if holding or SUSPICIOUS_START.match(delta):
                        # 可能是「把工具调用写成 JSON」：先攒着，一个字都不念
                        holding = True
                        held.append(delta)
                        joined = "".join(held)
                        tool_text = looks_like_tool_text(joined)
                        if not tool_text and len(joined) > 8 and not SUSPICIOUS_START.match(joined):
                            # 看清了：只是一段普通 JSON（例如用户要的示例）→ 放行
                            holding = False
                        if holding:
                            continue
                    if stats.llm_first_token == 0.0:
                        stats.llm_first_token = time.perf_counter() - t0
                    pieces.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                    if self.subtitle is not None:
                        self.subtitle.append(delta)
                    if self._interrupt.is_set():
                        break
                    for sentence in chunker.feed(delta):
                        speak(sentence)
                        if self._interrupt.is_set():
                            break
                    # 模型还在吐字、扬声器里也还在放：顺手听一下有没有人插话。
                    # 不这样做的话，必须等整段生成完才轮到判断，长回答会变得很钝。
                    if self._drain_playback(step=True):
                        break

                if not self._interrupt.is_set() and not calls:
                    for sentence in chunker.flush():
                        speak(sentence)
                    self._drain_playback()

            if not calls and held:
                raw = "".join(held)
                if tool_text:
                    # 把「写成文字的 JSON」抢回来当工具调用（小模型常见毛病）
                    recovered = parse_tool_call_text(raw, self.tools.names() if self.tools else None)
                    self.log.warning(f"[工具] 模型把调用写成了文字，{'已抢回' if recovered else '抢不回来'}：{raw[:120]}")
                    calls.extend(recovered)
                else:
                    # 只是普通 JSON 回答：补念出来
                    pieces.extend(held)
                    if on_delta is not None:
                        on_delta(raw)
                    if self.subtitle is not None:
                        self.subtitle.append(raw)
                    for sentence in chunker.feed(raw):
                        speak(sentence)
                    for sentence in chunker.flush():
                        speak(sentence)
                    self._drain_playback()

            if calls and self.tools is not None:
                # 模型只是「选了个工具」，真正干活的是确定性代码（见 voice_loop/tools.py）
                if stats.llm_first_token == 0.0:
                    stats.llm_first_token = time.perf_counter() - t0
                reply = self._run_tools(stats, calls, t0)
                if reply:
                    pieces = [reply]
                    if on_delta is not None:
                        on_delta(reply)
                    # 工具的话直接念：既省掉第二轮 LLM（本机约 4.6s），措辞也更可控
                    if not self._interrupt.is_set():
                        if first_audio is None:
                            first_audio = time.perf_counter() - t0
                        self.speak_text(reply)
        finally:
            self._barge_finish()
            if self._interrupt.is_set():
                # 回车打断 / 语音打断都要把剩下没放完的清掉
                self.speaker.interrupt()
            self._in_reply = False
            self._muted.clear()
            self._flush_mic()
            self.speaker.join(timeout=30.0)

        answer = "".join(pieces).strip()
        stats.answer = answer
        self._note_spoken(answer)
        self._note_dialog(commit_text or prompt, answer)
        stats.interrupted = self._interrupt.is_set()
        stats.first_audio = first_audio or 0.0
        stats.total_seconds = time.perf_counter() - t0
        self.llm.commit(commit_text or prompt, answer)
        self._write_session(stats)

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

    def _note_spoken(self, text: str) -> None:
        """记下刚说出口的话，供自识别判断。"""
        clean = _NOISE_STRIP.sub("", prepare_for_reading(text or ""))
        if len(clean) < 2:
            return
        self._recent_spoken.append((time.monotonic(), clean))
        if len(self._recent_spoken) > 6:
            self._recent_spoken.pop(0)

    def _note_dialog(self, user_text: str, answer: str) -> None:
        """记一轮对话，供技能做指代解析（「它」「那个」「刚才那条」）。

        只留原文，不做摘要：技能自己会用名字/时间在里头找出候选，
        找不准就反问，绝不猜。
        """
        for line in (f"你说：{user_text}", f"助手：{answer}"):
            if line.strip() and len(line) > 3:
                self._dialog.append(line)

    def _looks_like_own_voice(self, text: str, window: float = 30.0) -> bool:
        """这句话是不是「自己刚说的话被麦克风又捡回来了一遍」。

        打断检测靠能量估计回声，总有估不准的时候（音量突变、有人动了音箱）。
        这道护栏不看能量，而是看**内容**：如果打断收到的语音转出来就是自己刚才
        说过的话，那它一定是自识别，直接忽略——否则会变成
        「听见自己 → 回答 → 又听见自己」的嵌套。
        """
        clean = _NOISE_STRIP.sub("", text or "")
        if len(clean) < 2:
            return False
        now = time.monotonic()
        for when, spoken in self._recent_spoken:
            if now - when > window:
                continue
            if clean in spoken:
                return True
            if len(clean) >= 4 and difflib.SequenceMatcher(None, clean, spoken).ratio() >= 0.8:
                return True
        return False

    def _is_exit(self, text: str) -> bool:
        t = text.strip().strip("。！!？?，,、 ")
        return any(p and p in t and len(t) <= len(p) + 4 for p in self.settings.chat.exit_phrases)

    def _report_wake_miss(self, text: str) -> None:
        """未唤醒时给一点有用的提示：听到什么 + 离唤醒词有多近。

        这是调唤醒词最直接的依据：相似度高说明只差一点，加进 aliases 或者把
        fuzzy_ratio 降一点就行；相似度很低（像「胎儿戏」那样）降阈值没用，
        只能把那句话填进 aliases。环境里有别人说话时，这些行也是判断依据。
        """
        plain = (text or "").strip()
        key = _NOISE_STRIP.sub("", plain)
        # 单个字的「嗯/哎/啊」以及「好的/谢谢」这类是环境杂音，写进日志只会淹没有用的行
        if not plain or not is_meaningful(text) or key in _FILLER_WORDS:
            self.log.debug(f"[未唤醒] {text}")
            return

        ratio = self.wake.best_ratio(plain)
        close = ratio >= 0.6
        # 短句最可能是喊唤醒词喊错了；长句只有「很像」时才值得刷屏
        if not (2 <= len(key) <= 8 or close):
            self.log.debug(f"[未唤醒] {text}")
            return
        if plain == self._last_miss:
            self.log.debug(f"[未唤醒] {plain}")
            return
        self._last_miss = plain

        words = self.wake.settings.words
        target = words[0] if words else "唤醒词"
        if close:
            hint = (
                f"和「{target}」相似度 {ratio:.2f}，就差一点：把它加进 "
                f"{self._wake_path.name} 的 aliases，或者把 fuzzy_ratio 降到 "
                f"{max(0.5, round(ratio - 0.05, 2))}"
            )
        else:
            hint = (
                f"和「{target}」相似度 {ratio:.2f}，降阈值没用，"
                f"只能把它加进 {self._wake_path.name} 的 aliases"
            )
        print(f"[未唤醒] 听到：{plain}\n          {hint}", flush=True)

    def _transcribe(self, audio: np.ndarray) -> tuple[str, AsrResult, float]:
        rate = int(self.settings.audio.sample_rate)
        t0 = time.perf_counter()
        result = self.asr.transcribe(audio, rate)
        return result.text.strip(), result, time.perf_counter() - t0

    def _process(self, text: str, result: AsrResult | None, asr_seconds: float = 0.0) -> bool:
        """处理一句识别结果，返回 True 表示要退出。"""
        # 自识别护栏：打断收到的语音如果就是自己刚说的话，直接丢掉，不要回答
        from_barge = self._from_bargein
        self._from_bargein = False
        if from_barge and self._looks_like_own_voice(text):
            print(
                f"\n[自识别] 这句像是我自己刚说的（{text.strip()}），已忽略",
                flush=True,
            )
            self.log.warning(f"忽略自识别：{text!r}")
            return False

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
            + (
                "  打断: 你直接开口就停下听你说"
                if self.bargein is not None
                else "  打断: 只能按回车（[bargein] enabled=false）"
            )
            + "；按回车也能打断\n"
            + (f"  技能: {self.skills.stats()}\n" if self.skills else "")
            + "  提示: Ctrl+C 退出"
            + ("；字幕/提醒会显示在屏幕上\n" if self.subtitle is not None else "\n")
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
                        self._report_wake_miss(text)
                        continue

                    mark = "（模糊匹配）" if hit.fuzzy else ""
                    print(f"\n[已唤醒]{mark} {text}")
                    self._last_miss = ""
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
                            self.speak_text(ack, fresh=True)
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
        if self.subtitle is not None:
            try:
                self.subtitle.stop()
            except Exception:  # noqa: BLE001
                pass
            self.subtitle = None
        # 字幕和提醒弹窗都挂在同一个 Tk 宿主上，收尾时一起关掉，
        # 免得 Tcl 解释器在错误的线程里被回收（会打印 Tcl_AsyncDelete 之类的噪音）
        try:
            ui.shutdown()
        except Exception:  # noqa: BLE001
            pass
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
