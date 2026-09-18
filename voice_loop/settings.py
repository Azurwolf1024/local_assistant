"""配置加载：把 config.toml 映射为类型安全的 dataclass。"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class AppConfig:
    project_root: str = "."
    sessions_dir: str = "sessions"
    log_level: str = "info"
    save_audio: bool = False


@dataclass
class AudioConfig:
    input_device: str = ""
    output_device: str = ""
    sample_rate: int = 16000
    frame_size: int = 512
    mic_gain: float = 1.0
    max_record_seconds: float = 30.0
    listen_timeout: float = 30.0
    playback_volume: float = 1.0


@dataclass
class VadConfig:
    backend: str = "auto"
    model: str = "models/vad/silero_vad.onnx"
    threshold: float = 0.5
    min_speech_duration: float = 0.25
    min_silence_duration: float = 0.55
    max_speech_duration: float = 15.0
    energy_threshold: float = 0.012


@dataclass
class AsrConfig:
    strategy: str = "hybrid"
    language: str = "zh"
    num_threads: int = 6
    sensevoice_model: str = "models/asr/sensevoice-small/model.int8.onnx"
    sensevoice_tokens: str = "models/asr/sensevoice-small/tokens.txt"
    sensevoice_use_itn: bool = True
    whisper_model: str = "models/asr/whisper-large-v3-turbo-int8-ov"
    whisper_device: str = "CPU"
    whisper_min_duration: float = 6.0
    whisper_word_timestamps: bool = False


@dataclass
class LlmConfig:
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b"
    temperature: float = 0.7
    top_p: float = 0.9
    num_ctx: int = 4096
    num_predict: int = 512
    keep_alive: str = "30m"
    history_turns: int = 6
    system_prompt: str = ""


@dataclass
class TtsConfig:
    backend: str = "piper"
    voice: str = "zh_CN-huayan-medium"
    model: str = "models/tts/piper/zh_CN-huayan-medium.onnx"
    config: str = "models/tts/piper/zh_CN-huayan-medium.onnx.json"
    use_cuda: bool = False
    length_scale: float = 1.0        # 语速，1.0 最自然
    noise_scale: float = 0.667       # 音高变化幅度
    noise_w_scale: float = 0.85      # 音长变化幅度（略大更有感情）
    volume: float = 1.0
    sentence_silence: float = 0.08   # 块之间的补白，仅用于避免拼接感
    inject_pauses: bool = False      # 用 [[,]] 强化标点停顿（停顿更清晰但语气偏平）
    first_chunk_min_chars: int = 8   # 首块最少字数（越小出声越快）
    min_chunk_chars: int = 14        # 短于此长度的句子会与下一句合并
    max_chunk_chars: int = 60        # 整句超过此长度才从句标点处切
    max_hold_seconds: float = 1.2    # 攒句最长等待时间
    speak_streaming: bool = True     # 边生成边朗读（关掉则整体生成完再播）


@dataclass
class ChatConfig:
    mode: str = "vad"
    wake_word: str = ""              # 已废弃，保留兼容；请改用 data/wakewords.json
    exit_phrases: list[str] = field(default_factory=lambda: ["退出", "再见"])


@dataclass
class WakeConfig:
    enabled: bool = True
    file: str = "data/wakewords.json"
    ack: str = "在的"                # 被唤醒时的应答语，留空则不播报
    idle_timeout: float = 180.0      # 唤醒后多久没有指令就回到待唤醒（秒）
    idle_action: str = "standby"     # standby=卸载重型模型回待唤醒 | exit=整个服务退出
    lazy_load: bool = True           # true=唤醒后才加载 Whisper/LLM，待唤醒只跑 SenseVoice
    unload_llm: bool = True          # 回待唤醒时让 Ollama 释放模型（能腾出 4~5 GB）
    min_silence_wake: float = 0.30   # 待唤醒状态下的静音判定
    fuzzy_ratio: float = 0.75        # 模糊匹配阈值
    followup_window: float = 2.0     # 只说唤醒词后，等下半句的时长（秒）；0=不等直接应答
    # 后台运行相关
    pid_file: str = "sessions/listen.pid"
    stop_file: str = "sessions/listen.stop"
    log_file: str = "sessions/listen.log"


@dataclass
class SkillsConfig:
    enabled: bool = True
    data_dir: str = "data"
    alarm_file: str = "data/alarms.json"
    memo_file: str = "data/memos.json"
    schedule_file: str = "data/schedule.json"
    check_interval: float = 5.0      # 后台计时器轮询间隔（秒）
    default_remind_before: int = 10  # 日程默认提前多少分钟提醒
    speak_duplicate: bool = False    # 提醒重复播报
    visual_alert: bool = True        # 提醒时同时弹出右下角可视窗（怕没开扬声器）
    visual_timeout: float = 25.0     # 可视窗自动关闭秒数
    allow_system_commands: bool = True  # 允许「关屏幕」这类系统级操作


@dataclass
class SubtitleConfig:
    """屏幕底部居中的半透明字幕（关掉声音时靠它沟通）。"""

    enabled: bool = True
    width: int = 920            # 字幕条最大宽度（像素），屏幕太窄会自动缩
    alpha: float = 0.86         # 不透明度，越小声越透
    hold_seconds: float = 6.0   # 说完后多久自动隐藏
    font_size: int = 20         # 正文字号
    max_lines: int = 4          # 最多显示几行，超出只显示末尾（前面加「…」）
    show_user_text: bool = True  # 要不要连「你说：…」一起显示（能看出有没有听错）
    margin: int = 8             # 离任务栏上方多少像素


@dataclass
class Settings:
    app: AppConfig = field(default_factory=AppConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    path: Path = PROJECT_ROOT / "config.toml"

    # ---------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        base = Path(self.app.project_root)
        return base if base.is_absolute() else (PROJECT_ROOT / base).resolve()

    def resolve(self, value: str | Path) -> Path:
        """把配置里的相对路径解析为绝对路径。"""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def sessions_dir(self) -> Path:
        p = self.resolve(self.app.sessions_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    """按 dataclass 字段类型填充，忽略 TOML 中的未知键。"""
    valid = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - valid
    if unknown:
        print(f"[config] 警告：[{section}] 中存在未知配置项 {sorted(unknown)}，已忽略")
    kwargs = {k: v for k, v in data.items() if k in valid}
    return cls(**kwargs)


def load_settings(path: str | Path | None = None) -> Settings:
    cfg_path = Path(path) if path else (PROJECT_ROOT / "config.toml")
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{cfg_path}")
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    return Settings(
        app=_build(AppConfig, raw.get("app", {}), "app"),
        audio=_build(AudioConfig, raw.get("audio", {}), "audio"),
        vad=_build(VadConfig, raw.get("vad", {}), "vad"),
        asr=_build(AsrConfig, raw.get("asr", {}), "asr"),
        llm=_build(LlmConfig, raw.get("llm", {}), "llm"),
        wake=_build(WakeConfig, raw.get("wake", {}), "wake"),
        skills=_build(SkillsConfig, raw.get("skills", {}), "skills"),
        subtitle=_build(SubtitleConfig, raw.get("subtitle", {}), "subtitle"),
        tts=_build(TtsConfig, raw.get("tts", {}), "tts"),
        chat=_build(ChatConfig, raw.get("chat", {}), "chat"),
        path=cfg_path,
    )
