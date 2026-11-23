"""Piper：低延迟固定音色（出厂声线，不需要任何素材）。

★永远是降级链的最后一环★：它只依赖一个 onnx + espeak-ng-data，装完就在本地，
不联网、不需要 key，所以是降级链的最后一环（★前提是本地确实装了 onnx 声线，
`_ready()` 会在体检阶段把缺模型的情况报出来）。
"""

from __future__ import annotations

from ..spec import CAP_SPEED, FieldSpec, ProviderSpec

PIPER_DIR = "models/tts/piper"


def _voices(settings) -> list[str]:
    """`models/tts/piper/` 下所有可用的声线名（去掉 .onnx 后缀）。"""
    room = settings.resolve(PIPER_DIR)
    if not room.is_dir():
        return []
    return sorted(p.stem for p in room.glob("*.onnx"))


def _apply(settings, spec):
    tts = settings.tts
    voice = str(spec.voice_id or "").strip()
    if voice:
        tts.voice = voice
        # 模型路径跟着声线走（以前这段写死在 pipeline._apply_voice 里）
        tts.model = f"{PIPER_DIR}/{voice}.onnx"
        tts.config = f"{PIPER_DIR}/{voice}.onnx.json"
    model = str(spec.model or "").strip()
    if model:
        tts.model = model
        tts.config = (model[: -len(".onnx")] + ".onnx.json") if model.endswith(".onnx") else ""
    return settings


def _create(settings, spec):
    from ..piper_tts import PiperTts  # noqa: PLC0415 - 扫目录时不加载引擎

    return PiperTts(settings)


def _ready(settings) -> tuple[bool, str]:
    """体检：至少得有一个本地 onnx 声线，否则链尾也是哑的。

    配置里写的具体声线不存在时，引擎会在 `models/tts/piper/` 下另找一个，
    所以这里按「目录里有没有 onnx」判定，不按单个文件名。
    """
    if _voices(settings):
        return True, ""
    return False, (f"找不到 Piper 声线：{PIPER_DIR}/*.onnx"
                   "（python scripts/download_models.py --only piper）")


PROVIDER = ProviderSpec(
    id="piper",
    label="Piper（本地 · 低延迟）",
    capabilities=frozenset({CAP_SPEED}),
    fields=(
        FieldSpec("voice", "piper 声线", "select", default="zh_CN-huayan-medium",
                  hint="models/tts/piper/ 里的 onnx 名"),
        FieldSpec("model", "模型文件", "path", hint="一般留空，按声线名自动拼"),
        FieldSpec("use_cuda", "用 CUDA", "bool", default=False),
        FieldSpec("length_scale", "语速（大=慢）", "number", default=1.0),
        FieldSpec("noise_scale", "音高变化", "number", default=0.667),
        FieldSpec("noise_w_scale", "音长变化", "number", default=0.85),
    ),
    create=_create,
    apply=_apply,
    ready=_ready,
    voices=_voices,
    hint="出厂声线，不碰任何克隆素材；也是所有后端的保底退路。",
)


def model_available(settings, voice: str) -> bool:
    name = str(voice or "").strip()
    if not name:
        return False
    return settings.resolve(f"{PIPER_DIR}/{name}.onnx").is_file()
