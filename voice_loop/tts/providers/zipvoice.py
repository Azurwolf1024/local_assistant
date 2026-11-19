"""ZipVoice：零样本音色克隆（一段参考音频 + 逐字文本 → 把音色搬到中文上）。

★这里的「音色」不是一个 id，而是「模型目录 + 参考音频」★，所以
:class:`VoiceSpec` 里用 ``model``（微调过的角色模型目录）和 ``ref_audio``/``ref_text``
表达。以前这段映射散在 `pipeline._apply_reference` 里（还顺手改了全局配置），
现在收在 :func:`_apply` 一个函数里。

缺文件时**不切**（宁可音色不对，也不能哑）—— 判定沿用引擎自己的
``missing_files`` / ``precision``，报告与运行时永远一致。
"""

from __future__ import annotations

from pathlib import Path

from ..spec import CAP_CLONE, CAP_SPEED, CAP_STYLE, FieldSpec, ProviderSpec

MODEL_WANTED = ("tokens.txt", "lexicon.txt", "espeak-ng-data")


def _missing_model(settings, path: Path) -> list[str]:
    """角色的声音模型目录缺哪些文件（空 = 齐了）。"""
    from .. import precision  # noqa: PLC0415 - 只在检查模型时用

    missing = [str(path / name) for name in MODEL_WANTED if not (path / name).exists()]
    if missing:
        return missing
    if precision.has_any(path):
        return []
    want = precision.want_precision(settings)
    return [str(path / name) for name in precision.PRECISION_FILES[want]]


def _apply(settings, spec):
    tts = settings.tts
    model = str(spec.model or "").strip()
    if model:
        path = settings.resolve(model)
        missing = _missing_model(settings, path) if path.is_dir() else [str(path)]
        if missing:
            print(f"[tts] 声音模型 {model} 缺文件（{missing[0]}），继续用默认模型", flush=True)
        else:
            tts.clone_dir = model

    ref = str(spec.ref_audio or "").strip()
    if ref:
        if settings.resolve(ref).exists():
            tts.clone_audio = ref
            tts.clone_text = str(spec.ref_text or "").strip()
        else:
            print(f"[tts] 参考音频 {ref} 不存在，继续用当前的", flush=True)
    return settings


def _create(settings, spec):
    from ..zipvoice_tts import ZipVoiceTts  # noqa: PLC0415 - 扫目录时不加载引擎

    return ZipVoiceTts(settings)


def _ready(settings) -> tuple[bool, str]:
    from ..zipvoice_tts import missing_files  # noqa: PLC0415

    missing = missing_files(settings)
    if missing:
        return False, (f"模型没装全（缺 {missing[0].name}）；"
                       "装法：python scripts/download_models.py --only zipvoice")
    return True, ""


PROVIDER = ProviderSpec(
    id="zipvoice",
    label="ZipVoice（本地 · 零样本克隆）",
    capabilities=frozenset({CAP_CLONE, CAP_STYLE, CAP_SPEED}),
    fields=(
        FieldSpec("model", "专属模型目录", "path",
                  hint="训过专属声线才填；空 = 用 [tts] providers.zipvoice.dir"),
        FieldSpec("ref_audio", "参考音频", "path", hint="5~15 秒、单人、无背景音"),
        FieldSpec("ref_text", "参考音频的逐字文本", "text",
                  hint="必须跟音频逐字一致，否则音色退化"),
        FieldSpec("clone_dir", "模型目录", "path"),
        FieldSpec("clone_steps", "流匹配步数", "number", default=4,
                  hint="4 最快；8 慢一倍，输出时长完全一样"),
        FieldSpec("clone_precision", "精度", "select", default="int8",
                  options=("int8", "fp32")),
        FieldSpec("clone_speed", "语速", "number", default=1.0, hint=">1 会弄坏，已夹到 1.0"),
        FieldSpec("clone_min_chars", "段内最短切句", "number", default=16),
        FieldSpec("clone_threads", "线程数", "number", default=2),
    ),
    create=_create,
    apply=_apply,
    ready=_ready,
    hint="用一条参考音频把音色搬到中文上；没有参考音频时就是「男/女通用音」。",
)
