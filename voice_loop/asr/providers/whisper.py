"""Whisper large-v3-turbo（OpenVINO int8）—— 高精度路径，★不常驻★。

它 1 GB 上下、首次编译约 40 s，所以标成 :data:`CAP_HEAVY`：待唤醒状态绝不加载，
唤醒之后才在后台线程里起来（见 `pipeline._activate/_deactivate` 的两级加载）。

词库：Whisper 没有热词表，只能把词塞进 ``initial_prompt``（解码提示），
所以 :data:`HOTWORDS_PROMPT`。
"""

from __future__ import annotations

from ..spec import CAP_HEAVY, CAP_LOCAL, HOTWORDS_POST, FieldSpec, ProviderSpec

WHISPER_DIR = "models/asr/whisper-large-v3-turbo-int8-ov"


def _block(settings) -> dict:
    """`[asr.providers.whisper]` 这一段（★配置写在这里，不是 dataclass 字段★）。"""
    cfg = getattr(settings, "asr", None)
    return dict((getattr(cfg, "providers", {}) or {}).get("whisper") or {})


def model_path(settings) -> str:
    cfg = settings.asr
    return str(_block(settings).get("model") or getattr(cfg, "model", "")
               or getattr(cfg, "whisper_model", "") or "")


def device_of(settings) -> str:
    """设备：`[asr.providers.whisper] device` → `[asr] device` → 老字段 → auto。

    ★为什么三个地方都要看★：新配置写在 provider 块里（不是 AsrConfig 字段），
    只读 dataclass 的话会永远拿到老的 `whisper_device`（实测 `main.py gpu`
    把 `device = "auto"` 显示成 CPU）。
    """
    cfg = settings.asr
    return str(_block(settings).get("device") or getattr(cfg, "device", "")
               or getattr(cfg, "whisper_device", "") or "auto")


def _missing(settings) -> list[str]:
    value = model_path(settings)
    if value and not settings.resolve(value).exists():
        return [f"Whisper: {value}"]
    return []


def _ready(settings) -> tuple[bool, str]:
    missing = _missing(settings)
    if missing:
        return False, "缺 " + "、".join(missing)
    return True, ""


def _create(settings, spec):
    """★按「GPU 优先、CPU 兜底」的顺序试★：设备选错、驱动不灵时不至于整条路没了。"""
    from ...accel import pick_whisper_devices  # noqa: PLC0415
    from ..whisper_ov import WhisperOpenVinoEngine  # noqa: PLC0415 - 扫目录时不加载引擎

    cfg = settings.asr
    model_dir = settings.resolve(model_path(settings))
    language = str(getattr(cfg, "language", "zh") or "zh")
    threads = int(getattr(cfg, "num_threads", 0) or 0)
    stamps = bool(getattr(cfg, "whisper_word_timestamps", False))
    prompt = str(spec.params.get("initial_prompt", getattr(cfg, "initial_prompt", "")) or "")
    errors: list[str] = []
    for device in pick_whisper_devices(device_of(settings)):
        try:
            return WhisperOpenVinoEngine(
                model_dir=model_dir, device=device, language=language,
                num_threads=threads, word_timestamps=stamps, initial_prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001 - 换下一个设备
            errors.append(f"{device}: {type(exc).__name__}: {exc}")
    raise RuntimeError("Whisper 在所有设备上都没起来：" + " | ".join(errors or ["没有可用设备"]))


PROVIDER = ProviderSpec(
    id="whisper",
    label="Whisper large-v3-turbo（本地 · 高精度，按需加载）",
    capabilities=frozenset({CAP_LOCAL, CAP_HEAVY}),
    hotwords=HOTWORDS_POST,       # ★这个导出不支持提示词偏置，词库走事后纠正★
    fields=(
        FieldSpec("model", "模型目录", "path", default=WHISPER_DIR),
        FieldSpec("device", "设备", "select", default="auto",
                  options=("auto", "CPU", "GPU", "NPU"),
                  hint="auto = 有核显用核显；NPU 跑这个导出会崩进程，auto 绝不选它"),
        FieldSpec("language", "语言", "select", default="zh", options=("zh", "en", "auto")),
        FieldSpec("num_threads", "线程数（仅 CPU）", "number", default=0),
        FieldSpec("whisper_word_timestamps", "输出时间戳", "bool", default=False),
        FieldSpec("initial_prompt", "解码提示词（当前版本不生效）", "text",
                  hint="这个 OpenVINO 导出 + transformers 5.x 没有 prompt 入口；"
                       "Whisper 的词库走事后纠正"),
    ),
    ready=_ready,
    create=_create,
    missing=_missing,
    hint="准但重：只在唤醒之后后台加载，回到待唤醒立刻卸载。",
)
