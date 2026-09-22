"""ZipVoice 零样本音色克隆（sherpa-onnx，纯 CPU / onnxruntime）。

Piper 是「一个模型一种音色」，ZipVoice 是**零样本克隆**：
给它一小段参考音频（以及这段音频的逐字文本），它就用那个音色读你给的中文。

    tts = ZipVoiceTts(settings)
    tts.set_reference("data/personas/kalsit/问候.wav", "")   # 文本留空 = 自动转写
    for rate, pcm in tts.synth("我在，博士。"):
        ...

三条硬约束（sherpa-onnx 官方文档明确写了，实测也如此）：
    1. 参考音频和参考文本**必须逐字一致**，不一致音色会明显退化；
    2. 参考音频要单人、干净（无背景音乐/音效），5~15 秒最稳；
    3. 它是**离线（非流式）**模型：一次调用生成一整段，所以延迟看单句长度。

CPU 上是「以时间换音色」，RTF 通常大于 1（Piper 是 0.04）。
赶时间就用 piper，想要音色就用它——两条路都在，见 README 第 14 节。

模型与用法：https://k2-fsa.github.io/sherpa/onnx/tts/zipvoice.html
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from ..manifest import manifest_pairs
from ..settings import Settings
from .pacing import PacingFixer

# 参考音频的推荐上限（秒）。再长不会更像，只会更慢、更不稳。
REF_MAX_SECONDS = 15.0
# 超过这个长度就提醒一句（prompt 越长每句都要多等）
LONG_REF_SECONDS = 20.0

# 假名（含半角）——出现它就当日语
_KANA_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff\uff66-\uff9d]")


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """线性插值重采样（只为喂 ASR，够用且不引入新依赖）。"""
    if src == dst or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    n = max(1, int(round(x.size * dst / float(src))))
    idx = np.linspace(0.0, x.size - 1.0, n)
    return np.interp(idx, np.arange(x.size, dtype=np.float64), x).astype(np.float32)


def missing_files(settings: Settings) -> list[Path]:
    """缺哪些模型文件（空列表 = 齐了）。create_tts 用它在启动时就退到 Piper。"""
    base = settings.resolve(settings.tts.clone_dir)
    wanted = [
        base / "tokens.txt",
        base / "encoder.int8.onnx",
        base / "decoder.int8.onnx",
        base / "lexicon.txt",
        base / "espeak-ng-data",
        settings.resolve(settings.tts.clone_vocoder),
    ]
    return [p for p in wanted if not p.exists()]


# 目录里的 *清单文件* 解析结果缓存见 voice_loop/manifest.py


def manifest_texts(directory: Path) -> dict[str, str]:
    """目录里「能当参考文本用」的清单条目：``{音频名(小写): 文本}``。

    格式见 :mod:`voice_loop.manifest`（一行名字 + 空行 + 一段文本）。这里只保留
    **目录里真有对应音频**的条目——免得把别的 txt 里的段落当成某个音频的台词。
    """
    stems = {p.stem.lower() for p in directory.glob("*.wav")}
    stems |= {p.name.lower() for p in directory.glob("*.wav")}
    if not stems:
        return {}
    return {k: v for k, v in manifest_pairs(directory).items() if k in stems}



class ZipVoiceTts:
    name = "zipvoice"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.tts
        self.settings = settings
        self._clone_dir = settings.resolve(cfg.clone_dir)
        self._vocoder = settings.resolve(cfg.clone_vocoder)

        self._num_steps = max(1, int(getattr(cfg, "clone_steps", 4) or 4))
        self._min_chars = max(4, int(getattr(cfg, "clone_min_chars", 16) or 16))
        self._threads = max(1, int(getattr(cfg, "clone_threads", 2) or 2))
        self._speed = float(getattr(cfg, "clone_speed", 1.0) or 1.0)
        if self._speed > 1.0:
            # 实测：1.15 时输出从 3.62s 变成 1.37s（10 字/秒），已经算坏了
            print(
                f"[tts] clone_speed={self._speed:g} 会把它弄坏（实测 1.15 → 快 3 倍且含糊），"
                "已按 1.0 处理；想更快去调参考音频（短的=快）",
                file=sys.stderr,
            )
        self._speed = min(1.0, max(0.5, self._speed))
        self._max_ref_seconds = max(
            2.0, float(getattr(cfg, "clone_max_seconds", REF_MAX_SECONDS) or REF_MAX_SECONDS)
        )

        try:
            import sherpa_onnx  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            raise RuntimeError(
                "zipvoice 后端需要 sherpa-onnx：python -m pip install sherpa-onnx"
            ) from None
        self._sh = sherpa_onnx

        self._missing = missing_files(settings)
        if self._missing:
            raise FileNotFoundError(
                "找不到 ZipVoice 模型文件：\n  "
                + "\n  ".join(str(p) for p in self._missing)
                + "\n请执行：python scripts/download_models.py --only zipvoice"
            )

        self._engine = self._create_engine()
        self._rate = int(getattr(self._engine, "sample_rate", 24000) or 24000)

        silence_s = max(0.0, float(cfg.sentence_silence))
        self._silence = np.zeros(int(self._rate * silence_s), dtype=np.int16)
        self._synth_calls = 0
        # 输出静音裁剪：去掉模型每段开头那 0.5~1.5 秒死静音（见 tts/pacing.py）
        self._pacing = PacingFixer.from_config(cfg)
        # 没参考音频时退回 Piper 出声（宁可音色不对，也不能把嘴弄哑）
        self._fallback = None
        self._warned_no_ref = False

        # 参考音频（音色）
        self._ref_audio: np.ndarray | None = None
        self._ref_rate = 0
        self._ref_text = ""
        self._ref_path: Path | None = None
        audio = str(getattr(cfg, "clone_audio", "") or "").strip()
        if audio:
            self.set_reference(audio, str(getattr(cfg, "clone_text", "") or ""), quiet=True)

    # ------------------------------------------------------------------ 模型
    def _create_engine(self):
        cfg = self.settings.tts
        sh = self._sh
        kw = {
            "tokens": str(self._clone_dir / "tokens.txt"),
            "encoder": str(self._clone_dir / "encoder.int8.onnx"),
            "decoder": str(self._clone_dir / "decoder.int8.onnx"),
            "data_dir": str(self._clone_dir / "espeak-ng-data"),
            "lexicon": str(self._clone_dir / "lexicon.txt"),
            "vocoder": str(self._vocoder),
        }
        # 这三个是模型构造期的旋钮：0 = 不动，用库自己的默认值
        for key, field_name in (
            ("guidance_scale", "clone_guidance"),
            ("t_shift", "clone_t_shift"),
            ("target_rms", "clone_target_rms"),
            ("feat_scale", "clone_feat_scale"),
        ):
            value = float(getattr(cfg, field_name, 0.0) or 0.0)
            if value > 0:
                kw[key] = value
        model = sh.OfflineTtsModelConfig(
            zipvoice=sh.OfflineTtsZipvoiceModelConfig(**kw),
            num_threads=self._threads,
            debug=False,
            provider="cpu",
        )
        tts_config = sh.OfflineTtsConfig(model=model)
        validate = getattr(tts_config, "validate", None)
        if callable(validate) and not validate():
            raise ValueError("ZipVoice 配置没通过校验（上面的报错说明了原因）")
        return sh.OfflineTts(tts_config)

    # -------------------------------------------------------------- 参考音色
    @property
    def reference(self) -> str:
        if self._ref_path is None:
            return "（未设参考音频）"
        return self._ref_path.name

    @property
    def reference_text(self) -> str:
        return self._ref_text

    def set_reference(self, audio: str | Path, text: str = "", quiet: bool = False) -> bool:
        """设置参考音色。``text`` 留空时：先找同名 .txt，再（可选）用本地 ASR 转写。

        返回 True 表示参考音色已经生效；False 表示没换（文件缺失之类，保持原样）。
        """
        path = self.settings.resolve(audio)
        if not path.exists():
            if not quiet:
                print(f"[tts] 参考音频不存在：{path}（保持当前音色）", file=sys.stderr)
            return False
        try:
            import soundfile as sf  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            raise RuntimeError("读参考音频需要 soundfile：python -m pip install soundfile") from None
        try:
            samples, rate = sf.read(str(path), dtype="float32", always_2d=False)
        except Exception as exc:
            print(f"[tts] 参考音频读不了：{path}（{exc}）", file=sys.stderr)
            return False

        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim > 1:  # 只取第一声道（ZipVoice 只要单声道）
            samples = samples[:, 0]
        full = self._trim(rate, samples)
        before = full.size / float(rate)

        ref_text, from_file = self._resolve_text(path, text)
        if ref_text:
            # ★文本来自文件就不裁音频★：那段文本是整段的说明，截了就“声不对词”
            use = full
            if not quiet and before > LONG_REF_SECONDS:
                print(
                    f"[tts] 参考音频 {before:.1f}s（偏长，每句都要多等一会儿）。"
                    f"想快就换一条 5~15 秒的，并同时把文本换成那一条的。",
                    file=sys.stderr,
                )
        else:
            # 没有文本 → 可裁，然后拿裁完的音频去转写（这样文本和音频一定是一致的）
            cap = int(self._max_ref_seconds * rate)
            use = full[:cap] if 0 < cap < full.size else full
            ref_text = self._auto_text(path, use, rate, quiet=quiet)
        after = use.size / float(rate)
        text = self._normalize_text(ref_text, quiet=quiet)
        if not text and not quiet:
            print("没拿到参考文本，音色会明显退化（同名 .txt 或同目录清单里写一条）", file=sys.stderr)

        self._ref_audio = np.ascontiguousarray(use, dtype=np.float32)
        self._ref_rate = int(rate)
        self._ref_text = text
        self._ref_path = path
        if not quiet:
            src = "参数" if not ref_text else ("文件" if from_file else "自动转写")
            print(
                f"[tts] 参考音色：{path.name}（{before:.1f}s → {after:.1f}s @ {rate}Hz，"
                f"文本 {len(text)} 字，来自{src}）",
                flush=True,
            )
        return True

    _trim_pad_seconds = 0.05

    def _trim(self, rate: int, samples: np.ndarray) -> np.ndarray:
        """掐掉首尾静音——留白多了会稀释音色。"""
        if samples.size == 0:
            return samples
        peak = float(np.max(np.abs(samples)))
        if peak <= 1e-4:
            return samples
        thr = max(0.01, peak * 0.05)
        idx = np.nonzero(np.abs(samples) > thr)[0]
        if idx.size == 0:
            return samples
        pad = int(self._trim_pad_seconds * rate)
        return samples[max(0, int(idx[0]) - pad) : min(samples.size, int(idx[-1]) + pad + 1)]

    def _normalize_text(self, text: str, quiet: bool = False) -> str:
        """ZipVoice 的文本前端只认中文/英文（假名会被当 OOV 直接丢掉）。

        实测：拿日语参考音频配日语文本 → 前端报 `Ignore OOV`，输出退化成复读乱语
        （17 字中文句生成 22 秒）；换成**罗马字**后回到正常的 3 秒。
        所以这里把假名/汉字转成罗马字——espeak 按英文发音读它，与日语原音基本对得上。
        """
        if not text or not _KANA_RE.search(text):
            return text
        if not bool(getattr(self.settings.tts, "clone_romanize", True)):
            return text
        try:
            import pykakasi  # noqa: PLC0415
        except ImportError:
            if not quiet:
                print(
                    "[tts] 参考文本是日语，但没装 pykakasi，音色会明显退化"
                    "（python -m pip install pykakasi）",
                    file=sys.stderr,
                )
            return text
        try:
            roman = " ".join(
                str(piece.get("hepburn") or "").strip()
                for piece in pykakasi.kakasi().convert(text)
            )
        except Exception as exc:  # pragma: no cover
            if not quiet:
                print(f"[tts] 罗马字转换失败（{exc}），沿用原文", file=sys.stderr)
            return text
        roman = " ".join(roman.split())
        roman = re.sub(r"\s+([,.!?;:])", r"\1", roman)  # 标点前的空格去掉（espeak 会多插一拍）
        if roman and not quiet:
            tail = "…" if len(roman) > 50 else ""
            print(f"[tts] 参考文本是日语 → 已转罗马字：{roman[:50]}{tail}", flush=True)
        return roman or text

    def _resolve_text(self, path: Path, explicit: str = "") -> tuple[str, bool]:
        """找参考文本，返回 ``(文本, 是否来自文件)``。

        顺序：调用方给的 → 同名 ``.txt`` → 同目录清单（如 ``kaltsit.txt``）。
        「来自文件」意味着这段文本描述的是**整段音频**，所以音频不能裁（见 set_reference）。
        """
        text = (explicit or "").strip()
        if text:
            return text, False
        sidecar = path.with_suffix(".txt")
        if sidecar.exists():
            try:
                text = sidecar.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                return text, True
        listed = manifest_texts(path.parent).get(path.name.lower()) or manifest_texts(
            path.parent
        ).get(path.stem.lower())
        return (listed or ""), bool(listed)

    def _auto_text(self, path: Path, samples: np.ndarray, rate: int, quiet: bool = False) -> str:
        """没有现成文本时用本地 ASR 转写**这段音频**，并缓存成同名 .txt。"""
        if not bool(getattr(self.settings.tts, "clone_autotext", True)):
            return ""
        text = self._transcribe(path, samples, rate, quiet=quiet)
        if text:
            try:  # 落盘当缓存，也方便手工修正
                path.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
                if not quiet:
                    print(f"[tts] 自动转写的参考文本已写入 {path.with_suffix('.txt')}（不对就改它）", flush=True)
            except OSError:
                pass
        return text

    def _transcribe(self, path: Path, samples: np.ndarray, rate: int, quiet: bool = False) -> str:
        """用本机已有的 SenseVoice 转写参考音频（中/英/日/韩/粤都支持）。"""
        try:
            from ..asr.sensevoice import SenseVoiceEngine  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover
            if not quiet:
                print(f"[tts] 自动转写不可用（{exc}）", file=sys.stderr)
            return ""
        asr = self.settings.asr
        try:
            engine = SenseVoiceEngine(
                self.settings.resolve(asr.sensevoice_model),
                self.settings.resolve(asr.sensevoice_tokens),
                num_threads=int(getattr(asr, "sensevoice_threads", 4) or 4),
                use_itn=False,
                language="auto",
            )
        except Exception as exc:
            if not quiet:
                print(f"[tts] 自动转写失败（{exc}）", file=sys.stderr)
            return ""
        audio16 = _resample(samples, rate, 16000)
        try:
            result = engine.transcribe(audio16, 16000)
        except Exception as exc:
            if not quiet:
                print(f"[tts] 自动转写失败（{exc}）", file=sys.stderr)
            return ""
        text = str(getattr(result, "text", "") or "").strip()
        if text and not quiet:
            print(f"[tts] 自动转写“{path.name}”：{text}", flush=True)
        return text

    # ------------------------------------------------------------------ 合成
    @property
    def sample_rate(self) -> int:
        return self._rate

    def _gen_config(self):
        sh = self._sh
        gen = sh.GenerationConfig()
        gen.reference_audio = self._ref_audio
        gen.reference_sample_rate = self._ref_rate
        gen.reference_text = self._ref_text
        gen.num_steps = self._num_steps
        # 语速：<1 更慢、>1 更快（1.0 = 模型自己的节奏）
        try:
            gen.speed = self._speed
        except Exception:  # pragma: no cover - 老版本没有这个字段
            pass
        try:
            gen.extra["min_char_in_sentence"] = str(self._min_chars)
        except Exception:  # pragma: no cover - 老版本没有 extra
            pass
        return gen

    def _speak_without_reference(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        """没参考音频就直接用 Piper 出声——音色不对是小事，答不上话是大事。"""
        if not self._warned_no_ref:
            self._warned_no_ref = True
            print(
                "[tts] ZipVoice 没有可用的参考音频，这一句先用 Piper 出声"
                "（在 [tts] clone_audio 或角色的 voice_ref 里指定音频文件）",
                file=sys.stderr,
            )
        if self._fallback is None:
            from .piper_tts import PiperTts  # noqa: PLC0415 - 只在真用到时才导入

            self._fallback = PiperTts(self.settings)
        yield from self._fallback.synth(text)

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        """边生成边产出：第一个 chunk 出来就交给播放器，不必等整句合成完。"""
        text = (text or "").strip()
        if not text:
            return
        if self._ref_audio is None:
            yield from self._speak_without_reference(text)
            return
        gen = self._gen_config()
        box: queue.Queue = queue.Queue()
        errors: list[BaseException] = []

        def callback(samples, progress):  # sherpa 的流式回调
            box.put(np.asarray(samples, dtype=np.float32))
            return 0

        def worker() -> None:
            try:
                self._engine.generate(text, gen, callback=callback)
            except BaseException as exc:  # noqa: BLE001 - 要原样抛回主线程
                errors.append(exc)
            finally:
                box.put(None)

        thread = threading.Thread(target=worker, name="zipvoice", daemon=True)
        thread.start()
        while True:
            item = box.get()
            if item is None:
                break
            pcm = np.clip(item, -1.0, 1.0)
            piece = (pcm * 32767.0).astype(np.int16)
            # 每段开头有 0.5~1.5 秒死静音（实测），剪掉——顺带把首段出声提前
            piece = self._pacing.apply(piece, self._rate)
            if piece.size:
                yield self._rate, piece
        thread.join(timeout=0.1)
        if errors:
            raise RuntimeError(f"ZipVoice 合成失败：{errors[0]}")

        self._synth_calls += 1
        if self._silence.size:
            yield self._rate, self._silence

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        parts: list[np.ndarray] = []
        rate = self._rate
        for r, pcm in self.synth(text):
            rate = r
            parts.append(pcm)
        if not parts:
            return rate, np.zeros(0, dtype=np.int16)
        return rate, np.concatenate(parts)

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        t0 = time.perf_counter()
        first: float | None = None
        parts: list[np.ndarray] = []
        rate = self._rate
        for r, pcm in self.synth(text):
            if first is None:
                first = time.perf_counter() - t0
            rate = r
            parts.append(pcm)
        elapsed = time.perf_counter() - t0
        audio_s = (sum(p.size for p in parts) / float(rate)) if rate else 0.0
        return {
            "engine": self.name,
            "reference": self.reference,
            "num_steps": self._num_steps,
            "speed": self._speed,
            "text_len": len(text),
            "audio_seconds": audio_s,
            "synth_seconds": elapsed,
            "first_chunk_seconds": first if first is not None else 0.0,
            "rtf": elapsed / audio_s if audio_s else 0.0,
            "sample_rate": rate,
        }

    def configure(self, fn) -> None:
        """与 :class:`LazyTts` 对称：非懒加载时直接作用在自己身上。"""
        fn(self)

    def close(self) -> None:
        if self._fallback is not None:
            self._fallback.close()
            self._fallback = None
        self._engine = None  # type: ignore[assignment]
        self._ref_audio = None
        import gc  # noqa: PLC0415

        gc.collect()
