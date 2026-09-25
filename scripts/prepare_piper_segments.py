"""给 Piper 微调准备**切短**的数据集：长独白 → 一句一条（22050 Hz + ``metadata.csv``）。

为什么要切
----------
这是第 29 节体检出来的结论。老的 30 条素材平均 **9.4 秒**、最长 25 秒，
而每个 epoch 只有 4 步（batch 8）——训练 60 epoch 里，loss 在前 20 epoch 就降到底、
之后 40 epoch 原地抖，三个 checkpoint 一样坏。VITS 的时长预测器最怕的正是这种
「0.9 秒和 25 秒混在一起」的分布，而 **Piper 的配方吃的本来就是短句**。

实测：VAD 能把 283 秒切成 128 段（4.3×，段均 1.5 秒）→ 每 epoch 从 4 步升到 16 步。

★难点不在切，在**给每段配文本**★
------------------------------
清单里的标点不规整（`交谈2` 73 个字里**一个句号都没有**），所以「按标点切文本 + 按下标配对」
必然错位。这里的做法是：

  1. VAD 切音频（用**帧级语音掩码**自己分组，不用 segmenter 的返回值 ——
     它一次只吐一段，位置会串位）；
  2. 每段用**本地 ASR**（SenseVoice）转写；
  3. 把「各段转写拼起来」与**原文**做字符级对齐（``difflib``），
     再把每段的范围映射回原文 → **标签用原文**。

第 3 步是关键：ASR 会把 `Mon3tr` 听成 `monster`、把「迎敌」听成「营敌」，
直接拿转写当标签就是把错字教进音素表；映射回原文就既拿到了正确的句子边界、
又保住了专名的正确写法。

用法（默认试运行）：

    python scripts/prepare_piper_segments.py --dir data/personas/kaltsit --out data/piper/kaltsit
    python scripts/prepare_piper_segments.py --dir data/personas/kaltsit --out data/piper/kaltsit --apply

    # 然后（在 .venv-piper 里）
    python -m piper_train.preprocess --language cmn --sample-rate 22050 ^
        --input-dir data/piper/kaltsit --output-dir data/piper/kaltsit/training ^
        --dataset-format ljspeech --single-speaker --max-workers 4
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_tts_dataset import lookup, resolve_texts  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.tts import textcheck  # noqa: E402
from voice_loop.tts.pacing import trim_silence  # noqa: E402

TARGET_RATE = 22050      # ★必须和底模 zh_CN-huayan-medium 一致★

# ★踩过★：第一版把这段直接写成字面量，里面还带着「中文引号」—— 我实际敲进去的是 ASCII 引号，
# 于是字符串在中间被**截断**（后面的片段不再是 raw string，`\[` 变成非法转义，
# 而且 ASCII 引号反而漏出了字符类）。改成用 ``re.escape`` 拼，别再手写字面量。
_MARKS = "，。！？、；：\u201c\u201d\u2018\u2019（）《》…—－·,.!?;:'\"()[]<>~`-"
MARKS = set(_MARKS)
MARKS_RE = re.compile(r"[\s" + re.escape(_MARKS) + "]+")


def strip_marks(text: str) -> str:
    """去掉标点与空白，只用于**对齐**（标点在 cmn 声线里本来也不变成音素）。"""
    return MARKS_RE.sub("", text)


def label_bounds(original: str, seconds: list[float], window: int = 3) -> list[str]:
    """把原文按**每段音频的时长比例**切成连续区间，并让切点尽量落在标点后面。

    ★为什么不能用转写的字数当切点★（踩过）：转写会漏字（`Mon3tr`→`monster`、
    「我会」→「我」），按它的字数累加定位，切点会漂到词中间 —— 实测出现过
    「博士我」「会大」这种半个词的标签，等于给音频配了错文本。

    改用两条更稳的依据：
      1. **时长比例**：音频是「一口气按顺序念完」的，某个人的语速基本恒定，
         所以「这段音频占总时长的比例」≈「这段文本占字数的比例」。比转写可靠得多。
      2. **标点吸附**：切点落在标点后面最自然（一句话一条）。在 ±window 个字以内
         找标点，找不到才用比例位置。

    返回的标签拼起来严格等于原文（切点是把原文切成连续区间），可以断言。
    """
    body = " ".join(original.split())
    content = [i for i, ch in enumerate(body) if ch not in MARKS and not ch.isspace()]
    total = float(sum(seconds)) or 1.0
    count = len(content)
    if count == 0:
        return ["" for _ in seconds]

    bounds = [0]
    cumulative = 0.0
    for seconds_i in seconds[:-1]:
        cumulative += seconds_i
        target = max(0, min(count - 1, int(round(count * cumulative / total))))
        snapped = None
        for delta in range(0, window + 1):
            for candidate in (target - delta, target + delta):
                if 0 < candidate < count and body[content[candidate] - 1] in MARKS:
                    snapped = candidate
                    break
            if snapped is not None:
                break
        if snapped is not None:
            target = snapped
        bounds.append(content[target])
    bounds.append(len(body))

    # 单调化（比例位置天然单调，这里只是防御）
    for index in range(1, len(bounds)):
        bounds[index] = min(max(bounds[index], bounds[index - 1]), len(body))
    return [body[bounds[i] : bounds[i + 1]] for i in range(len(seconds))]


def read_mono(path: Path, rate: int) -> np.ndarray:
    """读成目标采样率单声道 float32（多相重采样）。"""
    from math import gcd  # noqa: PLC0415

    samples, src = sf.read(str(path), dtype="float32", always_2d=False)
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim > 1:
        x = x[:, 0]
    if src != rate:
        g = gcd(int(src), rate)
        x = resample_poly(x, rate // g, int(src) // g).astype(np.float32)
    return x


def speech_mask(x16: np.ndarray, settings) -> np.ndarray:
    """逐帧的语音/非语音布尔掩码（用它自己分组，位置才准）。

    ★为什么不直接用 segmenter 的返回值★：`accept()` 一次只吐一段，
    一次调用里产生两段时，第二段要等下次调用才吐出来 —— 那时「已消耗样本数」已经往后跑了，
    位置就串了。改用 `speech_detected` 取逐帧状态，边界由我们自己算。
    """
    from voice_loop.audio import make_segmenter  # noqa: PLC0415

    frame = int(settings.audio.frame_size)
    seg = make_segmenter(settings)
    flags: list[bool] = []
    for start in range(0, x16.size, frame):
        block = x16[start : start + frame]
        if block.size < frame:
            block = np.pad(block, (0, frame - block.size))
        seg.accept(block)          # 返回值丢掉：只用它的内部状态
        flags.append(bool(seg.speech_detected))
    return np.asarray(flags, dtype=bool)


def mask_to_regions(mask, frame: int, min_silence_s: float,
                    max_seconds: float, min_seconds: float) -> list[tuple[int, int]]:
    """把帧掩码变成样本区间：句内小停顿合并、超长段按最长停顿再切。"""
    mask = np.asarray(mask, dtype=bool)      # 测试里会直接传 list，这里统一一下
    if mask.size == 0:
        return []
    gap_need = int(round(min_silence_s * 16000 / frame))

    regions: list[list[int]] = []
    start: int | None = None
    gap = 0
    for index, flag in enumerate(mask):
        if flag:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= gap_need:
                regions.append([start, index - gap + 1])
                start = None
                gap = 0
    if start is not None:
        regions.append([start, int(mask.size)])

    max_frames = int(round(max_seconds * 16000 / frame))
    out: list[tuple[int, int]] = []
    for lo, hi in regions:
        if hi - lo <= max_frames:
            out.append((lo * frame, hi * frame))
            continue
        # 超长：在内部找最长的静音处切（找不到就平均切）
        pieces = [(lo, hi)]
        while pieces:
            a, b = pieces.pop(0)
            if b - a <= max_frames:
                out.append((a * frame, b * frame))
                continue
            window = mask[a + max_frames // 3 : b - max_frames // 3]
            quiet = np.flatnonzero(~window)
            cut = (a + max_frames // 3 + int(quiet[0] + quiet.size / 2)) if quiet.size else (a + (b - a) // 2)
            pieces.insert(0, (cut, b))
            pieces.insert(0, (a, cut))

    min_samples = int(min_seconds * 16000)
    return [(a, b) for a, b in out if b - a >= min_samples]


def main() -> int:
    ap = argparse.ArgumentParser(description="把长独白切成短句，给 Piper 微调用")
    ap.add_argument("--dir", required=True, help="角色素材目录（含 wav + 清单 txt）")
    ap.add_argument("--out", required=True, help="输出目录（写 metadata.csv + wav/）")
    ap.add_argument("--manifest", default="", help="清单 txt（默认用源目录里的 .txt）")
    ap.add_argument("--min-silence", type=float, default=0.35, help="判一句结束的静音时长（默认 0.35s）")
    ap.add_argument("--min-seconds", type=float, default=0.6, help="短于这个的片段丢掉")
    ap.add_argument("--max-seconds", type=float, default=12.0, help="长于这个的强制再切")
    ap.add_argument("--min-chars", type=int, default=4,
                    help="标签短于这么多字的段丢掉（比例切点会切出「Mo」这种碎片）")
    ap.add_argument("--lead-ms", type=int, default=80, help="段首留白")
    ap.add_argument("--tail-ms", type=int, default=120, help="段尾留白")
    ap.add_argument("--apply", action="store_true", help="真写文件（默认只试运行）")
    ap.add_argument("--force", action="store_true", help="输出目录非空时覆盖")
    args = ap.parse_args()

    src = Path(args.dir).resolve()
    if not src.is_dir():
        raise SystemExit(f"源目录不存在：{src}")
    wavs = sorted(src.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"{src} 里没有 wav")
    texts = resolve_texts(src, Path(args.manifest).resolve() if args.manifest else None)

    out = Path(args.out).resolve()
    wav_dir = out / "wav"
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} 非空（加 --force 才覆盖）")

    settings = load_settings(ROOT / "config.toml")
    from voice_loop.asr import AsrRouter  # noqa: PLC0415

    asr = AsrRouter(settings, logging.getLogger("segments"))
    vad_rate = int(settings.audio.sample_rate)

    rows: list[tuple[str, str]] = []
    report: list[tuple[str, int, int, float, float, int]] = []
    unusable: list[tuple[str, str]] = []
    shaky: list[tuple[str, int, int]] = []
    total_seconds = 0.0

    for wav in wavs:
        text = lookup(texts, wav)
        if not text.strip():
            unusable.append((wav.name, "清单里没有对应文本"))
            continue
        x = read_mono(wav, TARGET_RATE)
        x16 = read_mono(wav, vad_rate)
        mask = speech_mask(x16, settings)
        # ★先切出**全部**片段再标文本，最后才筛★（踩过，很隐蔽）：
        # 第一版把 `min_seconds` 交给 `mask_to_regions`，于是短片段在**标文本之前**就被删了，
        # 而标签是按「留下来的片段各自时长占比」分配全文的 —— 结果每个留下来的片段
        # 都分到了被删片段的那份文本：`--min-seconds 2.0` 那版的中位「帧数/音素 id」
        # 只有 **0.72**（正确口径应该 ~1.9），也就是说**文本比音频长了 2.65 倍**。
        # 这种错误不会报错、只会让模型学歪，所以改成「标完再筛」。
        spans = mask_to_regions(mask, int(settings.audio.frame_size), args.min_silence,
                               args.max_seconds, min_seconds=0.0)
        if not spans:
            unusable.append((wav.name, "VAD 没切出任何片段"))
            continue

        lead = int(args.lead_ms * vad_rate / 1000)
        tail = int(args.tail_ms * vad_rate / 1000)
        pieces: list[tuple[np.ndarray, str]] = []
        heard_all: list[str] = []
        for index, (lo, hi) in enumerate(spans, start=1):
            lo = max(0, lo - lead)
            hi = min(x16.size, hi + tail)
            clip16 = x16[lo:hi]
            heard = asr.transcribe(clip16, vad_rate, prefer="sensevoice").text.strip()
            heard_all.append(heard)
            # 22k 时间轴与 16k 完全对齐（重采样不改时长）
            a = int(lo * TARGET_RATE / vad_rate)
            b = int(hi * TARGET_RATE / vad_rate)
            clip = (np.clip(x[a:b], -1.0, 1.0) * 32767.0).astype(np.int16)
            clip = trim_silence(clip, TARGET_RATE, lead_ms=30, tail_ms=50)
            pieces.append((clip, heard))

        seconds_list = [clip.size / TARGET_RATE for clip, _heard in pieces]
        labels = label_bounds(text, seconds_list)
        # 质量指标：每段的**标签与它自己那段音频的转写**有多像。
        # 切点漂到词中间时这个数会显著下降，所以它比“整条文件的相似度”灵敏得多。
        matches = [textcheck.similarity(label, heard)
                   for label, (_clip, heard) in zip(labels, pieces) if heard]
        quality = float(np.mean(matches)) if matches else 0.0
        poor = sum(1 for m in matches if m < 0.5)
        # ★不变量★：标签拼起来就该是原文（一个字符不差）。不成立说明切分有洞，要停下来看
        if "".join(labels) != " ".join(text.split()):
            shaky.append((wav.name, len("".join(labels)), len(" ".join(text.split()))))
        kept = 0
        dropped = 0
        kept_seconds = 0.0
        for index, ((clip, heard), label) in enumerate(zip(pieces, labels), start=1):
            seconds = clip.size / TARGET_RATE
            # 筛选放在**标完文本之后**：这样被丢掉的片段连同它那一段文本一起丢，
            # 不会把文本摊到别人身上（见上面 mask_to_regions 那段的说明）。
            if seconds < args.min_seconds:
                dropped += 1
                continue
            # 空转写 = 这段里没人说话（咳啦/翻页/呼吸）；这时切点不前进、标签也是空的，
            # 两边一起丢掉，正好自洽。
            if not heard or not label.strip():
                dropped += 1
                continue
            # 「Mo」这种碎片：比例切点不知道 `Mon3tr` 是一个词，会把它切成两半。
            # 短标签对训练没价值（还会把音素表往怪方向拉），直接丢掉。
            if len(strip_marks(label)) < args.min_chars:
                dropped += 1
                continue
            stem = f"{wav.stem}_{index:02d}"
            rows.append((stem, " ".join(label.split())))
            kept += 1
            kept_seconds += seconds
            total_seconds += seconds
            if args.apply:
                wav_dir.mkdir(parents=True, exist_ok=True)
                sf.write(str(wav_dir / f"{stem}.wav"), clip, TARGET_RATE, subtype="PCM_16")
        report.append((wav.name, len(text), kept, kept_seconds, quality, len(spans), dropped, poor))

    if not rows:
        raise SystemExit("没有可用的（音频, 文本）对，什么都没写")

    print(f"源目录 {src}")
    print(f"{'文件':<28}{'字数':>6}{'切出':>6}{'采用':>6}{'丢弃':>6}{'秒':>8}{'段均':>7}{'标签↔音频':>10}{'可疑':>6}")
    print("-" * 92)
    for name, chars, kept, seconds, quality, found, dropped, poor in sorted(report, key=lambda r: -r[3]):
        avg = seconds / kept if kept else 0.0
        print(f"{name[:27]:<28}{chars:>6}{found:>6}{kept:>6}{dropped:>6}{seconds:>7.1f}s{avg:>6.2f}s"
              f"{quality:>10.2f}{poor:>6}")
    for name, why in unusable:
        print(f"{name[:27]:<28} 跳过：{why}")

    print()
    print(f"共 {len(rows)} 段 / {total_seconds:.1f} 秒（{total_seconds / 60:.1f} 分钟）"
          f"，段均 {total_seconds / max(1, len(rows)):.2f} 秒")
    if shaky:
        print("★标签没覆盖住原文（要对齐有洞的这几条）★")
        for name, got, want in shaky:
            print(f"   {name}：标签 {got} 字 / 原文 {want} 字")
    else:
        print("✓ 不变量成立：各段标签拼起来 == 原文（一个字符不差）")
    if args.apply:
        out.mkdir(parents=True, exist_ok=True)
        (out / "metadata.csv").write_text(
            "".join(f"wav/{stem}.wav|{text}\n" for stem, text in rows), encoding="utf-8"
        )
        print(f"\n√ 写好 {out / 'metadata.csv'}（{len(rows)} 行）+ {len(rows)} 个 wav @ {TARGET_RATE}Hz")
        rel = out.relative_to(ROOT)
        print("  下一步（在 .venv-piper 里）：")
        print(f"  python -m piper_train.preprocess --language cmn --sample-rate {TARGET_RATE} "
              f"--input-dir {rel} --output-dir {rel}/training --dataset-format ljspeech "
              f"--single-speaker --max-workers 4")
    else:
        print("\n（试运行：没写文件。加 --apply 真写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
