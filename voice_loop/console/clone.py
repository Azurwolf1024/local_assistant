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
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..manifest import manifest_pairs
from ..voice_data import persona_audio_dir

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


def _loose(name: str) -> str:
    """松一点的键：去掉空白/标点/大小写差异（「交谈1 (1)」也能和「交谈1」对上）。"""
    return re.sub(r"[\s\.\-_()（）\[\]【】·、]+", "", str(name)).lower()


def material_dir(settings: Any, char: Any) -> Path:
    """她的素材目录：人格里写了 `voice_dir` 就用它，**否则按约定** `<人格文件目录>/<id>/`。

    ★和 `voice_data.persona_audio_dir()` 同一套约定★（那边给 `persona_voice.py` 用）：
    没写 `voice_dir` 的角色（比如能天使）也能在这里看见素材，不必逼人先补一个字段
    —— 之前只从 `voice_ref` 所在目录列候选，没设参考的角色就是一片空白。
    """
    explicit = str(getattr(char, "voice_dir", "") or "")
    cid = str(getattr(char, "id", "") or "")
    try:
        anchor = _persona_file(settings, char)
    except ValueError:                      # 人格文件都找不到：退回项目根下的约定目录
        return settings.resolve(f"data/personas/{cid}")
    return persona_audio_dir(anchor, cid, explicit=explicit, root=settings.root)


def text_index(settings: Any, char: Any = None, room: Path | None = None) -> dict[str, str]:
    """音频名 → 对应文本：素材目录里的清单 txt（`<id>.txt`）＋ 人格 `lines` 的 scene。

    ★为什么要自动匹配★：零样本克隆吃的是「参考音频 + **它对应的**文本」，
    文本对不上就会照着错词对齐 → 听着像「声不对词」（工程日志 §21）。
    所以能按文件名找到就自动填，找不到才让用户手打。
    键同时给原名（小写）与松键，前端拿文件名直接查得上。
    """
    found: dict[str, str] = {}
    directory = room if room is not None else (material_dir(settings, char) if char is not None else None)
    if directory is not None and directory.is_dir():
        for name, body in manifest_pairs(directory).items():
            body = (body or "").strip()
            if name and body:
                found.setdefault(name, body)
                found.setdefault(_loose(name), body)
    lines = getattr(char, "lines", None) or []
    if isinstance(lines, dict):              # 老写法：{场景: 文本}
        lines = [{"scene": k, "text": v} for k, v in lines.items()]
    for row in lines:
        if not isinstance(row, dict):
            continue
        scene = str(row.get("scene") or "").strip()
        text = str(row.get("text") or "").strip()
        if scene and text:
            found.setdefault(scene.lower(), text)
            found.setdefault(_loose(scene), text)
    return found


def text_for(index: dict[str, str], *names: str, room: Path | None = None) -> tuple[str, str]:
    """按文件名找文本：先精确（小写）再松匹配，最后看同名 `.txt`。

    返回 `(文本, 来源)`；找不到就是 `("", "")`。
    """
    for name in names:
        if not name:
            continue
        got = index.get(str(name).lower()) or index.get(_loose(name))
        if got:
            return got, "清单"
    if room is not None:
        for name in names:
            if not name:
                continue
            sidecar = (room / Path(str(name)).name).with_suffix(".txt")
            if not sidecar.is_file():
                continue
            try:
                body = sidecar.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if body:
                return body, "同名文本"
    return "", ""


def candidates(settings: Any, char: Any = None, *, voice_dir: str = "", voice_ref: str = "",
               limit: int = 200) -> list[dict]:
    """这个角色能挑的参考候选：她的素材目录（约定 `data/personas/<id>/`）＋参考所在目录。

    每条候选除了体检结果，还带 `text`：按**文件名**从素材清单 / 她的台词里找到的原文。
    素材目录不存在时返回空表（UI 会提示「就传一条」）。
    """
    vdir = voice_dir or str(getattr(char, "voice_dir", "") or "")
    vref = voice_ref or str(getattr(char, "voice_ref", "") or "")
    roots: list[Path] = []
    room: Path | None = None
    if char is not None or vdir:
        room = material_dir(settings, char) if char is not None else settings.resolve(vdir)
        roots.append(room)
    if vdir:
        got = settings.resolve(vdir)
        if got not in roots:
            roots.append(got)
    if vref:
        got = settings.resolve(vref)
        if got.parent not in roots:
            roots.append(got.parent)

    index = text_index(settings, char, room)
    seen: set[str] = set()
    out: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if len(out) >= limit:
                return out
            path = _as_ref(settings, p)
            if path in seen:
                continue
            seen.add(path)
            clip = inspect(p)
            text, source = text_for(index, p.name, p.stem, room=root)
            out.append({**clip.to_dict(), "path": path, "text": text, "text_source": source})
    return out


def ui_index(settings: Any, char: Any, rows: list[dict] | None = None) -> dict[str, str]:
    """给控制台用的查表 `{松键(文件名): 文本}` —— 用户自己传音频时按文件名直接查得上。"""
    rows = rows if rows is not None else candidates(settings, char)
    out: dict[str, str] = {}
    for row in rows:
        text = str(row.get("text") or "")
        if text:
            out.setdefault(_loose(Path(str(row.get("name") or "")).stem), text)
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
    """角色的人格文件在哪：索引里写的那条为准（索引不在项目根下也照样找得到）。

    ★只有一份实现★：`persona_card.file_of()` ——「哪个文件是这个角色的」这件事
    资料卡编辑、克隆、素材目录三处都要答一遍，各写一套迟早答得不一样。
    """
    from . import persona_card as card  # noqa: PLC0415 - 避免模块级循环导入

    target = card.file_of(settings, str(getattr(char, "id", "") or ""))
    if not target.is_file():
        raise ValueError(f"找不到 {char.id} 的人格文件")
    return target


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

    # ★参考文本★：零样本克隆要的是「这条音频 + 它逐字对应的文本」。
    # 用户没手填就按文件名从素材清单/她的台词里找（找得到最准）；
    # 换了音频却又找不到新文本 → 把上一段那行清掉（留着就是「声不对词」）。
    # ★旧值一律读人格文件★：内存里的 char 可能是几秒前那份（角色是被监听热加载的）。
    room = material_dir(settings, char)
    try:
        current = json.loads(_persona_file(settings, char).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    chosen = Path(filename or rel).name
    matched, source = text_for(text_index(settings, char, room), chosen,
                               Path(chosen).stem, room=room)
    text = ref_text.strip() or matched
    cleared = False
    fields = {"voice_ref": rel}
    if text:
        fields["voice_ref_text"] = text
    elif str(current.get("voice_ref_text") or "").strip() \
            and str(current.get("voice_ref") or "") != rel:
        fields["voice_ref_text"] = ""
        cleared = True
    if not str(current.get("backend") or getattr(char, "backend", "") or "").strip():
        # 单条克隆走的是 ZipVoice（零样本）——不写 backend 会跟全局走，写明白更稳
        fields["backend"] = "zipvoice"
    report = _write_persona(settings, char, fields) if not dry_run else \
        {"file": "(试运行没写)", "changed": fields, "backup": ""}
    return {
        "character": char.id, "name": getattr(char, "name", char.id),
        "reference": rel, "ref_text": text, "text_source": source, "cleared_text": cleared,
        "uploaded": bool(saved) and not dry_run, "clip": info.to_dict(),
        "dry_run": bool(dry_run), **report,
    }
