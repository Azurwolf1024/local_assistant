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
    # qwen3.5 是多模态（自带视觉 + tools），一个模型同时干文本和看图：
    # 比 qwen2.5:7b + qwen2.5vl:3b 少 4.5 GB，生成还快 40%（见 README 第 7 节）
    model: str = "qwen3.5:4b"
    temperature: float = 0.7
    top_p: float = 0.9
    num_ctx: int = 4096
    num_predict: int = 512
    keep_alive: str = "30m"
    history_turns: int = 6
    system_prompt: str = ""
    # 加速相关（装了支持的运行时才有用，没装只是忽略）：
    #   num_gpu    = -1 不传（默认）；>0 交给 Ollama 决定层数；99 = 尽量全放显存
    #   num_thread = 0 不传；纯 CPU 时想指定线程数就填（比如 8）
    #   num_batch  = 0 不传；预填慢的话可以调到 512
    num_gpu: int = -1
    num_thread: int = 0
    num_batch: int = 0
    # 技能没接住的话交给模型时，要不要给它工具（查日程/记备忘/排日程）。
    # tools = 给（默认，实测闲聊不会多绕一圈，只有真调工具才多一轮）
    # chat  = 不给，保持老的纯聊天行为
    router: str = "tools"
    # ★路由顺序★：谁先决定「这件事该不该动手」
    #   model = 模型先选工具（默认）：模型看到全部工具自己决定调哪个；
    #           它没调工具、而这句话又明显要动手时，才让确定性技能层兵底。
    #           —— 想让模型能力不足时少吃亏，就靠这个兵底：
    #              实测模型是先选工具后，什么说法都能进，而守卫/算时间仍在代码里。
    #   rules = 老行为：先跑模式匹配（一堆正则），没接住才给模型。
    #           好处是查日程/报时这种零延迟，坏处是正则没覆盖的说法根本轮不到模型。
    route: str = "model"
    # 思考模式（新一代模型带 thinking）：off / on / auto
    #   off  = 不发 think 字段（语音对话必须关：开着会先默默想一两千字，
    #          实测同一次回答总耗时 16.8s → 7.2s，而这十几秒用户只能干等）
    #   on   = 显式打开（已确认模型支持时）
    #   auto = 完全不发这个字段，交给 Ollama / 模型自己的默认
    think: str = "off"


@dataclass
class VisionConfig:
    """看图：摄像头 / 屏幕截图 / 剪贴板 / 指定文件。"""

    enabled: bool = True
    model: str = "qwen3.5:4b"       # 看图用的模型；qwen3.5 自带视觉，所以跟 [llm] 同一个
    default_source: str = "camera"   # 只说「这是什么」没提来源时看哪里：camera / screen
    camera_index: int = 0
    warmup_frames: int = 4           # 丢掉前几帧（自动曝光还没稳，画面偏黑/偏黄）
    max_side: int = 1024             # 摄像头图片最长边（越小上传越快）
    screen_max_side: int = 1024      # 截图最长边（实测本机 1568 要 61 秒，1024 只要 33 秒）
    jpeg_quality: int = 82
    save_dir: str = "data/vision"    # 拍下来的图 / 截图存这里，方便回头核对模型看了什么
    keep_images: int = 40            # 只保留最近这么多张，超了自动删最旧的
    file_roots: list[str] = field(default_factory=lambda: ["桌面", "下载", "文档"])
    file_max_depth: int = 4          # 在根目录里往下找几层
    file_max_scan: int = 20000       # 最多扫多少个文件（防止在巨型目录里卡住）
    file_max_chars: int = 1800       # 读文本文件时最多塞多少字（中文约 1 字 1 token）
    file_num_ctx: int = 8192         # 读文件那一轮单独放大上下文
    confirm_expire: float = 120.0    # 「是这个文件吗？」多久没回答就作废（秒）
    say_first: str = "我看一眼。"     # 看图前先说的那句话（留空则不说）


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
    first_chunk_max_chars: int = 0   # 首块最多字数（0 = 不限）；克隆音色下建议 12~20：
                                     #   ZipVoice 整块生成完才回音频，首块 60 字 = 开口前等 8 秒
    min_chunk_chars: int = 14        # 短于此长度的句子会与下一句合并
    max_chunk_chars: int = 60        # 整句超过此长度才从句标点处切
    max_hold_seconds: float = 1.2    # 攒句最长等待时间
    speak_streaming: bool = True     # 边生成边朗读（关掉则整体生成完再播）

    # ---- 零样本音色克隆后端（backend = "zipvoice"，见 voice_loop/tts/zipvoice_tts.py）----
    # 用「一小段参考音频 + 这段音频的逐字文本」把音色搬到中文输出上。
    # 参考音频要求：单人、无背景音乐、5~15 秒；参考文本必须跟音频逐字一致。
    clone_dir: str = "models/tts/zipvoice/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
    clone_vocoder: str = "models/tts/zipvoice/vocos_24khz.onnx"
    clone_audio: str = ""            # 参考音频路径（wav）
    clone_text: str = ""             # 参考音频的逐字文本；留空则用同名 .txt 或本地 ASR 转写
    clone_autotext: bool = True      # 参考文本留空时自动转写，并把结果缓存成同名 .txt
    clone_romanize: bool = True      # 参考文本是日语时自动转罗马字（ZipVoice 只认中英文）
    clone_max_seconds: float = 15.0  # 参考音频截取上限；★只有文本靠自动转写时才截★，
                                     # 文本从文件/清单来的时候不截（截了文本就对不上，会乱说）
    clone_steps: int = 4             # 流匹配步数：4 最快，8 慢一倍（输出时长完全一样，实测）
    clone_precision: str = "int8"     # 用哪一份 onnx：int8（125 MB，快）/ fp32（600 MB，量化损失小）
                                     #   ★两份可以共存★（装的时候用 --precision both），
                                     #   目录里没这一份就自动退回 int8；哪个更好听要自己 AB
                                     #   （sessions/ab_fp32/ 里已经生成好了，直接对比听）
    clone_min_chars: int = 16        # 段内最短切句字数（越小出声越快）
    clone_threads: int = 2           # onnxruntime 线程数
    clone_speed: float = 1.0         # 语速：1.0=模型自己的节奏；<1 更慢；★>1 会弄坏（已夹到 ≤1.0）★
    clone_guidance: float = 0.0      # 0 = 用库默认，>0 才覆盖
    clone_t_shift: float = 0.0       # 0 = 用库默认
    clone_target_rms: float = 0.0    # 0 = 用库默认
    clone_feat_scale: float = 0.0    # 0 = 用库默认
    # ---- 输出静音裁剪（见 voice_loop/tts/pacing.py）----
    # ★实测：模型会在每段音频开头塞 0.58~1.48 秒纯数字静音（参考音频只有 0.04s），
    #   这才是「语速忽快忽慢」的真凶（发音速率本身很稳，5.88~7.48 字/有声秒）。★
    trim_output_silence: bool = True  # 关掉就完全用模型原始输出
    trim_lead_ms: int = 40            # 开头保留的静音（别设 0，免得第一个字被啃）
    trim_tail_ms: int = 80            # 结尾保留的静音
    trim_max_pause_ms: int = 0        # 0 = 不压缩句内停顿；>0 = 把超过此值的停顿压到 trim_min_pause_ms
                                      #   实测单次停顿都 <0.6s，阈值 600~1200 无效；要压得用 450→260
    trim_min_pause_ms: int = 260      # 压停顿时的下限
    trim_min_gap_ms: int = 0          # 0 = 不拉长过短的停顿；>0 = 句内停顿短于它就拉到它
                                      #   ★实测：「我在，博士。」的逗号只停 60ms（人类 200~400ms），
                                      #   拉到 220~260 就像人话了；不改音高，比变速安全★
    trim_min_gap_floor_ms: int = 60   # ★只拉长「本来就 ≥ 这个长度」的空隙★
                                      #   字与字之间有 10~40ms 的自然音渡，无差别撑成 240ms 会让
                                      #   每个字都垫一段等长静音 → 整句「一顿一顿」（实测 11 个空隙
                                      #   里 8 个是这种，白加 1.84 秒死气）。别设为 0。


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
    # 「收回唤醒」：说了这些就立刻回待唤醒，不用干等 idle_timeout
    standby_phrases: list[str] = field(
        default_factory=lambda: [
            "没事了", "没事", "没事儿了", "没什么事了", "没啥事了",
            "没别的事了", "没别的事情了", "没别的了", "没其他事了", "没其它事了",
            "就这些", "就这样", "先这样", "先这样吧", "退下", "退下吧", "你可以休息了",
        ]
    )
    standby_reply: str = "好，随时叫我。"   # 收回时的应答，留空则只打印不播报
    # 后台运行相关
    pid_file: str = "sessions/listen.pid"
    stop_file: str = "sessions/listen.stop"
    log_file: str = "sessions/listen.log"


@dataclass
class McpServerConfig:
    """一个 MCP 服务器（一个能力域）。"""

    name: str = ""
    enabled: bool = True
    transport: str = "inproc"        # inproc = 同进程（自家服务器，快）| stdio = 起子进程
    module: str = ""                 # inproc：模块路径，里面要有 build_server()
    command: list[str] = field(default_factory=list)   # stdio：启动命令
    cwd: str = ""                    # stdio：工作目录
    env: dict[str, str] = field(default_factory=dict)  # stdio：额外的环境变量
    tools: list[str] = field(default_factory=list)     # ★白名单★，空 = 全部（不推荐）
    namespace: bool = True           # 工具名要不要加 mcp__<服务器>__ 前缀
    timeout: float = 15.0            # 单次调用超时（秒）


@dataclass
class McpConfig:
    """自己的 MCP 架构（见 voice_loop/mcp/）。"""

    enabled: bool = True
    servers: list[McpServerConfig] = field(
        default_factory=lambda: [
            # 助手的核心能力：日程/备忘/提醒。
            # ★namespace=False★：工具名保持 list_schedule / add_memo 原样，
            # 模型已经认得这 8 个名字（实测 qwen3.5:4b 在这 8 个上 8/8）。
            McpServerConfig(
                name="skills",
                transport="inproc",
                module="voice_loop.mcp.servers.skills",
                namespace=False,
            )
        ]
    )


@dataclass
class PersonaConfig:
    """角色设定（名字/背景/称呼/示例台词…）。见 voice_loop/persona.py 第 14 节。"""

    enabled: bool = True
    file: str = "data/characters.json"      # 角色文件（可热加载）
    default: str = ""                        # 没指定角色时用谁（id 或名字）；空 = 用文件里 default=true 的
    extra_prompt: str = ""                   # 追加到任何角色后面的附加要求


@dataclass
class SkillsConfig:
    enabled: bool = True
    data_dir: str = "data"
    # ★统一事件表★：闹钟、日程、事件链现在是**同一种东西**（见 voice_loop/events.py）。
    # alarm_file / schedule_file 只剩「迁移数据源」这个用途，运行期不再读它们。
    event_file: str = "data/events.json"
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
class BargeInConfig:
    """打断。

    ★默认走「按 Esc」。★``enabled`` 只管**语音自动打断**（开口就停）：
    它在真实环境里很难调准，最常见的失败是把自己外放的声音当成你在插话，
    于是自我打断、套娃——实测踩过，所以默认关掉。想折腾再把它打开。
    不管这个开关怎么设，``Esc`` 永远能用。
    """

    enabled: bool = False        # 语音自动打断（实验性，实测容易自我打断）
    key: str = "esc+enter"       # 按键打断用哪些键（esc / enter / ctrl-c，可用 + 连接）
    min_seconds: float = 0.25    # 连续说多久才算插话（太小了容易被噪音打断）
    min_rms: float = 0.015       # 判定下限（幅度），压住底噪
    margin: float = 1.6          # 比「麦克风里听到的回声」响几倍才算插话（调大更稳、调小更灵敏）
    window_seconds: float = 0.30  # 麦克风能量滑窗
    seed_seconds: float = 1.0     # 开头用多久校准回声基准（这段时间里不判断）
    base_seconds: float = 6.0     # 回声基准的跟踪窗长度
    base_percentile: float = 90.0  # 基准取低于门槛那些帧的哪个分位（要压过回声本身的高分位）
    base_track: float = 0.3       # 基准每次跟随的比例（越小越稳）
    keep_seconds: float = 1.0     # 打断前保留多久的音频（免得丢掉开口的第一个字）
    collect_seconds: float = 6.0  # 打断后最多再收多久这句话


@dataclass
class SubtitleConfig:
    """屏幕底部居中的半透明字幕（关掉声音时靠它沟通）。"""

    enabled: bool = True
    width: int = 920            # 字幕条最大宽度（像素），屏幕太窄会自动缩
    alpha: float = 0.86         # 不透明度，越小声越透
    hold_seconds: float = 6.0   # ★说完之后★再停留几秒才隐藏（说话期间不会隐藏）
    sync_speech: bool = True    # 字幕只显示「已经念到的地方」，折叠落在念过的部分上
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
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    bargein: BargeInConfig = field(default_factory=BargeInConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
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


def _build_mcp(data: dict[str, Any]) -> McpConfig:
    """[mcp] 段要单独填：里面是「表数组」（[[mcp.servers]]），_build 处理不了。"""
    servers = [
        _build(McpServerConfig, raw, "mcp.servers")
        for raw in (data.get("servers") or [])
        if isinstance(raw, dict)
    ]
    cfg = _build(McpConfig, {k: v for k, v in data.items() if k != "servers"}, "mcp")
    cfg.servers = servers
    return cfg


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
        bargein=_build(BargeInConfig, raw.get("bargein", {}), "bargein"),
        vision=_build(VisionConfig, raw.get("vision", {}), "vision"),
        tts=_build(TtsConfig, raw.get("tts", {}), "tts"),
        chat=_build(ChatConfig, raw.get("chat", {}), "chat"),
        persona=_build(PersonaConfig, raw.get("persona", {}), "persona"),
        mcp=_build_mcp(raw.get("mcp", {})),
        path=cfg_path,
    )
