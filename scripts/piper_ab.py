"""Piper 专属声线（路线 C）的验收：沙沙声 / 语速与延迟 / 与 ZipVoice 对着听。

这是日志第 23 节写的「验收标准」① ② ③ 的具体实现：

    ① 沙沙声（高频那一层的形状）—— 复用 ab_clone_model 的尺子，别另写一套
    ② 同一批文本和 ZipVoice 对着听 —— 同目录产出 wav，人耳判断
    ③ RTF 与首声延迟 —— 决定「能不能替换现在的 TTS」
    ④ 顺带用本地 ASR **回听**一遍：确认微调后的声音没把字吐丢（拿 textcheck 比）

    python scripts/piper_ab.py                      # 用默认的一批文本 + 三个声音
    python scripts/piper_ab.py --text "我在，博士。" --only piper-ft
    python scripts/piper_ab.py --out sessions/piper_ab --no-asr

★两个必须记牢的坑（都踩过）★：

    - 引擎契约是 **int16 PCM**（见 voice_loop/tts/base.py）。把它当 [-1,1] 浮点再乘
      32767 会把整条音频**削平**（RMS 0.98 = 满幅方波），频谱全是谐波、数字全是垃圾。
      所以这里一律原样传 int16。
    - Piper 输出 22.05 kHz（Nyquist 11.025 kHz），**10–12 kHz 那个频段有一半在 Nyquist
      之外**，ZipVoice 是 24 kHz（Nyquist 12 kHz）。所以频段一律**按名字取**，
      碰到超出 Nyquist 的段标 N/A，不能拿 bands[-1] 当「10–12k」——之前就标错过。
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ab_clone_model as ruler  # noqa: E402  （专门用来量沙沙声的那把尺子）
import soundfile as sf  # noqa: E402
from voice_loop.settings import Settings, load_settings  # noqa: E402

DEFAULT_TEXTS = [
    "我在，博士。今天的日程已经排好了，上午九点是机器学习课。",
    "下午两点还有组会，材料我都放在桌面上了。",
    "别熬夜了，先去休息吧。",
]
SAMPLE_RATE_OUT = 22050
WRITE_RATE = None          # 保存时用各自的原始采样率（不重采样，免得把沙沙声洗掉）


def make_settings(base: Settings, **tts_overrides) -> Settings:
    """复制一份 settings 再改 TTS（★不能就地改★：同一个进程里要连着跑三个声音）。"""
    clone = copy.deepcopy(base)
    for key, value in tts_overrides.items():
        setattr(clone.tts, key, value)
    return clone


def piper_engine(settings: Settings, model: Path):
    from voice_loop.tts.piper_tts import PiperTts  # noqa: PLC0415

    cfg = make_settings(
        settings,
        backend="piper",
        model=str(model.relative_to(settings.root).as_posix()),
        config=str(model.with_suffix(model.suffix + ".json").relative_to(settings.root).as_posix()),
    )
    return PiperTts(cfg)


def zipvoice_engine(settings: Settings, ref: Path):
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts  # noqa: PLC0415

    cfg = make_settings(settings, backend="zipvoice", clone_audio=str(
        ref.relative_to(settings.root).as_posix()))
    engine = ZipVoiceTts(cfg)
    engine.set_reference(cfg.resolve(cfg.tts.clone_audio), "")
    return engine


def to_int16(pcm) -> np.ndarray:
    """统一成 int16。★引擎给的本来就是 int16★，只有测试用的假数据才是浮点。"""
    x = np.asarray(pcm)
    if x.dtype == np.int16:
        return x
    x = x.astype(np.float64)
    if np.max(np.abs(x), initial=0.0) <= 1.5:        # 看起来是 [-1,1] 浮点
        x = x * 32767.0
    return np.clip(x, -32768, 32767).astype(np.int16)


def synth(engine, text: str) -> tuple[int, np.ndarray, float, float]:
    """合一句，返回 (采样率, int16 音频, 总耗时, 首声耗时)。"""
    t0 = time.perf_counter()
    first = None
    parts: list[np.ndarray] = []
    rate = 0
    for r, chunk in engine.synth(text):
        if first is None:
            first = time.perf_counter() - t0
        rate = r
        parts.append(to_int16(chunk))
    total = time.perf_counter() - t0
    audio = np.concatenate(parts) if parts else np.zeros(1, dtype=np.int16)
    return rate, audio, total, (first if first is not None else total)


def band_names(rate: int) -> list[tuple[str, bool]]:
    """频段名 + 该段是不是在 Nyquist 之内（Piper 22.05k 的 10–12k 只到 11.025k）。"""
    nyq = rate / 2.0
    out = []
    for lo, hi in zip(ruler.BAND_EDGES[:-1], ruler.BAND_EDGES[1:]):
        label = f"{lo // 1000}-{hi // 1000}k" if hi < 24000 else f"{lo // 1000}k+"
        out.append((label, lo < nyq))
    return out


def measure(pcm16: np.ndarray, rate: int, seconds: float, first: float) -> dict:
    """把要看的几项一次量出来（尺子全来自 ab_clone_model）。pcm 必须是 int16。"""
    audio_s = pcm16.size / float(rate)
    bands = ruler.hf_band_profile(pcm16, rate)
    names = band_names(rate)
    got = {name: (value if usable else float("nan")) for (name, usable), value in zip(names, bands)}
    f = pcm16.astype(np.float64) / 32768.0
    quiet_hf, quiet_pct = ruler.audible_quiet_hf(pcm16, rate)
    return {
        "audio_s": audio_s,
        "synth_s": seconds,
        "rtf": seconds / audio_s if audio_s else 0.0,
        "first_s": first,
        "f0": _f0(f, rate),
        "peak": float(np.max(np.abs(f), initial=0.0)),
        "rms": float(np.sqrt((f ** 2).mean())),
        # ★沙沙声看这两列★：安静帧（停顿/气口）里高频占多少。ZipVoice 的沙沙声就在那层。
        "quiet_hf_pct": quiet_hf,
        "quiet_share_pct": quiet_pct,
        "floor_db": ruler.floor_db(pcm16, rate),
        "gaps": len(ruler.gaps_ms(pcm16, rate)),
        "bands": got,
    }


def _f0(pcm: np.ndarray, rate: int) -> float:
    """顺手量一下音区（跟 ZipVoice 的守卫用同一把尺子，可比）。"""
    try:
        from voice_loop.tts import pitch as pitchkit  # noqa: PLC0415

        return pitchkit.f0_median(pcm.astype(np.float32), rate) or 0.0
    except Exception:  # noqa: BLE001 - 量不到不影响其它项
        return 0.0


def _asr_check(rows: list[dict], out_dir: Path) -> None:
    """用本地 ASR 回听一遍，跟要念的文本比——**查吞字**（跟 ZipVoice 那条文本守卫同一个尺子）。

    为什么要查：路线 C 的动机之一就是「ZipVoice 偶尔吞字」，那微调出来的 Piper
    如果吞得更狠，快也没用。这里用 `voice_loop.tts.textcheck.similarity`（已经把
    中文数字归一过，正常合成应该 = 1.000）。
    """
    try:
        from voice_loop.settings import load_settings  # noqa: PLC0415
        from voice_loop.asr import AsrRouter  # noqa: PLC0415
        from voice_loop.tts import textcheck  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ASR] 跳过回听（导入失败：{exc}）")
        return
    import numpy as np  # noqa: PLC0415

    print("\n" + "=" * 96)
    print(" ASR 回听：微调后的声音有没有把字吐丢（相似度 = 1.000 表示字字对得上）")
    print("=" * 96)
    settings = load_settings(ROOT / "config.toml")
    try:
        asr = AsrRouter(settings, __import__("logging").getLogger("piper-ab"))
    except Exception as exc:  # noqa: BLE001 - 没装/没下模型就跳过
        print(f"[ASR] 跳过回听（ASR 起不来：{type(exc).__name__}: {exc}）")
        return
    print(f"{'文件':<18}{'相似度':>8}  {'听到的（前 28 字）'}")
    for row in rows:
        path = out_dir / row["path"]
        try:
            pcm, rate = sf.read(str(path), dtype="float32")
            if rate != 16000:                      # ASR 只吃 16 kHz
                pcm = _resample(pcm, rate, 16000)
            heard = asr.transcribe(pcm, 16000, prefer="sensevoice").text
        except Exception as exc:  # noqa: BLE001
            print(f"{row['path']:<18}{'失败':>8}  {type(exc).__name__}: {exc}")
            continue
        ratio = textcheck.similarity(row["text"], heard)
        flag = "" if ratio >= 0.99 else "  ★可能吞字★"
        print(f"{row['path']:<18}{ratio:>8.3f}  {heard[:28]}{flag}")


def _resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """线性插值重采样（回听用足够了：ASR 对这点失真不敏感）。"""
    import numpy as np  # noqa: PLC0415

    n = int(round(pcm.size * dst / float(src)))
    if n <= 1:
        return pcm.astype("float32")
    x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, pcm).astype("float32")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Piper 微调声线验收（沙沙声 / RTF / 试听对照）")
    ap.add_argument("--text", action="append", default=None, help="要念的句子（可多次）")
    ap.add_argument("--out", default="sessions/piper_ab", help="产物目录")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--piper-model", default="models/tts/piper/kaltsit-finetune.onnx")
    ap.add_argument("--piper-base", default="models/tts/piper/zh_CN-huayan-medium.onnx")
    ap.add_argument("--ref", default="data/personas/kaltsit/干员报到.wav", help="ZipVoice 参考音")
    ap.add_argument("--only", choices=["piper", "piper-ft", "piper-base", "zipvoice"],
                    help="只跑一个声音（默认三个都跑）")
    ap.add_argument("--label", default="piper-ft",
                    help="--only piper-ft 时这个声音叫什么（产物文件名用它，方便比多份微调权重）")
    ap.add_argument("--no-asr", action="store_true", help="不回听核对文本（省时间）")
    args = ap.parse_args(argv)

    settings = load_settings(ROOT / args.config)
    texts = args.text or DEFAULT_TEXTS
    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    voices: list[tuple[str, object]] = []
    ft = settings.resolve(args.piper_model)
    base = settings.resolve(args.piper_base)
    ref = settings.resolve(args.ref)
    if args.only in (None, "piper", "piper-ft"):
        if ft.exists():
            voices.append((args.label, piper_engine(settings, ft)))
        else:
            print(f"[跳过] 微调声线不在：{ft}（先跑 scripts/export_piper_onnx.py）")
    if args.only in (None, "piper", "piper-base"):
        if base.exists():
            voices.append(("piper-base", piper_engine(settings, base)))
        else:
            print(f"[跳过] 出厂 Piper 不在：{base}")
    if args.only in (None, "zipvoice"):
        if ref.exists():
            voices.append(("zipvoice", zipvoice_engine(settings, ref)))
        else:
            print(f"[跳过] ZipVoice 参考音不在：{ref}")
    if not voices:
        print("没有可用的声音，什么都没做")
        return 2

    rows: list[dict] = []
    print("=" * 96)
    print(f" 口径：{len(voices)} 个声音 × {len(texts)} 句 → {out_dir}")
    print("=" * 96)
    for name, engine in voices:
        rate_hint = ""
        # 预热一次（Piper 第一次合成要建 phonemizer，不计入 RTF，但首声延迟单独也算一次）
        try:
            synth(engine, "预热。")
            rate_hint = f"{engine.sample_rate} Hz"
        except Exception as exc:  # noqa: BLE001 - 一个声音坏了不该挡住其它
            print(f"[{name}] 预热失败：{type(exc).__name__}: {exc}")
            continue
        for index, text in enumerate(texts, start=1):
            try:
                rate, pcm, seconds, first = synth(engine, text)
            except Exception as exc:  # noqa: BLE001
                print(f"[{name}] 第 {index} 句失败：{type(exc).__name__}: {exc}")
                continue
            path = out_dir / f"{name}_{index}.wav"
            # ★原样存 int16★：各自保留自己的采样率（重采样会把高频那层抹平，量不准），
            # 也不做任何增益调整（削平过一次，不能再犯）。
            sf.write(path, pcm, rate, subtype="PCM_16")
            m = measure(pcm, rate, seconds, first)
            m.update(voice=name, text=text, path=path.name, rate=rate)
            rows.append(m)
            clip = "★削波★" if m["peak"] > 0.999 else ""
            print(f"[{name}] {index}. {text[:20]}… → {path.name}"
                  f"  {m['audio_s']:.2f}s  合成 {seconds:.2f}s  RTF {m['rtf']:.2f}"
                  f"  首声 {first:.2f}s  F0 {m['f0']:.0f}Hz  峰值 {m['peak']:.2f}{clip}")
        print(f"  （{name} 采样率 {rate_hint}）")

    if not rows:
        return 1

    print("\n" + "=" * 96)
    print(" 每个声音的平均值（★沙沙声看「安静帧高频」：停顿里高频占比越低越好★）")
    print("=" * 96)
    print(f"{'声音':<12}{'RTF':>7}{'首声':>8}{'时长':>8}{'F0':>7}{'安静帧高频':>11}{'底噪':>8}{'峰值':>7}")
    for name, _engine in voices:
        got = [r for r in rows if r["voice"] == name]
        if not got:
            continue
        avg = lambda k: sum(r[k] for r in got) / len(got)  # noqa: E731
        q = avg("quiet_hf_pct")
        print(f"{name:<12}{avg('rtf'):>7.2f}{avg('first_s'):>8.2f}{avg('audio_s'):>8.2f}"
              f"{avg('f0'):>7.0f}{(f'{q:.1f}%' if q == q else 'N/A'):>11}"
              f"{avg('floor_db'):>8.0f}{avg('peak'):>7.2f}")

    print("\n 各频段（相对 0.3-3 kHz，只在有声帧上算；超出 Nyquist 的标 N/A）")
    header = None
    for name, _engine in voices:
        got = [r for r in rows if r["voice"] == name]
        if not got:
            continue
        keys = list(got[0]["bands"].keys())
        if header is None:
            header = keys
            print(f"{'声音':<12}" + "".join(f"{k:>10}" for k in keys))
        cells = []
        for k in keys:
            vals = [r["bands"][k] for r in got if r["bands"].get(k) == r["bands"].get(k)]
            cells.append(f"{sum(vals) / len(vals):>+10.1f}" if vals else "N/A".rjust(10))
        print(f"{name:<12}" + "".join(cells))

    if not args.no_asr:
        _asr_check(rows, out_dir)

    notes = [
        "这份是「路线 C（Piper 专属声线）」的验收产物，由 scripts/piper_ab.py 生成。",
        "",
        "听的时候建议按顺序：",
        "  piper-ft_1 / piper-base_1 / zipvoice_1  ——  同一句话的三个声音（切换对比）",
        "  再听同一声音的 2、3 句，看是否稳定。",
        "",
        "关注点：① 有没有沙沙声（zipvoice 那组是当前的部署，用户说它有沙沙声）",
        "        ② 像不像凯尔希（微调只用了 30 条素材 / 283 秒，别期待过高）",
        "        ③ 语速与停顿听起来赶不赶（Piper 的节奏由 length_scale 控制，不是参考音带的）",
        "",
        "客观数字看上面两张表：",
        "  · RTF = 合成耗时 / 音频时长（越小越快）；首声 = 从开口到第一个音频块（交互感看它）",
        "  · 安静帧高频 % = 停顿/气口里 6 kHz 以上的能量占比 —— **沙沙声就在这一列**（越低越好）",
        "  · 各频段 = 有声帧里各频段相对 0.3-3 kHz 的电平：自然语音是一路滚降，沙沙声会让高段平掉",
        "    （Piper 22.05 kHz 的 Nyquist 只到 11 kHz，所以 10-12k / 12k+ 对它天生就是 N/A）",
        "  · ASR 回听相似度 = 1.000 表示没吞字（< 0.99 要留神）",
    ]
    (out_dir / "说明.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    print(f"\n试听说明已写：{out_dir / '说明.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
