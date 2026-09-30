"""「只用一条语音就能克隆」的落地部分：把一条音频变成某个角色的克隆参考。

★为什么放在控制台的模块里（而不是 voice_loop/tts/）★：
这里做的事只有三件 —— 挑候选、校验一条音频、把结果写进人格文件。
真正出声还是**服务里**那套 TTS（控制台自己加载 ZipVoice 要几百 MB 到几 GB，
而且两个进程抢同一张声卡没好处，见 panels/voices.py 开头的说明）。
所以本模块**不导入任何模型**，纯文件操作 + 一点元数据检查，测试里可以放心跑。

零样本克隆的原理（见工程日志第 26 节）：给一段「参考音频 + 它对应的文本」，
模型就能用同一个音色说任何话 —— 所以「一条语音」够用，不必先训模型。
"""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 参考音频的合理范围：太短没音色，太长白等（ZipVoice 编码参考也要时间）
MIN_SECONDS = 0.8
MAX_SECONDS = 60.0
MAX_UPLOAD_MB = 25.0
AUDIO_EXTS = (".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac", ".opus")
# 上传后的落盘名字（放在角色素材目录里，和别的素材一起）
CLONE_REF_STEM = "clone-ref"


@dataclass
class Clip:
    """一条候选音频的体检结果。"""

    path: str = ""
    name: str = ""
    seconds: float = 0.0
    samplerate: int = 0
    channels: int = 0
    size_mb: float = 0.0
    ok: bool = False
    problem: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "seconds": round(self.seconds, 2),
            "samplerate": self.samplerate, "channels": self.channels,
            "size_mb": round(self.size_mb, 2), "ok": self.ok,
            "problem": self.problem, "note": self.note,
        }


# --------------------------------------------------------------------------- #
def _probe_wave(path: Path) -> tuple[float, int, int, str]:
    """读 wav 的时长/采样率/声道；读不动就返回空 + 原因。"""
    try:
        import wave

        with wave.open(str(path), "rb") as got:
            rate = got.getframerate() or 0
            frames = got.getnframes() or 0
            return (frames / rate if rate else 0.0), rate, got.getnchannels(), ""
    except Exception as exc:  # noqa: BLE001 - 不是 wav / 头坏了
        try:                                   # 非 wav（mp3/m4a…）交给 soundfile
            import soundfile as sf

            info = sf.info(str(path))
            return float(info.duration), int(info.samplerate), int(info.channels), ""
        except Exception:  # noqa: BLE001
            return 0.0, 0, 0, f"读不出音频信息（{type(exc).__name__}）"


def inspect(path: str | Path) -> Clip:
    """体检一条音频：存在吗、多长、什么格式、适合当参考吗。"""
    p = Path(path)
    clip = Clip(path=str(p), name=p.name)
    if not p.is_file():
        clip.problem = "文件不存在"
        return clip
    clip.size_mb = p.stat().st_size / 1048576
    seconds, rate, channels, err = _probe_wave(p)
    clip.seconds, clip.samplerate, clip.channels = seconds, rate, channels
    if err:
        clip.problem = err
        return clip
    if clip.size_mb > MAX_UPLOAD_MB * 2:
        clip.problem = f"文件太大（{clip.size_mb:.0f} MB）"
        return clip
    if seconds and seconds < MIN_SECONDS:
        clip.problem = f"太短（{seconds:.2f} 秒）——参考音频至少要 {MIN_SECONDS} 秒才够听出音色"
        return clip
    if seconds > MAX_SECONDS:
        clip.problem = f"太长（{seconds:.1f} 秒）——克隆用不了这么长，切一段 {MAX_SECONDS:.0f} 秒以内的"
        return clip
    clip.ok = True
    if not seconds:
        clip.note = "时长没读出来（不影响使用，只是没法帮你判断长短）"
    return clip


def candidates(settings: Any, voice_dir: str = "", voice_ref: str = "") -> list[dict]:
    """这个角色能挑的参考候选：她素材目录里的音频（以及当前参考所在目录）。"""
    roots: list[Path] = []
    if voice_dir:
        roots.append(settings.resolve(voice_dir))
    if voice_ref:
        got = settings.resolve(voice_ref)
        if got.parent not in roots:
            roots.append(got.parent)
    seen: set[str] = set()
    out: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            rel = p.relative_to(settings.root).as_posix() if p.is_relative_to(settings.root) \
                else str(p)
            if rel in seen:
                continue
            seen.add(rel)
            clip = inspect(p)
            out.append({**clip.to_dict(), "path": rel})
    return out


def preview_sentence(char: Any, limit: int = 28) -> str:
    """试听念哪句：用她**自己的台词**里较短的一条（听的是她真会说的话）。

    没有台词就用一句中性的话 —— 但不编造人设。
    """
    lines = list(getattr(char, "lines", None) or [])
    usable = [str(ln.get("text") or "").strip() for ln in lines if isinstance(ln, dict)]
    usable = [t for t in usable if t]
    if usable:
        return min(usable, key=len)[:limit]
    return f"你好，我是{getattr(char, 'name', '') or '这个角色'}。"


# --------------------------------------------------------------------------- #
def _persona_file(settings: Any, char: Any) -> Path:
    """角色的人格文件在哪：索引里写的那条为准（索引不在项目根下也照样找得到）。"""
    index_path = settings.resolve(settings.persona.file)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for item in index.get("characters") or []:
        entry = item if isinstance(item, dict) else {"file": item}
        cid = str(entry.get("id") or Path(str(entry.get("file") or "")).stem)
        if cid == getattr(char, "id", ""):
            target = (index_path.parent / str(entry.get("file") or "")).resolve()
            if target.is_file():
                return target
    fallback = index_path.parent / f"personas/{char.id}.json"
    if fallback.is_file():
        return fallback
    raise ValueError(f"找不到 {char.id} 的人格文件")


def _as_ref(settings: Any, path: Path) -> str:
    """写成 voice_ref 的形式：在项目里就相对（好搬），在外面就绝对（不瞎猜）。"""
    return path.relative_to(settings.root).as_posix() if path.is_relative_to(settings.root) \
        else str(path)


def _write_persona(settings: Any, char: Any, fields: dict) -> dict:
    """把几个字段写回人格文件：改前备份、只动这几个键、写完校验 JSON。

    ★人格文件是唯一真相★（§14）：声线、参考、称呼都从它读，
    所以「克隆」这件事的最后一公里就是这里 —— 写坏了角色就起不来，
    因此每一步都要能回滚（.bak 就在旁边）。
    """
    target = _persona_file(settings, char)
    data = json.loads(target.read_text(encoding="utf-8"))
    changed = {k: v for k, v in fields.items() if str(data.get(k) or "") != str(v or "")}
    if not changed:
        return {"file": str(target), "changed": {}, "backup": ""}
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    data.update(fields)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(target.read_text(encoding="utf-8"))       # 写完校验（写坏了至少当场知道）
    return {"file": str(target), "changed": changed, "backup": str(backup)}


def install(settings: Any, char: Any, *, clip: str = "", data_base64: str = "",
            filename: str = "", ref_text: str = "", dry_run: bool = True) -> dict:
    """把「一条语音」装成这个角色的克隆参考。

    两种来源：`clip`（已有文件，路径相对项目根或绝对路径）/ `data_base64`（控制台里传上来的）。
    上传的那份会存到 `data/personas/<角色>/clone-ref.<ext>`（**跟着人格文件走**，
    不是跟着项目根走 —— 人格文件可能整个搬到别处）✓；
    已有文件则**原地引用**（不复制，避免同一段音频存两份）。
    """
    if not clip and not data_base64:
        raise ValueError("要么给 clip（已有文件路径），要么给 data（上传的音频）")

    saved = ""
    if data_base64:
        try:
            blob = base64.b64decode(data_base64.split(",")[-1], validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"上传的数据不是合法 base64：{exc}") from exc
        if len(blob) > MAX_UPLOAD_MB * 1048576:
            raise ValueError(f"上传的音频超过 {MAX_UPLOAD_MB:.0f} MB")
        ext = Path(filename or "clip.wav").suffix.lower()
        if ext not in AUDIO_EXTS:
            ext = ".wav"
        room = _persona_file(settings, char).parent / char.id
        room.mkdir(parents=True, exist_ok=True)
        target = room / f"{CLONE_REF_STEM}{ext}"
        if not dry_run:
            target.write_bytes(blob)
        rel = _as_ref(settings, target)
        saved = rel
    else:
        rel = _as_ref(settings, settings.resolve(clip))

    info = inspect(settings.resolve(rel))
    if data_base64 and dry_run:
        # 试运行时不落盘，直接对内存里的字节做个大概判断（只看大小 ✓）
        mb = len(base64.b64decode(data_base64.split(",")[-1])) / 1048576
        info = Clip(path=saved or "(上传)", name=filename or "(上传)", size_mb=mb, ok=True,
                    note="试运行：只看大小，没落盘也没读时长")
    if not info.ok:
        raise ValueError(info.problem or "这条音频不能当参考")

    fields = {"voice_ref": rel}
    if ref_text.strip():
        fields["voice_ref_text"] = ref_text.strip()
    if not str(getattr(char, "backend", "") or "").strip():
        # 单条克隆走的是 ZipVoice（零样本）——不写 backend 会跟全局走，写明白更稳
        fields["backend"] = "zipvoice"
    report = _write_persona(settings, char, fields) if not dry_run else \
        {"file": "(试运行没写)", "changed": fields, "backup": ""}
    return {
        "character": char.id, "name": getattr(char, "name", char.id),
        "reference": rel, "uploaded": bool(saved) and not dry_run, "clip": info.to_dict(),
        "dry_run": bool(dry_run), **report,
    }
