"""Piper：低延迟固定音色（出厂声线，不需要任何素材）。

★永远是降级链的最后一环★：它只依赖一个 onnx + espeak-ng-data，装完就在本地，
不联网、不需要 key，所以「不管上面出什么事，至少还能出声」这条不变量靠它兜住。
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
    voices=_voices,
    hint="出厂声线，不碰任何克隆素材；也是所有后端的保底退路。",
)


def model_available(settings, voice: str) -> bool:
    name = str(voice or "").strip()
    if not name:
        return False
    return settings.resolve(f"{PIPER_DIR}/{name}.onnx").is_file()
