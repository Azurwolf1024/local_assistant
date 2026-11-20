"""SenseVoiceSmall（sherpa-onnx）—— 常驻的中文快路径。

★它永远是"常驻"那一个★：230 MB、CPU 上 10 倍速以上，待唤醒状态也只留它一个
（Whisper 那种 1 GB / 首次编译 40 s 的引擎绝不常驻，见 :data:`CAP_HEAVY`）。

词库：不支持热词表，但 sherpa-onnx 给了 ``hr_lexicon`` / ``hr_dict_dir``
（同音字替换），配了就走引擎侧，没配就走事后纠正（``hotwords="post"``）。
"""

from __future__ import annotations

from ..spec import CAP_FAST, CAP_LOCAL, HOTWORDS_POST, FieldSpec, ProviderSpec

SENSEVOICE_DIR = "models/asr/sensevoice-small"


def model_path(settings) -> str:
    """新写法 ``[asr.providers.sensevoice] model`` → 老写法 ``sensevoice_model``。"""
    cfg = settings.asr
    return str(getattr(cfg, "model", "") or getattr(cfg, "sensevoice_model", "") or "")


def tokens_path(settings) -> str:
    cfg = settings.asr
    return str(getattr(cfg, "tokens", "") or getattr(cfg, "sensevoice_tokens", "") or "")


def _missing(settings) -> list[str]:
    out = []
    for label, value in (("SenseVoice", model_path(settings)), ("SenseVoice tokens", tokens_path(settings))):
        if value and not settings.resolve(value).is_file():
            out.append(f"{label}: {value}")
    return out


def _ready(settings) -> tuple[bool, str]:
    missing = _missing(settings)
    if missing:
        return False, "缺 " + "、".join(missing)
    return True, ""


def _create(settings, spec):
    from ..sensevoice import SenseVoiceEngine  # noqa: PLC0415 - 扫目录时不加载引擎

    cfg = settings.asr
    return SenseVoiceEngine(
        model_path=settings.resolve(model_path(settings)),
        tokens_path=settings.resolve(tokens_path(settings)),
        num_threads=int(getattr(cfg, "num_threads", 4) or 4),
        use_itn=bool(spec.params.get("use_itn", getattr(cfg, "sensevoice_use_itn", True))),
        language=str(getattr(cfg, "language", "zh") or "zh"),
        hr_lexicon=str(getattr(cfg, "hr_lexicon", "") or ""),
        hr_dict_dir=str(getattr(cfg, "hr_dict_dir", "") or ""),
    )


PROVIDER = ProviderSpec(
    id="sensevoice",
    label="SenseVoiceSmall（本地 · 低延迟）",
    capabilities=frozenset({CAP_LOCAL, CAP_FAST}),
    hotwords=HOTWORDS_POST,
    fields=(
        FieldSpec("model", "模型文件", "path", default=f"{SENSEVOICE_DIR}/model.int8.onnx"),
        FieldSpec("tokens", "tokens.txt", "path", default=f"{SENSEVOICE_DIR}/tokens.txt"),
        FieldSpec("language", "语言", "select", default="zh", options=("zh", "en", "auto")),
        FieldSpec("num_threads", "线程数", "number", default=6),
        FieldSpec("sensevoice_use_itn", "数字/日期规整（ITN）", "bool", default=True),
        FieldSpec("hr_lexicon", "同音字替换词表（可选）", "path",
                  hint="sherpa-onnx 的 hr_lexicon：一行一个词，配合 hr_dict_dir 用"),
        FieldSpec("hr_dict_dir", "拼音字典目录（可选）", "path",
                  hint="同音字替换要用它；没有就退回事后纠正"),
    ),
    ready=_ready,
    create=_create,
    missing=_missing,
    hint="出厂常驻引擎：最快、最省内存；不支持热词表，词库靠事后纠正。",
)
