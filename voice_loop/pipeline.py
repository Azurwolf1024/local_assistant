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
from .control import ControlChannel, reap as reap_control, serve_once as serve_control_once
from .llm import OllamaClient, OllamaError
from .mcp import MCPHost
from .names import NameCorrector
from .persona import Character, CharacterRegistry, render_system_prompt
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
    repair_args,
    reroute_correction,
)
from .memory import MemoryHub
from .memory.schedule import SharedSchedule
from .tts import BACKENDS as TTS_BACKENDS, create_tts
from .tts.zipvoice_tts import _resample
from .tts import precision
from . import ui
from .wake import WakeSession, WakeWordMatcher, is_standby


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


@dataclass
class DeferredAnswer:
    """攒着没念的模型回答（等确定要不要让技能层兜底）。

    为什么会有这么个东西：``route = model`` 时模型先选工具。它没调工具、
    而这句话又明显要动手时，正确的答话应该来自确定性层（它才知道库里有什么），
    所以这一段先不发声——问过技能层之后再决定念哪一句（见 :meth:`VoiceLoop._speak_deferred`）。
    """

    text: str = ""
    t0: float = 0.0
    first_token: float = 0.0


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
        # ★合成回听★：有 ASR 就把「说的是不是这句」的校验器接上去（见 textcheck.py）
        self._wire_text_guard()
        # ★角色声线的「默认值」快照★：切角色时用角色自己的，切回来时得能回默认，
        # 否则一个角色换过模型/声线后，另一个没配声线的角色会跟着沿用她
        self._base_voice = str(getattr(settings.tts, "voice", "") or "")
        # ★全局后端★：角色没写 backend 时就回到它（见 _apply_voice）
        self._base_backend = (str(getattr(settings.tts, "backend", "") or "piper")).strip().lower()
        self._base_clone_dir = str(getattr(settings.tts, "clone_dir", "") or "")
        self._base_clone_audio = str(getattr(settings.tts, "clone_audio", "") or "")
        self._base_clone_text = str(getattr(settings.tts, "clone_text", "") or "")
        # 技能与提醒
        self.skills: Skills | None = Skills(settings, self.log) if enable_skills else None
        # 工具层（旧的进程内注册表）：MCP 宿主没就绪时当兼底，也还是不少单测的入口
        self.tools: ToolRegistry | None = (
            ToolRegistry(settings, self.skills, self.log) if self.skills else None
        )
        # ★自己搭的 MCP 架构★：能力域拆成服务器，宿主只负责聚合与路由
        # （见 voice_loop/mcp/；工具清单与调用默认都走它）
        self.mcp: MCPHost | None = (
            MCPHost(
                settings.mcp,
                self.log,
                deps={"settings": settings, "skills": self.skills, "logger": self.log},
            )
            if self.skills and getattr(settings, "mcp", None) is not None
            else None
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
                # ★字幕跟声音同步★：还在生成回答、或者扬声器里还有没放完的音频，
                # 就一直不隐藏；真的停下来之后再停留 hold_seconds 秒（见 subtitle._tick）
                self.subtitle.set_keepalive(
                    lambda: bool(
                        self._in_reply or self.speaker.pending > 0 or self.speaker.speaking
                    )
                )
                # ★字幕只显示到「已经念到的地方」★：折叠只留末尾，
                # 长回答会把正在念的那句折掉、屏幕上反而是没念到的后文
                if bool(getattr(sub_cfg, "sync_speech", True)):
                    self.subtitle.set_progress(self._spoken_position)
                self.subtitle.start()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"字幕初始化失败：{exc}")
                self.subtitle = None

        # 唤醒词
        self._wake_path = settings.resolve(settings.wake.file)
        WakeWordMatcher.write_default(self._wake_path)
        self.wake = WakeWordMatcher(self._wake_path)
        # ★角色设定★：人设与唤醒词都来自 data/characters.json（见 voice_loop/persona.py）
        self.persona: CharacterRegistry | None = None
        self.character: Character | None = None
        if getattr(settings, "persona", None) is not None and settings.persona.enabled:
            try:
                self.persona = CharacterRegistry(settings.resolve(settings.persona.file))
                self.wake.set_characters(self.persona.all(only_enabled=True))
                self._apply_character(
                    self.persona.get(settings.persona.default) or self.persona.default(),
                    reason="启动",
                )
            except Exception as exc:  # noqa: BLE001 - 角色文件坏了也要能说话
                self.log.warning(f"角色设定加载失败（改用 config.toml 的 system_prompt）：{exc}")
                self.persona = None
                self.character = None
        # ★人名校正★：ASR 对少见人名（游戏角色名）很不敏感，先把听错的名字改回规范名，
        # 后面唤醒匹配、对话里的称呼、送给大模型的正文就都是干净的名字（见 voice_loop/names.py）。
        self.names = NameCorrector.from_characters(
            self.persona.all(only_enabled=True) if self.persona else []
        )
        if self.wake.settings.words and not self.names.names:
            self.names = NameCorrector([
                w for w in [*self.wake.settings.words, *self.wake.settings.aliases] if w
            ], self.wake.settings.aliases)
        self._idle_timeout = float(
            self.wake.settings.idle_timeout or settings.wake.idle_timeout
        )
        self._followup_window = float(settings.wake.followup_window)
        self.session = WakeSession(self._idle_timeout)
        self._stop_file = settings.resolve(settings.wake.stop_file)
        # ★控制台信箱★：网页 UI 是另一个进程，靠这个目录让服务念一句 / 切角色 / 回答
        # （见 voice_loop/control.py 的协议说明；只在常驻服务模式下轮询）
        self.control = ControlChannel(settings.resolve(settings.app.console_dir))
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
        self._hotkey = None            # 按键打断（默认 Esc）的监听线程
        # 字幕同步用：这一轮念到第几个字、已经排了多少秒音频
        self._spoken_chars = 0
        self._spoken_seconds = 0.0
        self._speech_marks: list[tuple[int, float]] = []  # (累计字数, 累计音频秒)
        self._play_anchor_audio = 0.0  # 「从这一刻开始播」对应的音频秒数
        self._play_anchor_time = 0.0
        self._last_submit_at = 0.0
        self._sync_active = False      # 这一轮说话是否还在进行（字幕据此裁显示范围）
        self._speech_begin_at = 0.0
        self._muted = threading.Event()      # 播放期间忽略麦克风，避免自我唤醒
        self._needs_flush = False            # 播放过之后，下次监听前要清一次回声
        self._speak_lock = threading.Lock()  # 保证同一时刻只有一处发声
        self._in_reply = False
        self._barge_audio: np.ndarray | None = None   # 被语音打断时收到的那句话
        self._watcher: threading.Thread | None = None
        self._turn = 0
        self._session_file = settings.sessions_dir / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        # ★记忆（四级 + 自清洁 + 知识库）★：见 voice_loop/memory/ 与工程日志第 36 节。
        # 关掉就完全回到「没有记忆」的老行为（inject/archive 都不会发生）。
        self._memory_summaries = 0
        self.memory_hub: MemoryHub | None = None
        if getattr(settings, "memory", None) is not None and settings.memory.enabled:
            try:
                self.memory_hub = MemoryHub(settings, schedule=SharedSchedule(settings),
                                            llm_call=self._memory_llm, logger=self.log)
            except Exception as exc:  # noqa: BLE001 - 记忆起不来不该影响说话
                self.log.warning(f"[记忆] 初始化失败，这次不带记忆：{exc}")

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
                        "  [提示] 我刚在说话，这段语音被忽略了；想让我听，等我说完或按 Esc 打断",
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
    _WAIT_FIRST_SECONDS = 15.0   # 开声后等第一段音频的上限（超了就放弃字幕限制）
    _GAP_SECONDS = 3.0           # 句间空白容忍度（超过就认为这一轮说完了）

    def _speech_reset(self) -> None:
        self._spoken_chars = 0
        self._spoken_seconds = 0.0
        self._speech_marks = []
        self._play_anchor_audio = 0.0
        self._play_anchor_time = 0.0

    def _speech_begin(self) -> None:
        """一轮发言开始：进度从零算起，字幕开始只显示「念过的部分」。"""
        if not self.tts_enabled:
            return
        self._speech_reset()
        self._sync_active = True
        self._speech_begin_at = time.monotonic()

    def _speech_end(self) -> None:
        """一轮发言结束：字幕放开限制，显示全文（方便回头读）。"""
        self._sync_active = False

    def _speak_chunk(self, chunk: str) -> bool:
        """合成并排队一小段，顺手把字幕进度往前推。

        返回「真的排出音频了吗」。字幕那边靠 :meth:`_spoken_position` 问
        「现在念到第几个字」，所以这里要记住每段念到了哪、以及它有多长。
        """
        if not chunk or not chunk.strip():
            return False
        # 上一句已经放完了 → 这是新的一句，进度重新算（提醒 / 技能回答常走这条）
        if self._spoken_chars and not (self.speaker.pending > 0 or self.speaker.speaking):
            self._speech_reset()
        self._sync_active = True
        if not (self.speaker.pending > 0 or self.speaker.speaking):
            # 队列是空的：这一段提交下去就会立刻开始播 → 记下播放时间锚点
            self._play_anchor_audio = self._spoken_seconds
            self._play_anchor_time = time.monotonic()
        queued = 0.0
        for rate, pcm in self.tts.synth(chunk):
            if self._interrupt.is_set():
                break
            self.speaker.submit(pcm, rate)
            queued += pcm.size / float(rate or 1)
        if not queued:
            return False
        self._spoken_chars += len(chunk)
        self._spoken_seconds += queued
        self._speech_marks.append((self._spoken_chars, self._spoken_seconds))
        self._last_submit_at = time.monotonic()
        return True

    def _played_audio_seconds(self) -> float:
        """已经播出去多少秒音频（估算）。

        队列空了就认为「全部已播」；在播的话从最后一个播放锚点按实时往上加。
        两个滑窗不要求精确对应——字幕只要「接近」声音就行。
        """
        if not self._speech_marks:
            return 0.0
        if not (self.speaker.pending > 0 or self.speaker.speaking):
            return self._spoken_seconds
        if not self._play_anchor_time:
            return 0.0
        played = self._play_anchor_audio + (time.monotonic() - self._play_anchor_time)
        return max(0.0, min(self._spoken_seconds, played))

    def _spoken_position(self) -> int | None:
        """字幕该显示到第几个字；``None`` = 不限制（显示全文）。

        它会在 UI 线程里被每帧调一次，所以只做几十次运算、不加锁。
        """
        if not self.tts_enabled or not self._sync_active:
            return None
        playing = self.speaker.pending > 0 or self.speaker.speaking
        if not playing:
            if not self._speech_marks:
                # 这一轮已经开声、但第一段还没合成出来（克隆音色要 2~3 秒）：
                # ★先一个字都不显示★，免得把还没念的全文（带折叠）先撮上屏。
                # 超出 WAIT_FIRST 秒还没声（TTS 出问题了）就放弃限制。
                if time.monotonic() - self._speech_begin_at < self._WAIT_FIRST_SECONDS:
                    return 0
                self._sync_active = False
                return None
            # 队列空：句与句之间的小空白算在同一轮里；
            # 真结束了（_speech_end）或空了很久，就把全文放开
            if time.monotonic() - self._last_submit_at < self._GAP_SECONDS:
                return self._spoken_chars
            self._sync_active = False
            return None
        if not self._speech_marks:
            return 0                      # 声音还没出来 → 一个字都先不显示
        played = self._played_audio_seconds()
        marks = list(self._speech_marks)   # 快照：pipeline 线程还在往后追加
        prev_chars, prev_sec = 0, 0.0
        for chars, sec in marks:
            if played <= sec:
                span = sec - prev_sec
                frac = 1.0 if span <= 0 else min(1.0, max(0.0, (played - prev_sec) / span))
                return int(round(prev_chars + (chars - prev_chars) * frac))
            prev_chars, prev_sec = chars, sec
        return prev_chars

    def _submit_chunks(self, text: str) -> None:
        tts_cfg = self.settings.tts
        chunker = SpeechChunker(
            max_chars=int(tts_cfg.max_chunk_chars),
            first_min_chars=int(tts_cfg.first_chunk_min_chars),
            min_chunk_chars=int(tts_cfg.min_chunk_chars),
            max_hold_seconds=0.0,
            first_chunk_max_chars=int(getattr(tts_cfg, "first_chunk_max_chars", 0) or 0),
        )
        for chunk in chunker.feed(text) + chunker.flush():
            if self._interrupt.is_set():
                return
            self._speak_chunk(chunk)

    def speak_text(self, text: str, wait: bool = True, fresh: bool = False) -> float:
        """直接朗读一段文字（技能回答、提醒播报）。"""
        text = prepare_for_reading(text)
        if not text:
            return 0.0
        # ★先告诉字幕「这一轮开始发声了」★：这样它从一开始就只显示念过的部分，
        # 不会先把全文（带折叠）闪一下再跳回去
        self._speech_begin()
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
                    self._speech_end()
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
        # 待唤醒状态下播报完就把 TTS 释放掉，别让它一直占内存
        self._speak_brief(text)

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
            # 视觉模型：现在默认跟 [llm] 同一个（qwen3.5:4b 自带视觉）；
            # 如果配成单独的 VL 模型（好几 GB），看完图就该让它走。
            # 名字相同时 release() 会被下面的去重跳过，不会白调一次。
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
        print(f"\n[待唤醒] 请说「{self._wake_hint()}」…\n", flush=True)
        return False

    def _idle_desc(self) -> str:
        if self._idle_timeout >= 120:
            return f"空闲 {self._idle_timeout / 60:.0f} 分钟"
        return f"空闲 {self._idle_timeout:.0f} 秒"

    # ======================================================================
    # 角色（人设 + 唤醒词归属）：见 voice_loop/persona.py
    # ======================================================================
    def _apply_character(self, char: Character | None, reason: str = "") -> None:
        """把角色装上：人设（system prompt）、应答语、声线、温度。

        ``char=None`` 表示回退到 config.toml 里那段 system_prompt（老行为）。
        """
        self.character = char
        if char is None:
            self.llm.system_prompt = None
            self.llm.temperature = None
            return
        extra = str(getattr(self.settings.persona, "extra_prompt", "") or "")
        self.llm.system_prompt = render_system_prompt(char, extra)
        self.llm.temperature = float(char.temperature) if char.temperature else None
        # 唤醒应答语跟着角色走（wakewords.json 里的 ack 只在没有角色时生效）
        if char.ack:
            self.wake.settings.ack = char.ack
        self._apply_voice(char, reason)
        self._seed_memory_identity(char)
        self.log.info(f"[角色] 生效：{char.label}（{reason or '设定'}）")

    def _seed_memory_identity(self, char: Character) -> None:
        """把角色的名字/身份/称呼写成 L3 的 **pinned** 事实（深层记忆的起点）。

        「我叫白泽」这类不该随时间淡化，也不该被淘汰掉——所以它们 pinned。
        幂等：人格文件是唯一真相，改了称呼下次生效时会跟着改。
        """
        hub = getattr(self, "memory_hub", None)
        if hub is None:
            return
        try:
            hub.for_character(char.id).seed_identity(char.name, char.title, char.user_title)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[记忆] 身份写入失败（不影响说话）：{exc}")

    def _tts_label(self) -> str:
        """启动横幅用的一行说明（不加载模型，纯看配置）。"""
        cfg = self.settings.tts
        if (cfg.backend or "piper").strip().lower() == "zipvoice":
            ref = str(getattr(cfg, "clone_audio", "") or "").strip()
            name = ref.replace("\\", "/").rsplit("/", 1)[-1] if ref else "未设参考音频"
            model = str(getattr(cfg, "clone_dir", "") or "").replace("\\", "/").rstrip("/")
            model = model.rsplit("/", 1)[-1] if model else ""
            return (
                f"zipvoice（模型 {model}，参考 {name}，"
                f"{int(getattr(cfg, 'clone_steps', 4) or 4)} 步）"
            )
        return f"piper / {cfg.voice}"

    def _apply_voice(self, char: Character, reason: str = "") -> None:
        """角色自带声线时换声线（Piper 换模型，克隆后端换模型+参考音频）。

        ★「不同角色唤醒不同声线」就在这一条路上★：唤醒词命中 → _switch_character
        → _apply_character → _apply_voice。

        Piper 的声线绑在 onnx 模型上，换就必须卸载重载；ZipVoice 多了个「模型目录」
        维度（微调过的角色用自己那份），同样要卸载重载；只换参考音频则当场生效。
        声线/模型/参考不存在就只警告、继续用当前的——**绝不能因为换声线把嘴弄哑了**。
        """
        # ★先定后端，再定声线★：角色可以指定自己的后端（Character.backend）。
        # 默认角色「白泽」是原创助手，固定 backend=piper —— 用**公开的出厂声线**，
        # 不跟着全局的克隆后端去用某条参考音频（那是别人的配音素材）。
        # 空 = 跟 config.toml 的 [tts] backend（也就是原来的行为）。
        want_backend = (getattr(char, "backend", "") or "").strip().lower() or self._base_backend
        cur_backend = (str(getattr(self.settings.tts, "backend", "") or "piper")).strip().lower()
        if want_backend not in TTS_BACKENDS:
            # ★写错后端名不能把嘴弄哑★：忽略它、继续用当前的（跟「声线没装就沿用旧的」一个道理）
            self.log.warning(f"角色 {char.name} 的 backend={want_backend!r} 不认识，忽略")
            want_backend = cur_backend
        if want_backend != cur_backend:
            self.settings.tts.backend = want_backend
            # 换后端必须把当前引擎卸掉（克隆那份可能几百 MB ~ 几 GB）；
            # 下次出声时按新后端重建 —— 这就是 create_tts 把「选哪一类引擎」
            # 推迟到加载那一刻的原因（见 voice_loop/tts/__init__.py 的 resolve_backend）。
            unload_old = getattr(self.tts, "unload", None)
            if callable(unload_old):
                unload_old()
            self.log.info(f"[角色] 后端：{cur_backend} → {want_backend}（{char.name} 指定）")
            print(f"[角色] TTS 后端切到 {want_backend}（{char.name} 指定的）", flush=True)
        if want_backend == "zipvoice":
            self._apply_reference(char, reason)
            return
        want = (char.voice or "").strip() or self._base_voice
        tts_cfg = self.settings.tts
        current = str(getattr(tts_cfg, "voice", "") or "")
        if not want or want == current:
            return
        model = self.settings.resolve(f"models/tts/piper/{want}.onnx")
        if not model.exists():
            self.log.warning(f"角色 {char.name} 想用声线 {want}，但没装（继续用 {current}）")
            print(
                f"[角色] 「{want}」声线没装，继续用 {current}"
                f"（想用就把 {want}.onnx 与 .onnx.json 放进 models/tts/piper/）",
                flush=True,
            )
            return
        tts_cfg.voice = want
        tts_cfg.model = f"models/tts/piper/{want}.onnx"
        tts_cfg.config = f"models/tts/piper/{want}.onnx.json"
        unload = getattr(self.tts, "unload", None)
        if callable(unload):
            unload()
        self.log.info(f"[角色] 声线：{current} → {want}（{reason or '切换'}）")

    def _apply_reference(self, char: Character, reason: str = "") -> None:
        """克隆后端下，换角色 = 换「模型目录」+「参考音频」（都没配就用回默认）。

        两种代价分开算：
        - 只换参考音频：``configure(set_reference)`` 当场生效，不用重载；
        - 换模型目录：必须 ``unload()``，下次说话时用新目录重新构造引擎
            （代价≈一次加载，实测 ZipVoice 加载几秒到十几秒）。
        模型目录里的文件不齐就不要切——宁可音色不对，也不能哑。
        """
        tts_cfg = self.settings.tts
        # ---- ① 模型目录（微调过的角色声线）----
        want_model = str(getattr(char, "voice_model", "") or "").strip()
        target_model = want_model or self._base_clone_dir
        current_model = str(getattr(tts_cfg, "clone_dir", "") or "")
        model_changed = False
        if target_model and target_model != current_model:
            if want_model:  # 角色指定的：先确认目录真的可用
                path = self.settings.resolve(want_model)
                missing = self._missing_voice_model(path) if path.is_dir() else [path]
                if missing:
                    self.log.warning(
                        f"角色 {char.name} 的声音模型不完整：{missing[0]}（继续用 {current_model}）"
                    )
                    print(
                        f"[角色] 声音模型 {want_model} 缺文件（{missing[0]}），继续用当前模型",
                        flush=True,
                    )
                else:
                    tts_cfg.clone_dir = want_model
                    model_changed = True
            else:  # 角色没配 → 回默认（比如刚从配过的角色切过来）
                tts_cfg.clone_dir = self._base_clone_dir
                model_changed = True
        # ---- ② 参考音频 ----
        want = str(getattr(char, "voice_ref", "") or "").strip() or self._base_clone_audio
        text = str(getattr(char, "voice_ref_text", "") or "").strip()
        ref_changed = False
        if want:
            path = self.settings.resolve(want)
            if not path.exists():
                self.log.warning(f"角色 {char.name} 想用参考音色 {want}，但文件不在（继续用当前参考）")
                print(f"[角色] 参考音频 {want} 不存在，继续用当前的", flush=True)
            elif want != str(getattr(tts_cfg, "clone_audio", "") or "") or text != str(
                getattr(tts_cfg, "clone_text", "") or ""
            ):
                tts_cfg.clone_audio = want
                tts_cfg.clone_text = text
                ref_changed = True
        if model_changed:
            # 构造函数会自己按 clone_audio 设参考，所以卸载后不用再 configure
            unload = getattr(self.tts, "unload", None)
            if callable(unload):
                unload()
            self.log.info(
                f"[角色] 声音模型：{current_model.rsplit('/', 1)[-1] or '（默认）'} → "
                f"{target_model.rsplit('/', 1)[-1]}（{reason or '切换'}，下次说话时加载）"
            )
        elif ref_changed:
            configure = getattr(self.tts, "configure", None)
            if callable(configure):  # 已经加载了就当场换；没加载等下次加载自然生效
                configure(lambda engine: engine.set_reference(want, text))
            self.log.info(f"[角色] 参考音色 → {want}（{reason or '切换'}）")

    def _wire_text_guard(self) -> None:
        """把「合成回听」的校验器接给 TTS（见 voice_loop/tts/textcheck.py）。

        为什么放在管线里：ASR 是管线的资源，TTS 不该依赖它（有人只想用 TTS）。
        ★只走 SenseVoice 快路径★（``prefer="sensevoice"``）：hybrid 策略会再跑一遍
        Whisper，那是给「没听清的用户语音」用的，不是给回听自己合成音的。
        """
        cfg = self.settings.tts
        min_ratio = float(getattr(cfg, "text_guard_min", 0.0) or 0.0)
        if min_ratio <= 0 or self.asr is None:
            return
        from .tts.textcheck import similarity  # noqa: PLC0415

        def verify(text: str, pcm, rate: int) -> float | None:
            try:
                audio = np.asarray(pcm, dtype=np.float32)
                if audio.size == 0:
                    return None
                if audio.dtype != np.float32 and np.max(np.abs(audio), initial=0.0) > 2.0:
                    audio = audio.astype(np.float32) / 32768.0
                if int(rate) != 16000:
                    audio = _resample(audio, int(rate), 16000)
                heard = self.asr.transcribe(audio, 16000, prefer="sensevoice").text
                if not heard.strip():
                    return None      # 没听出东西 = 判不了，别因此重采
                return similarity(text, heard)
            except Exception as exc:  # noqa: BLE001 - 校验失败绝不能影响出声
                self.log.warning(f"合成回听失败（{exc}）——这一轮不做文本校验")
                return None

        # ★必须用 on_load 而不是 configure★：管线是在「引擎还没加载」时接这一手的，
        # configure 那种「没加载就丢掉」的语义会让守卫**一声不响地失效**（见 LazyTts.on_load）。
        on_load = getattr(self.tts, "on_load", None)
        if callable(on_load):
            on_load(lambda engine: engine.set_text_verifier(verify))
        elif hasattr(self.tts, "set_text_verifier"):
            self.tts.set_text_verifier(verify)
        self.log.info(f"TTS 文本保真守卫已开（相似度 < {min_ratio:g} 就重采）")

    def _missing_voice_model(self, path) -> list[str]:
        """角色的声音模型目录缺哪些文件（空列表 = 齐了）。

        精度按 ``tts.clone_precision`` 算，且**只要有一套能用就不算缺**——
        目录里常会有 int8 / fp32 两份（做 A/B 用），报告得跟运行时一致。
        """
        wanted = ["tokens.txt", "lexicon.txt", "espeak-ng-data"]
        missing = [str(path / name) for name in wanted if not (path / name).exists()]
        if missing:
            return missing
        if precision.has_any(path):
            return []
        return [str(path / name) for name in precision.PRECISION_FILES[precision.want_precision(self.settings)]]

    def _switch_character(self, cid: str) -> Character | None:
        """唤醒词点了谁的名就切到谁（人设 + 应答语 + 声线）。"""
        if self.persona is None or not cid:
            return None
        char = self.persona.get(cid)
        if char is None:
            return None
        if self.character is not None and char.id == self.character.id:
            return char
        old = self.character.name if self.character else "（无角色）"
        self._apply_character(char, reason="切换")
        # 换人不继承上一位的口气：清掉对话历史，否则她会学着上一个人的语气说话
        self.llm.reset()
        words = "、".join(char.wake_words) or char.name
        print(f"\n[角色] {old} → {char.label}（喊「{words}」切过来，喊别人的名字就切走）", flush=True)
        return char

    def _wake_hint(self) -> str:
        """提示该喊什么：多角色时把所有角色的主唤醒词列出来。"""
        words = [w for w in (self.wake.character_words or self.wake.settings.words) if w]
        return "」或「".join(words[:3]) if words else "唤醒词"

    def _resolve_address(self, text: str) -> tuple[str | None, str]:
        """★对话进行中★也认一下「你在叫谁」：返回 ``(要处理的请求, 提示语)``。

        - 喊**当前这位**的名字（含听错的写法）→ 把名字摘掉，剩下的当请求
          （「开尔西，现在几点了」→「现在几点了」）；
        - 整句只有名字 → 返回 ``None``（上层回一句应答语，别把名字本身送给大模型）；
        - 喊的是**别人** → 原样放过，只提示一句怎么切（对话中间不换人）。

        ★为什么要有它★：唤醒之后没人会每次都规规矩矩先喊名字。原来这条路上完全不看
        唤醒词，名字会被当成请求的一部分送进大模型（听着就是「它没听懂我在叫它」）。
        文本里听错的名字在这之前已经被 :mod:`voice_loop.names` 改成规范名了，
        所以这里既认「凯尔希」也认「开尔西」。
        """
        if self.wake is None or not self.wake.enabled:
            return text, ""
        strict = float(getattr(self.wake.settings, "fuzzy_ratio", 0.75) or 0.75)
        loose = float(getattr(self.wake.settings, "session_fuzzy_ratio", 0.0) or 0.0) or strict
        hit = self.wake.match(text, ratio=loose)
        if hit is None:
            return text, ""

        who = self.persona.get(hit.character) if (self.persona and hit.character) else None
        current = self.character.id if self.character else ""
        if who is not None and who.id != current:
            # 对话中不换人（用户明确说过不要）：提醒一句就行，别把名字摘掉
            return text, f"[对话中] 你喊的是 {who.name}——想换人先说「没事了」，再喊名字"

        request = self.wake.strip_word(text, hit).strip()
        mark = "（模糊）" if hit.fuzzy else ""
        if len(request) < 2:
            return None, f"[对话中认名]{mark} 认出「{hit.word}」"
        return request, ""

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

    def _serve_console(self, limit: int = 4) -> int:
        """把控制台投过来的命令做掉（念一句 / 切角色 / 文字问答）。返回处理条数。

        ★绝不能醒着失败把服务弄挂★：这里抓所有异常只记日志。命令的实现见
        ``voice_loop/control.py`` 的 ``execute``（协议与命令表放一起，免得加了命令忘了登记）。
        """
        try:
            return serve_control_once(self, self.control, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 控制台坏了不能连累语音
            self.log.warning(f"处理控制台命令失败（已忽略）：{exc}")
            return 0

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
        """回答一句。走哪条路由看 ``[llm] route``：

        - ``model``（默认）：**模型先选工具**（它能看到全部工具，自己决定调哪个、
          要不要调），确定性代码只在工具里干活（算时间、写库、守卫）。
          模型没调工具、而这句话又明显要动手时，才让技能层兜底。
        - ``rules``：老顺序，先跑模式匹配，没接住才给模型。
        """
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

        if self._route_mode() == "rules":
            # -------------------------------------------------- 老顺序：技能先
            skill = self.skills.handle(user_text, dialog=list(self._dialog))
            if skill is not None and skill.action == "vision":
                return self._respond_vision(stats, skill, user_text, on_delta)
            if skill is not None:
                stats.extra["route"] = "rules→skills"
                return self._serve_skill(stats, skill, user_text, on_delta)
            stats.extra["route"] = "rules→llm"
            self._llm_turn(stats, user_text, on_delta)
            return stats

        # ---------------------------------------------- 模型先路由（默认路径）
        # 「这句话看起来要动手」时把模型的回答先攒住不念：万一它漏调工具，
        # 这一句该由确定性层来答（它才知道库里有什么）。见 skills.needs_attention。
        assert self.skills is not None
        hold = self.skills.needs_attention(user_text)
        stats.extra["route"] = "model"
        stats.extra["hold"] = hold
        deferred = self._llm_turn(stats, user_text, on_delta, hold_for_route=hold)
        if deferred is None:
            return stats

        # 模型一个工具都没调（而且没念出任何字）：技能层兜底
        skill = self.skills.handle(user_text, dialog=list(self._dialog))
        if skill is not None and skill.action == "vision":
            self.log.info(f"[路由] 模型没调工具，技能兜住看图：{user_text}")
            return self._respond_vision(stats, skill, user_text, on_delta)
        if skill is not None:
            stats.extra["route"] = "model→skills"
            self.log.info(f"[路由] 模型漏了，技能兜住：{user_text} -> {skill.action}")
            print(f"      [路由] 模型没调工具，本地技能兜住（{skill.action}）", flush=True)
            return self._serve_skill(stats, skill, user_text, on_delta)
        self.log.info(f"[路由] 模型没调工具，技能也没接住：{user_text}")
        return self._speak_deferred(stats, deferred, on_delta)

    def _route_mode(self) -> str:
        """这一轮按哪种顺序：``model`` = 模型先选工具，``rules`` = 模式匹配先。

        没工具（``router = chat`` / 技能关掉）时只能走 rules——模型手上什么都没有，
        先跑技能至少还能查日程。
        """
        if self.skills is None or self.tools is None:
            return "rules"
        if str(self.settings.llm.router or "tools").lower() != "tools":
            return "rules"
        mode = str(getattr(self.settings.llm, "route", "model") or "model").strip().lower()
        return "model" if mode in ("model", "llm", "model_first", "auto") else "rules"

    def _llm_turn(
        self, stats: TurnStats, user_text: str, on_delta=None, hold_for_route: bool = False
    ) -> DeferredAnswer | None:
        """交给模型这一轮（带工具）。返回值非空表示「攒着一句话没念，等你决定」。"""
        self._in_reply = True
        self._muted.set()
        self._mark_played()
        try:
            return self._stream_answer(
                stats, user_text, on_delta=on_delta, hold_for_route=hold_for_route
            )
        finally:
            self._in_reply = False
            self._muted.clear()

    def _serve_skill(
        self, stats: TurnStats, skill, user_text: str, on_delta=None
    ) -> TurnStats:
        """确定性技能的回答：直接念，不经过模型。"""
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

    def _speak_deferred(
        self, stats: TurnStats, deferred: "DeferredAnswer", on_delta=None
    ) -> TurnStats:
        """把攒住的模型回答补念出来（模型没调工具、技能也没兜住）。

        ★代价★：这一句没法边生成边播（文字是攒着等决定的），所以首音 = 首字。
        换来的是「模型漏了路由时不会说出错误答案」。只有看起来要动手的句子才走这里。
        """
        text = deferred.text.strip() or "……我没想好说什么。"
        stats.extra["deferred"] = True
        self._interrupt.clear()
        self._in_reply = True
        self._muted.set()
        self._mark_played()
        stats.answer = text
        stats.llm_first_token = deferred.first_token
        stats.first_audio = deferred.first_token
        self._note_spoken(text)
        self._note_dialog(stats.user_text, text)
        if on_delta is not None:
            on_delta(text)
        self.speak_text(text)
        stats.total_seconds = time.perf_counter() - deferred.t0
        stats.interrupted = self._interrupt.is_set()
        self.llm.commit(stats.user_text, text)
        self._write_session(stats)
        self._in_reply = False
        self._muted.clear()
        return stats

    def _tool_specs(self) -> list[dict] | None:
        """这一轮要不要给模型工具（[llm] router = tools / chat）。

        优先用 MCP 宿主（它可能聚合了好几个服务器）；宿主里一个工具都没有
        （比如 [mcp] enabled = false）就退回旧的进程内注册表，
        免得助手突然变成不会查日程、不会记事的。——前者是常态，后者是兼底。
        """
        if self.tools is None:
            return None
        if str(self.settings.llm.router or "tools").lower() != "tools":
            return None
        if self.mcp is not None and self.mcp.names():
            return self.mcp.specs()
        return self.tools.specs()

    def _tool_names(self) -> set[str] | None:
        """模型当前能看到的所有工具名（把「写成文字的工具调用」抢回来时要用）。"""
        if self.mcp is not None and self.mcp.names():
            return self.mcp.names()
        return self.tools.names() if self.tools else None

    def _call_tool(self, call: dict) -> tuple[bool, str]:
        """执行一次工具调用，返回 ``(ok, 要念的话)``。

        路由：MCP 宿主里有这个名字就走宿主（可能被转到另一个服务器），
        否则退回旧的进程内 ToolRegistry（兼底 / 外部配置把 MCP 关了）。
        """
        fn = call.get("function") or call
        name = str(fn.get("name") or "")
        if self.mcp is not None and self.mcp.handleable(name):
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            return self.mcp.call(name, dict(args or {}))
        assert self.tools is not None
        res = self.tools.call(call)
        return res.ok, (res.reply or res.error or "")

    def _run_tools(self, stats: TurnStats, calls: list[dict], t0: float) -> str:
        """执行模型选中的工具，返回要念的话。

        只跑一轮（不再把结果喂回去让它改写）：模型容易把「九月二十三日」改说成别的，
        而工具产出的 reply 已经是可以直接念的一句话了。

        每个调用先过一遍 :func:`repair_args`（模型改写原话时换回原话）与
        :func:`reroute_correction`（「刚记下一条 + 这一句只是时间」→ 改上一条，不新建）。
        """
        assert self.tools is not None
        out: list[tuple[bool, str]] = []
        fixed: list[dict] = []
        for call in calls:
            new_call = repair_args(call, stats.user_text)
            if new_call is not call:
                self.log.info(f"[工具] 模型改写了原话，已换回：{describe_calls([call])}")
            # 「刚记下一条 + 这句只是时间」= 在改上一条，不是新建（实测模型会选 add_*）
            routed = reroute_correction(new_call, stats.user_text, self.skills)
            if routed is not new_call:
                self.log.info(f"[工具] 这一句是在改上一条，已改走 fix_last：{stats.user_text}")
            fixed.append(routed)
        for call in fixed:
            # ★同一句话只说一遍，不管模型把同一个工具调了几次★：
            # 实测（2026-09-20）「下周三下午3点，我有社团活动，到时候记得提醒我。」
            # 模型会同时调两个新增工具、text 一模一样——合并前是两个工具（日程 + 闹钟），
            # 合并后是同一个工具调两遍，两种都靠事件层的 _find_duplicate 去重（回「已经记过了」）。
            ok, text = self._call_tool(call)
            out.append((ok, text))
        stats.extra["tool"] = describe_calls(fixed)
        stats.extra["tool_ok"] = all(ok for ok, _ in out)
        stats.extra["tool_seconds"] = round(time.perf_counter() - t0, 3)
        for ok, text in out:
            if not ok:
                self.log.warning(f"[工具] 失败：{text or '（没有说明）'}")
        reply = " ".join(text for _ok, text in out if text).strip()
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
        hold_for_route: bool = False,
    ) -> DeferredAnswer | None:
        """把 LLM 的回答边生成边播出（可带图片）。

        ``commit_text``：写进对话历史的用户话（看图时用原始那句，
        而不是塞了文件内容的那一大段 prompt）。
        ``hold_for_route``：把模型的文字先攒着不念，返回 :class:`DeferredAnswer`
        交给调用方决定（模型没调工具时，可能该由技能层来答）。
        """
        tts_cfg = self.settings.tts
        chunker = SpeechChunker(
            max_chars=int(tts_cfg.max_chunk_chars),
            first_min_chars=int(tts_cfg.first_chunk_min_chars),
            min_chunk_chars=int(tts_cfg.min_chunk_chars),
            max_hold_seconds=float(tts_cfg.max_hold_seconds),
            first_chunk_max_chars=int(getattr(tts_cfg, "first_chunk_max_chars", 0) or 0),
        )
        self._interrupt.clear()
        t0 = time.perf_counter()
        pieces: list[str] = []
        first_audio: float | None = None
        calls: list[dict] = []
        # 有些模型会把工具调用**写成一段 JSON 文字**（而不是真的调工具）。
        # 这种文字绝对不能念出来：先攒着，看清了再决定是当工具调用还是当正常回答。
        # hold_for_route 时从一开始就攒着（等路由定下来才念，见 respond 的 route=model）。
        held: list[str] = []
        holding = bool(hold_for_route)
        tool_text = False
        # 看图那一轮不给工具（它有图要描述）
        tools = self._tool_specs() if (not images and self.tools is not None) else None
        # ★TOOL_HINT 要紧贴用户那一句★（而不是放在人设前面）。
        # 实测：放在最前面时 qwen3.5:4b 会当没看见，直接凭记忆答「下周有什么安排」；
        # 挪到用户话前面（extra_messages 插在最后一条之前）就稳定调工具了。
        hint = [{"role": "system", "content": TOOL_HINT}] if tools else None
        # ★记忆摘要在 TOOL_HINT 之前★：工具提示必须紧贴用户那一句（实测过），
        # 记忆是背景信息，放前面正合适。没有记忆时这一层完全不存在。
        memory_block = self._memory_context(prompt)
        if memory_block:
            hint = [{"role": "system", "content": memory_block}] + (hint or [])

        def speak(sentence: str) -> None:
            nonlocal first_audio
            if not self.tts_enabled:
                return
            if self._speak_chunk(sentence) and first_audio is None:
                first_audio = time.perf_counter() - t0

        try:
            # ★从回答一开始就告诉字幕「这一轮要发声了」★：这样字幕不会先把
            # 全文（折叠后的末尾）闪一下，再跳回正在念的那句
            self._speech_begin()
            with self._speak_lock:
                for ev in self.llm.chat_events(
                    prompt,
                    images=images,
                    model=model,
                    num_ctx=num_ctx,
                    tools=tools,
                    extra_messages=hint,
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
                        if (
                            not hold_for_route
                            and not tool_text
                            and len(joined) > 8
                            and not SUSPICIOUS_START.match(joined)
                        ):
                            # 看清了：只是一段普通 JSON（例如用户要的示例）→ 放行
                            holding = False
                        if holding:
                            # 攒着不念，但回车打断还是要能生效（否则这一段话就只能干等）
                            if self._interrupt.is_set():
                                break
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
                    self._speech_end()

            if not calls and hold_for_route and not self._interrupt.is_set():
                # 模型一个字都没念、也没调工具：交给 respond() 问过技能层再决定
                return DeferredAnswer(
                    text="".join(held), t0=t0, first_token=stats.llm_first_token
                )

            if not calls and held:
                raw = "".join(held)
                if tool_text:
                    # 把「写成文字的 JSON」抢回来当工具调用（小模型常见毛病）
                    recovered = parse_tool_call_text(raw, self._tool_names())
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
                # 按键打断 / 语音打断都要把剩下没放完的清掉
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
    def _start_interrupt_watcher(self, raw_key: bool = True) -> None:
        """播放过程中按 Esc 即可打断。后台运行时没有终端，自动跳过。

        ``raw_key=False`` 时退回「按回车」的老办法（ptt 模式下回车另有用途，
        读裸按键会和录音抢同一个输入，所以那里不用它）。
        """
        active = lambda: bool(  # noqa: E731
            self._in_reply or self.speaker.speaking or self.speaker.pending
        )

        def interrupt_now(label: str) -> None:
            self._interrupt.set()
            self.speaker.interrupt()
            print(f"  [打断] 已打断{label}", flush=True)

        if raw_key:
            try:
                from .hotkey import KeyWatcher, keys_from_spec

                keys = keys_from_spec(str(getattr(self.settings.bargein, "key", "esc+enter")))
                self._hotkey = KeyWatcher(
                    lambda: interrupt_now("（Esc）"), active, keys=keys, logger=self.log
                )
                self._hotkey.start()
                return
            except Exception as exc:  # noqa: BLE001 - 读不到裸按键就退回回车方案
                self.log.debug(f"裸按键监听装不上（{exc}），退回「按回车打断」")

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
                if active():
                    interrupt_now("")

        self._watcher = threading.Thread(target=watch, daemon=True, name="interrupt")
        self._watcher.start()

    def use_wake_file(self, path: str | Path) -> None:
        """切换唤醒词文件（供 listen --wake-file 使用）。

        这是「用这个文件里的唤醒词」的意思，所以**故意不再叠加角色文件的唤醒词**：
        测试和调唤醒词时要的是这个文件说了算。
        """
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

    def _is_standby(self, text: str) -> bool:
        """「没事了」这类收回唤醒的话（见 config.toml 的 [wake] standby_phrases）。"""
        return is_standby(text, self.settings.wake.standby_phrases)

    def _speak_brief(self, text: str) -> None:
        """待唤醒状态下的一句话：说完就把 TTS 收回（别为了一句话常驻几百 MB）。"""
        if not text:
            return
        self.speak_text(text, fresh=True)
        if self.lazy and not self._active:
            unload = getattr(self.tts, "unload", None)
            if callable(unload):
                unload()

    def _standby_now(self, text: str) -> None:
        """收回这次唤醒：关会话 + 说一句话；重型模型由主循环下一轮释放。"""
        self.session.close()
        reply = (self.settings.wake.standby_reply or "").strip()
        print(f"\n你说：{text}\n助手：{reply or '（回待唤醒）'}\n", flush=True)
        self._speak_brief(reply)

    def _report_wake_miss(self, text: str) -> None:
        """未唤醒时给一点有用的提示：听到什么 + 离唤醒词有多近。

        这是调唤醒词最直接的依据：相似度高说明只差一点，加进 aliases 或者把
        fuzzy_ratio 降一点就行；相似度很低（像「胎儿戏」那样）降阈值没用，
        只能把那句话填进 aliases。环境里有别人说话时，这些行也是判断依据。
        """
        plain = (text or "").strip()
        key = _NOISE_STRIP.sub("", plain)
        # 「没事了」这种是收回唤醒，不是喊错唤醒词，别刷一行提示
        if self._is_standby(plain):
            self.log.debug(f"[待命] 已忽略：{plain}")
            return
        # 单个字的「嗯/哎/啊」以及「好的/谢谢」这类是环境杂音，写进日志只会淹没有用的行
        if not plain or not is_meaningful(text) or key in _FILLER_WORDS:
            self.log.debug(f"[未唤醒] {text}")
            return

        ratio, word, char_id = self.wake.best_target(plain)
        target = word or "唤醒词"
        hint_file = self._wake_path.name          # 没有角色时就是全局那本
        if char_id and self.persona is not None:
            path = (self.persona.files or {}).get(char_id)
            if path is not None:
                hint_file = f"{Path(path).name}（角色「{target}」的别名住这里）"
        close = ratio >= 0.6
        # 短句最可能是喊唤醒词喊错了；长句只有「很像」时才值得刷屏
        if not (2 <= len(key) <= 8 or close):
            self.log.debug(f"[未唤醒] {text}")
            return
        if plain == self._last_miss:
            self.log.debug(f"[未唤醒] {plain}")
            return
        self._last_miss = plain

        if close:
            hint = (
                f"和「{target}」相似度 {ratio:.2f}，就差一点：把它加进 "
                f"{hint_file} 的 aliases，或者把 fuzzy_ratio 降到 "
                f"{max(0.5, round(ratio - 0.05, 2))}"
            )
        else:
            hint = (
                f"和「{target}」相似度 {ratio:.2f}，降阈值没用，"
                f"只能把它加进 {hint_file} 的 aliases"
            )
        print(f"[未唤醒] 听到：{plain}\n          {hint}", flush=True)

    def _transcribe(self, audio: np.ndarray) -> tuple[str, AsrResult, float]:
        rate = int(self.settings.audio.sample_rate)
        t0 = time.perf_counter()
        result = self.asr.transcribe(audio, rate)
        return self._fix_names(result.text.strip()), result, time.perf_counter() - t0

    def _fix_names(self, text: str) -> str:
        """把句首听错的人名改回规范名（只认不准的词，拿不准就不改）。"""
        if not text or self.names is None or not self.names.enabled:
            return text
        fixed, hits = self.names.correct(text)
        for hit in hits:
            print(
                f"[人名] 听到「{hit.spoken}」→ 认成「{hit.name}」"
                f"（相似度 {hit.score:.2f}）",
                flush=True,
            )
            self.log.info(f"人名校正：{hit.spoken!r} → {hit.name!r}")
        return fixed

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

        # 「没事了」：收回这次唤醒，回待唤醒（不是退出服务，也不送 LLM）。
        # 只在唤醒会话里算——普通对话（chat / 文本模式）没开过会话，那它就是普通一句话。
        if self.session.active and self._is_standby(text):
            self._standby_now(text)
            return False

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
        route = str(stats.extra.get("route") or "")
        if stats.extra.get("skill"):
            print(
                f"\n      [本地技能 {stats.extra['skill']} / 路由 {route or '—'}"
                f" / 播报 {stats.total_seconds:.2f}s]"
            )
        else:
            print(
                f"\n      [路由 {route or '—'}"
                f"{' 工具=' + str(stats.extra.get('tool')) if stats.extra.get('tool') else ''}"
                f" / 首字 {stats.llm_first_token:.2f}s / 首音 {stats.first_audio:.2f}s"
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
            f"  TTS : {self._tts_label()}\n"
            f"  模式: {'自动断句（直接说话，停顿即发送）' if mode == 'vad' else '回车录制'}"
            + (f"\n  技能: {self.skills.stats()}" if self.skills else "")
            + f"\n  提示: 说「{self.settings.chat.exit_phrases[0]}」退出"
            + ("；回答过程中按 Esc 可打断" if mode == "vad" else "")
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
        # ★唤醒词住在哪要说清★：装了角色就是各角色的人格文件，全局那本只是兜底；
        # 打印错文件会让人改半天没反应。
        if self.wake.characters_loaded() and self.persona is not None:
            wake_where = f"各角色的 data/personas/<id>.json（{self.persona.path.name} 里指到的人格文件）"
        else:
            wake_where = str(self._wake_path)
        asr_desc = f"{self.settings.asr.strategy} / SenseVoice" + (
            " 常驻 + Whisper 按需" if self.lazy else " + Whisper"
        )
        print(
            f"\n=== 本地语音助手 · 常驻服务 ===\n"
            f"  ASR : {asr_desc}\n"
            f"  LLM : {self.settings.llm.model} @ {self.settings.llm.host}\n"
            f"  TTS : {self._tts_label()}\n"
            f"  唤醒词: {words}    (改 {wake_where}，保存即生效)\n"
            + (
                f"  角色  : {self.persona.stats()}\n" if self.persona is not None else ""
            )
            + f"  空闲回收: {self._idle_desc()}"
            + ("（则释放模型回到待唤醒）" if self.lazy else "（则结束服务）")
            + "\n"
            + (
                f"  收回  : 说「{self.settings.wake.standby_phrases[0]}」立刻回待唤醒，不用等超时\n"
                if self.settings.wake.standby_phrases
                else ""
            )
            + (
                "  打断: 你直接开口、或按 Esc 都行"
                if self.bargein is not None
                else "  打断: 按 Esc（[bargein] enabled=false，语音自动打断关着）"
            )
            + "\n"
            + (f"  技能: {self.skills.stats()}\n" if self.skills else "")
            + "  提示: Ctrl+C 退出"
            + ("；字幕/提醒会显示在屏幕上\n" if self.subtitle is not None else "\n")
        )

        if not self.wake.enabled:
            print("[警告] 唤醒词未启用或列表为空，将退化为普通对话模式。\n")

        # 在日志里也记一份 PID，pid 文件被意外删掉时 stop 能靠它恢复
        self.log.info(f"服务已启动 PID={os.getpid()}")

        self.mic.open()
        # 常驻服务没有「按回车录音」这回事，所以直接用裸按键（Esc）
        self._start_interrupt_watcher()
        # 上一次服务如果是崩在半路的，信箱里会留着「已认领没回执」的请求：
        # 先给它们判失败，不然控制台会一直等下去（见 control.reap）。
        try:
            stale = reap_control(self.control)
            if stale:
                self.log.info(f"控制台信箱：{stale} 条上次没做完的请求已判为失败")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"清理控制台信箱失败（忽略）：{exc}")
        if self.scheduler:
            self.scheduler.start()
        if not self.lazy:
            # 不分级时模型已经加载好了，直接算活跃
            self._active = True
        print(f"\n[待唤醒] 请说「{self._wake_hint()}」…\n")

        try:
            while not self._stop.is_set():
                if self._check_stop_file():
                    break
                # 控制台（网页 UI）的命令：最多 5 秒内被响应（本循环每轮也会被 listen_once 上限带走）
                self._serve_console()
                try:
                    if self.wake.maybe_reload():
                        self._idle_timeout = float(
                            self.wake.settings.idle_timeout or self.settings.wake.idle_timeout
                        )
                        self.session.timeout = self._idle_timeout
                        print(f"[唤醒词已更新] {'、'.join(self.wake.settings.words)}\n")
                    # 角色文件改了（改了人设/加了角色/补了别名）→ 重新装上
                    if self.persona is not None and self.persona.maybe_reload():
                        self.wake.set_characters(self.persona.all(only_enabled=True))
                        self.names = NameCorrector.from_characters(
                            self.persona.all(only_enabled=True)
                        )
                        keep = self.persona.get(self.character.id) if self.character else None
                        self._apply_character(keep or self.persona.default(), reason="热重载")
                        print(f"[角色设定已更新] {self.persona.stats()}\n")
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
                        # ★对话中也要能认名字★（含听错的）：喊别人就换人，喊自己就摘掉名字，
                        # 整句只有名字就当「叫一声」——别把名字本身送给大模型。
                        spoken, request = text, None
                        request, address_note = self._resolve_address(text)
                        if request is None:      # 整句只是个名字 → 回一句应答语
                            ack = (
                                (self.character.ack if self.character else "")
                                or self.wake.settings.ack
                                or ""
                            ).strip()
                            print(f"\n你说：{spoken}（只是叫一声）\n助手：{ack}\n", flush=True)
                            if ack:
                                self.speak_text(ack, fresh=True)
                            continue
                        if address_note:
                            print(f"\n{address_note}", flush=True)
                        if self._process(request, result, asr_seconds):
                            break
                        continue

                    hit = self.wake.match(text)
                    if hit is None:
                        self._report_wake_miss(text)
                        continue

                    mark = "（模糊匹配）" if hit.fuzzy else ""
                    # ★多角色★：谁的名字被喊了就用谁的人设（必要时连声线一起换）
                    self._switch_character(hit.character)
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

                    # 「凯尔希，没事了」：只是想收回这次唤醒，
                    # 别为了它把 Whisper / TTS / Ollama 全加载一遍再卸掉
                    first = (
                        follow_result[0]
                        if follow_result is not None
                        else (request if len(request) >= 2 else "")
                    )
                    if first and self._is_standby(first):
                        self._standby_now(first)
                        continue

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
                            ("事件", self.skills.store.path),
                            ("备忘", self.skills.memos.path),
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
    def _memory_context(self, text: str) -> str:
        """这一轮要带上的记忆摘要（塞进提示词）。★不加载模型、失败也不影响回答★。"""
        hub = getattr(self, "memory_hub", None)
        cfg = getattr(self.settings, "memory", None)
        if hub is None or not getattr(cfg, "inject", True):
            return ""
        try:
            mem = hub.for_character(self.character.id if self.character else "default")
            return mem.prompt_block(text, max_chars=int(getattr(cfg, "inject_chars", 700) or 700))
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[记忆] 摘要生成失败（这一轮不带记忆）：{exc}")
            return ""

    def _memory_llm(self, prompt: str) -> str:
        """给记忆层用的「一句问、一句答」口子（归档时总结用）。

        ★两个必须处理的地方★：
        1. `llm.chat()` 会把「问 + 答」写进对话历史，而这段提示词跟用户的对话毫无关系
           （真混进去，角色下轮会突然开始念记忆条目）→ 用完 `reset()` 清掉。
           归档发生在退出路径上，历史本来就要丢，所以这里清掉是安全的。
        2. ★限次★：一次归档最多总结几条（`[memory] llm_summary_max`），
           否则「退出」会卡在几十次模型调用上。用完就报错 → 引用方退回规则摘要。
        """
        limit = int(getattr(self.settings.memory, "llm_summary_max", 8) or 8)
        if self._memory_summaries >= limit:
            raise RuntimeError("本次归档的总结次数已用完")
        self._memory_summaries += 1
        try:
            return self.llm.chat(prompt)
        finally:
            self.llm.reset()

    def _close_memory(self) -> None:
        """收尾：归档原始对话（L1→L2/L3）→ 巩固（自清洁）→ 滑窗清原始对话。

        ★像睡觉那样整理一次★。任何一步失败都不能影响「退出」本身，
        所以整段包在 try 里：最坏只是这次没归档（原始文件还在，下次还会扫到）。
        """
        hub = getattr(self, "memory_hub", None)
        if hub is None:
            return
        cfg = getattr(self.settings, "memory", None)
        try:
            mem = hub.for_character(self.character.id if self.character else "default")
            self._memory_summaries = 0
            if getattr(cfg, "archive_on_close", True) and self._session_file.exists():
                use_llm = getattr(cfg, "llm_summary", True)
                got = mem.ingest_session(self._session_file,
                                         llm_call=self._memory_llm if use_llm else None)
                self.log.info(f"[记忆] 归档：{got}")
            if getattr(cfg, "consolidate_on_close", True):
                self.log.info(f"[记忆] 巩固：{mem.consolidate()}")
            if getattr(cfg, "prune_sessions", True):
                gone = mem.prune_raw_sessions(self.settings.sessions_dir, dry_run=False)
                if gone:
                    self.log.info(f"[记忆] 滑动窗口清掉 {len(gone)} 个原始对话")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[记忆] 收尾失败（不影响退出）：{exc}")

    def _write_session(self, stats: TurnStats) -> None:
        try:
            with open(self._session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(stats), ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._stop.set()
        # ★先归档记忆再拆别的东西★：记忆收尾只需要原始对话文件与配置，
        # 而 MCP/hotkey 的清理有可能报错（报错就不该把归档吞掉）。
        self._close_memory()
        if self._hotkey is not None:
            # 按键监听要收尾：POSIX 下它把终端设成了 cbreak，得还原回去
            try:
                self._hotkey.stop()
            except Exception:  # noqa: BLE001
                pass
            self._hotkey = None
        if self.scheduler:
            self.scheduler.stop()
        if self.mcp is not None:
            # 关掉 MCP 服务器（stdio 的那些要收子进程，不然会留下孤儿）
            try:
                self.mcp.close()
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"关闭 MCP 出错（已忽略）：{exc}")
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
