"""识别评测：★改了什么，立刻能量出好坏★。

为什么必须有这一层
    识别优化最容易变成"凭感觉"：调个阈值、换个模型、加两条词，听起来好像好了 ——
    但 CER 可能反而升高、误唤醒可能翻倍。所以先把指标落成代码，后面所有
    「针对性优化」（词库、复核条件、KWS）都用它判断，而不是靠印象。

四项指标
    CER         字错率（编辑距离 / 参考长度）—— 准不准
    唤醒命中率   喊了它有没有醒 —— 醒了才谈得上后面的一切
    误唤醒率     没喊它也醒 —— 这个高了比不准更烦人
    RTF / 延迟   推理耗时 / 音频时长 —— 快不快（唤醒速度看这个）

素材两来源（★都要有★）
    `data/asr_eval/<集合>/*.wav` + 同名 `.txt`：真机录音（含口音与噪声，最可信）
    用本地 TTS 自动合成：覆盖面广（唤醒词 / 人名 / 领域词全都来一遍），零人工
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

SETS = ("wake", "names", "command", "negative")

_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】…—\-~·]+")


def normalize_text(text: str) -> str:
    """比对前先归一：去标点空白、全角数字转半角、大小写统一。

    ★ITN 会把「九」写成 9、把「三点」写成 3:00★，不归一的话 CER 会虚高，
    让人误以为模型变差了。
    """
    out = _PUNCT.sub("", str(text or "")).lower()
    return out.translate({0xFF10 + i: 0x30 + i for i in range(10)})


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（字符级；短句直接两行滚动数组）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    """字错率：0 = 全对；>1 = 比参考还长很多（通常意味着幻觉/复读）。"""
    r, h = normalize_text(ref), normalize_text(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


@dataclass
class Case:
    """一条评测素材。"""

    name: str
    wav: Path
    ref: str = ""
    set_name: str = "command"      # 目录名（wake / names / command / negative）
    group: str = ""                # 相对路径（`synth/wake` —— 合成素材带 synth 前缀）


@dataclass
class CaseResult:
    name: str
    set_name: str
    ref: str = ""
    hyp: str = ""
    engine: str = ""
    cer: float = 0.0
    rtf: float = 0.0
    latency: float = 0.0
    woke: bool = False        # 唤醒集合：喊了有没有醒
    false_wake: bool = False  # 负样本集合：没喊有没有醒
    group: str = ""           # 相对路径分组（synth/wake → "synth/wake"）
    note: str = ""


@dataclass
class Report:
    cases: list[CaseResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 汇总
    def subset(self, set_name: str) -> list[CaseResult]:
        return [c for c in self.cases if c.set_name == set_name]

    def summary(self) -> dict:
        scored = [c for c in self.cases if c.ref]
        wake = self.subset("wake")
        neg = self.subset("negative")
        rtf = [c.rtf for c in self.cases if c.rtf > 0]
        out = {
            "cases": len(self.cases),
            "cer": round(sum(c.cer for c in scored) / len(scored), 4) if scored else 0.0,
            "wake_hit": round(sum(1 for c in wake if c.woke) / len(wake), 3) if wake else None,
            "false_wake": round(sum(1 for c in neg if c.false_wake) / len(neg), 3) if neg else None,
            "rtf": round(sum(rtf) / len(rtf), 3) if rtf else 0.0,
            "latency": round(sum(c.latency for c in self.cases) / len(self.cases), 3)
            if self.cases else 0.0,
        }
        return out

    def lines(self) -> list[str]:
        """给人看的表格（scripts/eval_asr.py 直接打印）。"""
        out: list[str] = []
        head = f"{'素材':<28}{'集合':<10}{'引擎':<12}{'CER':>7}{'RTF':>7}  识别结果"
        out.append(head)
        out.append("-" * 96)
        for case in self.cases:
            flag = ""
            if case.set_name == "wake":
                flag = "唤醒√" if case.woke else "★没醒★"
            elif case.set_name == "negative" and case.false_wake:
                flag = "★误唤醒★"
            label = f"{case.group}/{case.name}" if case.group else case.name
            out.append(f"{label[:27]:<28}{case.set_name:<10}{case.engine[:11]:<12}"
                       f"{case.cer:>7.3f}{case.rtf:>7.3f}  {case.hyp[:36]} {flag}")
        return out


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #
def load_cases(root: str | Path, *, sets: list[str] | None = None) -> list[Case]:
    """读 `root/**/*.wav`（同名 `.txt` 是参考文本）。

    ★允许嵌套★：`data/asr_eval/wake/` 是真机录音，`data/asr_eval/synth/wake/`
    是 TTS 合成的同名集合 —— 集合名取**所在目录名**，前缀进 ``group``，
    这样 `--set wake` 会两套都跑、`--set synth` 只跑合成那套。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    wanted = [str(s) for s in sets] if sets else []
    out: list[Case] = []
    for wav in sorted(base.rglob("*.wav")):
        rel = wav.relative_to(base)
        if len(rel.parts) < 2:
            continue                              # 直接摆在根目录的不算（没有集合）
        set_name = rel.parts[-2]
        group = "/".join(rel.parts[:-1])
        if wanted and not any(s in set_name or s in group for s in wanted):
            continue
        ref_path = wav.with_suffix(".txt")
        ref = ""
        if ref_path.is_file():
            ref = ref_path.read_text(encoding="utf-8", errors="replace").strip()
        out.append(Case(name=wav.stem, wav=wav, ref=ref, set_name=set_name, group=group))
    return out


def synthesize(settings: Any, texts: list[str], out_dir: str | Path, *,
               set_name: str = "synth", logger=None, prefix: str = "") -> list[Case]:
    """用本地 TTS 把一串文本合成成评测素材（零人工，覆盖广）。

    ★注意★：合成音没有口音/噪声/麦克风频响，所以它只能证明"词库和模型对得上"，
    不能替代真机录音 —— 指标要两套分开看（脚本里会标 `synth/` 前缀）。
    """
    from ..audio import save_wav  # noqa: PLC0415
    from ..tts import create_tts  # noqa: PLC0415

    room = Path(out_dir) / set_name
    room.mkdir(parents=True, exist_ok=True)
    tts = create_tts(settings, logger)
    out: list[Case] = []
    try:
        for index, text in enumerate(texts, 1):
            clean = str(text or "").strip()
            if not clean:
                continue
            name = f"{prefix}{index:03d}_{clean[:10]}"
            wav = room / f"{name}.wav"
            if wav.is_file() and wav.with_suffix(".txt").is_file():
                out.append(Case(name=name, wav=wav,
                                ref=wav.with_suffix(".txt").read_text(encoding="utf-8").strip(),
                                set_name=set_name))
                continue
            try:
                chunks = list(tts.synth(clean))
            except Exception as exc:  # noqa: BLE001 - 一条合成失败不该中断整批
                if logger is not None:
                    logger.warning(f"[评测] 合成失败（{clean[:12]}）：{exc}")
                continue
            if not chunks:
                continue
            rate = int(chunks[0][0])
            pcm = np.concatenate([np.asarray(data) for _, data in chunks])
            save_wav(wav, pcm, rate)
            wav.with_suffix(".txt").write_text(clean + "\n", encoding="utf-8")
            out.append(Case(name=name, wav=wav, ref=clean, set_name=set_name))
    finally:
        close = getattr(tts, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return out


# --------------------------------------------------------------------------- #
# 跑评测
# --------------------------------------------------------------------------- #
def run(settings: Any, cases: list[Case], *, hearing=None, wake=None, kws=None,
        prefer: str = "", logger=None) -> Report:
    """把素材跑一遍，返回报告。

    ``hearing`` 可以传进来复用（自测会传假引擎）；不给就按配置自己建一个。
    """
    from ..audio import load_wav  # noqa: PLC0415

    report = Report()
    own = None
    if hearing is None:
        from . import Hearing  # noqa: PLC0415

        own = hearing = Hearing(settings, logger, preload=False)
    try:
        for case in cases:
            item = CaseResult(name=case.name, set_name=case.set_name, ref=case.ref,
                              group=case.group)
            try:
                audio, rate = load_wav(case.wav, 16000)
                if audio.size == 0:
                    item.note = "空音频"
                    report.errors.append(f"{case.name}：空音频")
                    report.cases.append(item)
                    continue
                t0 = time.perf_counter()
                result = hearing.transcribe(np.ascontiguousarray(audio), int(rate), prefer=prefer)
                item.latency = time.perf_counter() - t0
                item.hyp = str(getattr(result, "text", "") or "")
                item.engine = str(getattr(result, "engine", "") or "")
                item.rtf = float(getattr(result, "rtf", 0.0) or 0.0)
                item.cer = cer(case.ref, item.hyp) if case.ref else 0.0
                if case.set_name == "wake":
                    item.woke = _woke(wake, item.hyp, kws, audio, rate)
                elif case.set_name == "negative":
                    item.false_wake = _woke(wake, item.hyp, kws, audio, rate)
            except Exception as exc:  # noqa: BLE001 - 一条素材坏了不该中断整轮
                item.note = f"{type(exc).__name__}: {exc}"
                report.errors.append(f"{case.name}：{item.note}")
            report.cases.append(item)
    finally:
        if own is not None:
            own.close()
    return report


def _woke(wake, text: str, kws, audio, rate: int) -> bool:
    """这条素材"算不算唤醒"：KWS 命中 或 文本匹配命中（两条路都认）。"""
    if kws is not None:
        try:
            if kws.detect(audio, rate):
                return True
        except Exception:  # noqa: BLE001 - KWS 出错就退回文本判定
            pass
    if wake is None or not text:
        return False
    try:
        return wake.match(text) is not None
    except Exception:  # noqa: BLE001
        return False


__all__ = ["Case", "CaseResult", "Report", "SETS", "cer", "edit_distance", "load_cases",
           "normalize_text", "run", "synthesize"]
