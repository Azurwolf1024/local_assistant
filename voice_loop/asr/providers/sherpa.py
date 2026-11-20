"""任意 sherpa-onnx 离线模型（paraformer / transducer / CTC）—— 「换模型不改代码」。

★为什么值得单独做一个 provider★：词库真正能"喂进引擎"的那条路只有 transducer 系
（``hotwords_file`` 加权）。SenseVoice 只能同音字替换、Whisper 只能塞提示词，
所以想认真提准确率，通常就是换一个 zipformer/paraformer 上来。

配置示例（`config.toml`）::

    [asr]
    provider = "sherpa"

    [asr.providers.sherpa]
    kind = "transducer"
    encoder = "models/asr/zipformer/encoder.onnx"
    decoder = "models/asr/zipformer/decoder.onnx"
    joiner = "models/asr/zipformer/joiner.onnx"
    tokens = "models/asr/zipformer/tokens.txt"
    # 词库由 [asr.lexicon] 自动生成后填进来（不用手写）
"""

from __future__ import annotations

from ..spec import CAP_FAST, CAP_LOCAL, HOTWORDS_NATIVE, FieldSpec, ProviderSpec

KINDS = ("paraformer", "transducer", "ctc")


def _paths(settings) -> dict:
    cfg = settings.asr
    return {
        "kind": str(getattr(cfg, "sherpa_kind", "") or "paraformer").lower(),
        "model": str(getattr(cfg, "model", "") or getattr(cfg, "sherpa_model", "") or ""),
        "tokens": str(getattr(cfg, "tokens", "") or getattr(cfg, "sherpa_tokens", "") or ""),
        "encoder": str(getattr(cfg, "sherpa_encoder", "") or ""),
        "decoder": str(getattr(cfg, "sherpa_decoder", "") or ""),
        "joiner": str(getattr(cfg, "sherpa_joiner", "") or ""),
    }


def _missing(settings) -> list[str]:
    got = _paths(settings)
    kind = got["kind"]
    out = []
    needed = ["tokens"]
    needed += ["encoder", "decoder", "joiner"] if kind == "transducer" else ["model"]
    labels = {"tokens": "tokens.txt", "model": "模型", "encoder": "encoder",
              "decoder": "decoder", "joiner": "joiner"}
    for key in needed:
        value = got.get(key, "")
        if not value:
            out.append(f"sherpa {labels[key]}：没配路径")
        elif not settings.resolve(value).is_file():
            out.append(f"sherpa {labels[key]}: {value}")
    return out


def _ready(settings) -> tuple[bool, str]:
    missing = _missing(settings)
    if missing:
        return False, "；".join(missing[:3])
    return True, ""


def _create(settings, spec):
    from ..sherpa_offline import SherpaOfflineEngine  # noqa: PLC0415

    cfg = settings.asr
    got = _paths(settings)
    hotwords = str(spec.params.get("hotwords_file", getattr(cfg, "hotwords_file", "")) or "")
    return SherpaOfflineEngine(
        kind=got["kind"],
        model=settings.resolve(got["model"]) if got["model"] else "",
        tokens=settings.resolve(got["tokens"]) if got["tokens"] else "",
        encoder=settings.resolve(got["encoder"]) if got["encoder"] else "",
        decoder=settings.resolve(got["decoder"]) if got["decoder"] else "",
        joiner=settings.resolve(got["joiner"]) if got["joiner"] else "",
        num_threads=int(getattr(cfg, "num_threads", 4) or 4),
        provider=str(getattr(cfg, "provider_device", "") or "cpu"),
        language=str(getattr(cfg, "language", "zh") or "zh"),
        hotwords_file=settings.resolve(hotwords) if hotwords else "",
        hotwords_score=float(getattr(cfg, "hotwords_score", 1.5) or 1.5),
        modeling_unit=str(getattr(cfg, "modeling_unit", "cjkchar") or "cjkchar"),
        bpe_vocab=str(getattr(cfg, "bpe_vocab", "") or ""),
        rule_fsts=str(getattr(cfg, "rule_fsts", "") or ""),
    )


PROVIDER = ProviderSpec(
    id="sherpa",
    label="任意 sherpa-onnx 模型（paraformer / zipformer / CTC）",
    capabilities=frozenset({CAP_LOCAL, CAP_FAST}),
    hotwords=HOTWORDS_NATIVE,
    fields=(
        FieldSpec("sherpa_kind", "模型形态", "select", default="paraformer", options=KINDS),
        FieldSpec("model", "模型文件（paraformer/ctc）", "path"),
        FieldSpec("sherpa_encoder", "encoder（transducer）", "path"),
        FieldSpec("sherpa_decoder", "decoder（transducer）", "path"),
        FieldSpec("sherpa_joiner", "joiner（transducer）", "path"),
        FieldSpec("tokens", "tokens.txt", "path"),
        FieldSpec("num_threads", "线程数", "number", default=4),
        FieldSpec("provider_device", "推理设备", "select", default="cpu", options=("cpu", "cuda")),
        FieldSpec("hotwords_file", "热词表（★由 [asr.lexicon] 自动生成★）", "path",
                  hint="只有 transducer 支持；留空就自动用词库生成的那份"),
        FieldSpec("hotwords_score", "热词权重", "number", default=1.5),
        FieldSpec("modeling_unit", "热词切分单位", "select", default="cjkchar",
                  options=("cjkchar", "bpe", "cjkchar+bpe")),
    ),
    ready=_ready,
    create=_create,
    missing=_missing,
    hint="想认真提准确率就换它：transducer 系原生支持热词加权（词库真能喂进引擎）。",
)
