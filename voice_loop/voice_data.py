"""角色语音素材盘点：哪些角色够条件训练自己的声线。

「够条件」的定义很朴素，但必须写清楚，否则容易白跑几小时训练：

* **有音频**：``data/personas/<id>/`` 里有 wav；
* **有对应文本**：每个 wav 都能找到文本——同名 ``.txt``，或者同目录的清单文件
  （``<id>.txt``，格式见 :mod:`voice_loop.manifest`）；
* **量够**：总时长 ≥ ``MIN_SECONDS``（默认 60 秒）。低于这个量微调很容易过拟合，
  不如直接用零样本克隆（``voice_ref``）。

不满足的角色不是不能做，而是**先补素材更划算**：缺文本可以用
``python scripts/transcribe_lines.py`` 之类的流程补（本项目有 SenseVoice），
或者手工写；缺音频就得多下几段。

这里只做「盘点」，不动数据、不训练——真正的训练见 ``scripts/persona_voice.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .manifest import manifest_pairs

# 够条件的最低门槛
MIN_SECONDS = 60.0        # 总时长下限（秒）
MIN_CLIPS = 5             # 条数下限
TEXT_COVERAGE = 0.8       # 有文本的条数占比下限

MODEL_SUBDIR = "models/tts/zipvoice/personas"   # 训好的角色模型放这儿
_WAV_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a"}


def _duration(path: Path) -> float:
    """读音频时长（秒）。读不了就当 0，不抛。"""
    try:
        import soundfile as sf  # noqa: PLC0415

        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:  # noqa: BLE001 - 盘点阶段不该因为一个坏文件炸掉
        return 0.0


@dataclass
class VoiceData:
    """一个角色的素材与模型状态。"""

    id: str
    name: str
    audio_dir: Path
    clips: list[Path] = field(default_factory=list)
    seconds: float = 0.0
    with_text: list[str] = field(default_factory=list)   # 已有文本的音频名
    without_text: list[str] = field(default_factory=list)
    model_dir: Path | None = None
    persona_json: Path | None = None                  # 人格文件（写 voice_model 时用）
    reference: str = ""                                  # persona 里配的 voice_ref

    # ------------------------------------------------------------------ 判断
    @property
    def text_coverage(self) -> float:
        if not self.clips:
            return 0.0
        return len(self.with_text) / len(self.clips)

    @property
    def has_audio(self) -> bool:
        return bool(self.clips)

    @property
    def has_text(self) -> bool:
        return bool(self.clips) and self.text_coverage >= TEXT_COVERAGE

    @property
    def enough(self) -> bool:
        return self.seconds >= MIN_SECONDS and len(self.clips) >= MIN_CLIPS

    @property
    def trained(self) -> bool:
        """训好的模型目录是否完整可用（int8 / fp32 任一套算数）。

        精度定义在 :mod:`voice_loop.tts.precision`，跟真正加载模型时**同一份**。
        """
        if not self.model_dir or not self.model_dir.is_dir():
            return False
        from .tts.precision import has_any  # noqa: PLC0415 - 只有查这个属性时才需要

        if not has_any(self.model_dir):
            return False
        return all((self.model_dir / name).exists() for name in ("tokens.txt", "lexicon.txt", "espeak-ng-data"))

    @property
    def status(self) -> str:
        if not self.has_audio:
            return "缺音频"
        if not self.has_text:
            return f"缺文本（{len(self.without_text)}/{len(self.clips)} 条没配）"
        if not self.enough:
            return f"素材偏少（{self.seconds:.0f}s / {len(self.clips)} 条）"
        return "已训模型" if self.trained else "可训练"

    @property
    def ready(self) -> bool:
        return self.status == "可训练"


def persona_audio_dir(persona_json: Path, char_id: str, explicit: str = "", root: Path | None = None) -> Path:
    """素材目录：人格里写了 ``voice_dir`` 就用它，否则按约定 ``<人格 json 所在目录>/<id>/``。"""
    if explicit:
        p = Path(explicit)
        if p.is_absolute():
            return p
        return (root or Path.cwd()) / p
    return persona_json.parent / char_id


def inspect(persona_json: Path, char_id: str, char_name: str, reference: str = "",
            root: Path | None = None, voice_dir: str = "") -> VoiceData:
    """盘点一个角色（只看约定目录，不做任何网络/模型操作）。"""
    base = Path(root) if root else persona_json.parent.parent.parent
    audio_dir = persona_audio_dir(persona_json, char_id, explicit=voice_dir, root=base)
    data = VoiceData(id=char_id, name=char_name, audio_dir=audio_dir, reference=reference)
    data.persona_json = Path(persona_json)
    data.model_dir = base / MODEL_SUBDIR / char_id
    if not audio_dir.is_dir():
        return data

    wavs = sorted(p for p in audio_dir.iterdir() if p.suffix.lower() in _WAV_SUFFIXES)
    texts = {k.lower(): v for k, v in manifest_pairs(audio_dir).items() if v.strip()}
    for wav in wavs:
        data.clips.append(wav)
        data.seconds += _duration(wav)
        has_text = bool(texts.get(wav.name.lower()) or texts.get(wav.stem.lower()))
        if not has_text:
            sidecar = wav.with_suffix(".txt")
            has_text = sidecar.exists() and bool(sidecar.read_text(encoding="utf-8", errors="replace").strip())
        (data.with_text if has_text else data.without_text).append(wav.name)
    return data


def inspect_all(registry, root: Path) -> list[VoiceData]:
    """盘点索引里的所有角色（按名字排序，稳定输出）。"""
    out: list[VoiceData] = []
    files = getattr(registry, "files", {}) or {}
    for char in sorted(registry.all(), key=lambda c: c.name):
        path = files.get(char.id)
        if not path:
            continue  # 旧的内联写法：没人格文件，素材目录无从谈起
        out.append(
            inspect(
                Path(path),
                char.id,
                char.name,
                reference=str(getattr(char, "voice_ref", "") or ""),
                root=root,
                voice_dir=str(getattr(char, "voice_dir", "") or ""),
            )
        )
    return out


def render_report(items: list[VoiceData], root: Path) -> str:
    """给人看的盘点表。"""
    lines = ["角色语音素材盘点", "=" * 78]
    if not items:
        return "\n".join(lines + ["（data/characters.json 里没有角色）"])

    ready = [d for d in items if d.ready]
    lines.append(
        f"共 {len(items)} 个角色；够条件训自己的声线：{len(ready)} 个"
        f"（门槛：≥{MIN_SECONDS:.0f} 秒 / ≥{MIN_CLIPS} 条 / 文本覆盖 ≥{TEXT_COVERAGE:.0%}）"
    )
    lines.append("")
    header = f"{'角色':<10} {'条数':>4} {'时长':>8} {'文本覆盖':>8}  {'状态':<26} 模型目录"
    lines.append(header)
    lines.append("-" * 78)
    for item in items:
        model = ""
        if item.model_dir:
            try:
                model = str(item.model_dir.relative_to(root)).replace("\\", "/")
            except ValueError:
                model = str(item.model_dir)
        lines.append(
            f"{item.name:<10} {len(item.clips):>4} {item.seconds:>7.1f}s "
            f"{item.text_coverage:>7.0%}  {item.status:<26} "
            + (model if item.trained else "")
        )
    lines.append("")

    if ready:
        names = "、".join(d.name for d in ready)
        lines.append(f"可以开始训练：{names}")
        lines.append("  python scripts/persona_voice.py --persona <id>          # 一条龙：数据→训练→导出→安装→校验")
    lacking_text = [d for d in items if d.has_audio and not d.has_text]
    if lacking_text:
        lines.append("")
        lines.append("有音频但缺文本（补上文本就能训）：")
        for d in lacking_text:
            sample = "、".join(d.without_text[:3])
            lines.append(f"  {d.name}：{len(d.without_text)} 条缺文本，例如 {sample}")
        lines.append(f"  文本放 {lacking_text[0].audio_dir}\\<id>.txt，格式是「文件名一行 + 正文一行」")
    no_audio = [d for d in items if not d.has_audio]
    if no_audio:
        lines.append("")
        lines.append("还没放音频（只靠 voice_ref 零样本克隆）：" + "、".join(d.name for d in no_audio))
    return "\n".join(lines)
