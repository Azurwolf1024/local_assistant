"""``Hearing``：语音识别的唯一门面（管线只认它，不再认引擎名字）。

它保留的是**策略**，不是引擎：

```
主引擎（快） ──结果可疑/太长/没听出──▶ 复核引擎（准） ──▶ 择优（+标点移植）
        │
        └─ 两级加载：heavy 引擎只在「唤醒之后」才加载，「回到待唤醒」立刻卸
```

为什么要有这一层（对应 `tts/session.py` 的 ``VoiceShell``）
    以前 ``pipeline`` 直接调 ``asr.set_whisper_enabled / whisper_loaded /
    load_whisper / transcribe(prefer="sensevoice")`` —— 四个 API 全带引擎名，
    所以「换引擎」和「换策略」都改不动。现在这些全部变成能力：
    ``heavy``（要两级加载吗）、``fast``（能不能当回听校验的快路径）、
    ``hotwords``（词库怎么喂）。

★兼容★：老名字（``set_whisper_enabled`` / ``load_whisper`` / ``whisper_loaded``）
都还在，只是转发到通用实现，所以既有的唤醒竞态自测（``test_wake_cycle.py``）不用改。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from ..text import transplant_punctuation
from .base import AsrResult
from .quality import quality_score
from .registry import AsrRegistry
from .spec import CAP_HEAVY, AsrSpec

DEFAULT_WARMUP_SECONDS = 0.5


class Hearing:
    """语音识别门面。一个实例 = 一套「怎么听」的策略 + 它加载的引擎。"""

    def __init__(
        self,
        settings,
        logger=None,
        *,
        preload: bool = True,
        heavy_enabled: bool = True,
        registry: AsrRegistry | None = None,
        lexicon: Any = None,
        **legacy: Any,
    ) -> None:
        # ★老关键字也认★：以前叫 ``whisper_enabled``（那时只有 Whisper 需要两级加载）
        if "whisper_enabled" in legacy:
            heavy_enabled = bool(legacy.pop("whisper_enabled"))
        if legacy:
            raise TypeError(f"Hearing 不认识这些参数：{', '.join(legacy)}")
        self.settings = settings
        self.log = logger
        self.registry = registry or AsrRegistry(settings, logger)
        self.registry.discover()
        self.spec: AsrSpec = self.registry.default_spec()
        self.lexicon = lexicon
        self._heavy_enabled = bool(heavy_enabled)
        self._heavy_loading = False
        self._heavy_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr")
        if preload:
            for pid in self._wanted():
                self._engine(pid)

    # ------------------------------------------------------------------ 查询
    @property
    def available(self) -> list[str]:
        """已经加载好的引擎（启动横幅 / 自检用）。"""
        return self.registry.loaded()

    @property
    def language(self) -> str:
        return str(self.spec.language or getattr(self.settings.asr, "language", "zh") or "zh")

    def describe(self) -> str:
        """``sensevoice(常驻) + whisper(按需)`` 这种一行字。"""
        return self.registry.summary()

    def providers(self) -> list[dict]:
        return self.registry.describe()

    def refresh_spec(self) -> AsrSpec:
        """配置改了之后重新读一遍（控制台/CLI 改完不用重启）。"""
        self.spec = self.registry.default_spec()
        return self.spec

    # ------------------------------------------------------- 两级加载（heavy）
    @property
    def heavy_ids(self) -> list[str]:
        """这条策略里所有 ``heavy`` 后端（通常是复核位那一个）。"""
        ids = [self.registry.provider_for(self.spec)]
        if self.spec.verify:
            ids.append(self.registry.provider_for(self.spec.replace(provider=self.spec.verify)))
        out = []
        for pid in ids:
            provider = self.registry.get(pid)
            if provider is not None and provider.supports(CAP_HEAVY) and pid not in out:
                out.append(pid)
        return out

    @property
    def heavy_enabled(self) -> bool:
        return self._heavy_enabled

    @property
    def heavy_loaded(self) -> bool:
        return any(self.registry.is_loaded(pid) for pid in self.heavy_ids)

    def set_heavy_enabled(self, enabled: bool) -> bool:
        """打开/关闭重引擎。关闭时尽快卸载，返回是否真的已经卸掉。

        ★不能阻塞等加载完成★：首次加载几十秒，而本方法会在「回到待唤醒」
        「停机」这些路径上被调用，阻塞会把整个服务卡死。如果后台正在加载，
        就只把开关关掉，让加载线程加载完自己发现"已经不需要了"再卸载。
        """
        self._heavy_enabled = bool(enabled)
        if enabled:
            return not self.heavy_loaded
        return self.request_unload_heavy()

    def request_unload_heavy(self) -> bool:
        """不阻塞地请求卸载重引擎，返回是否真的卸掉了。"""
        if not self.heavy_loaded and not self._heavy_loading:
            return True
        if not self._heavy_lock.acquire(blocking=False):
            return False                    # 后台正在加载/卸载，交给它收尾
        try:
            gone = False
            for pid in self.heavy_ids:
                gone = self.registry.unload(pid) or gone
            if gone:
                self._info("重引擎已卸载，内存已释放")
            return True
        finally:
            self._heavy_lock.release()

    def load_heavy(self) -> bool:
        """显式加载重引擎（耗时长，建议在后台线程调用）。

        ★不会顺手把开关打开★：否则预热线程会在待唤醒状态把它加载起来，
        于是待唤醒时每句话都跑一遍几秒的重引擎（踩过）。
        """
        ids = self.heavy_ids
        if not ids:
            return True
        with self._heavy_lock:
            self._heavy_loading = True
            try:
                ok = True
                for pid in ids:
                    ok = self._engine(pid, force_heavy=True) is not None and ok
            finally:
                self._heavy_loading = False
            if self.heavy_loaded and not self._heavy_enabled:
                for pid in ids:
                    self.registry.unload(pid)
                self._warn("重引擎加载完成时已回到待唤醒，立即卸载")
                return False
            return ok

    # -------------------------------------------------------- 老名字（兼容）
    @property
    def whisper_enabled(self) -> bool:
        return self._heavy_enabled

    @property
    def whisper_loaded(self) -> bool:
        return self.heavy_loaded

    def set_whisper_enabled(self, enabled: bool) -> bool:
        return self.set_heavy_enabled(enabled)

    def request_unload_whisper(self) -> bool:
        return self.request_unload_heavy()

    def load_whisper(self) -> bool:
        return self.load_heavy()

    # --------------------------------------------------------------- 引擎管理
    def _wanted(self) -> list[str]:
        """preload 时要加载哪些：主引擎（常驻）+ 复核引擎（heavy 且允许时）。"""
        ids = [self.registry.provider_for(self.spec)]
        if self.spec.verify:
            verify = self.registry.provider_for(self.spec.replace(provider=self.spec.verify))
            provider = self.registry.get(verify)
            if provider is not None and (not provider.supports(CAP_HEAVY) or self._heavy_enabled):
                ids.append(verify)
        return ids

    def _spec_for(self, provider_id: str) -> AsrSpec:
        """把对外的策略 spec 收窄成"给某个后端用"的那一份（含词库注入）。"""
        spec = self.spec.replace(provider=provider_id)
        lex = self.lexicon
        if lex is None:
            return spec
        provider = self.registry.get(provider_id)
        mode = provider.hotwords if provider is not None else "post"
        try:
            if mode == "native":
                path = lex.hotwords_file()
                if path:
                    spec = spec.replace(params={**spec.params, "hotwords_file": str(path)})
            elif mode == "prompt":
                text = lex.prompt_text()
                if text:
                    spec = spec.replace(params={**spec.params, "initial_prompt": text})
        except Exception as exc:  # noqa: BLE001 - 词库出问题不该让人说不出话
            self._warn(f"词库注入失败（{exc}），这一轮按无词库识别")
        return spec

    def _engine(self, provider_id: str, *, force_heavy: bool = False):
        provider = self.registry.get(provider_id)
        if provider is None:
            return None
        allow_heavy = self._heavy_enabled or force_heavy
        if provider.supports(CAP_HEAVY) and not allow_heavy:
            return None
        return self.registry.engine(provider_id, self._spec_for(provider_id),
                                    allow_heavy=allow_heavy)

    def _post_fix(self, provider_id: str, text: str) -> str:
        """事后纠正（``hotwords="post"`` 的后端走这条）。"""
        lex = self.lexicon
        provider = self.registry.get(provider_id)
        if lex is None or text is None:
            return text
        if provider is not None and provider.hotwords != "post":
            return text
        try:
            fixed, _hits = lex.fix(text)
            return fixed or text
        except Exception as exc:  # noqa: BLE001
            self._warn(f"词库纠正失败（{exc}），用原文本")
            return text

    # ------------------------------------------------------------------ 推理
    def _resolve_prefer(self, prefer: str) -> str:
        """``prefer`` 支持 ``""``（按策略）/ ``"fast"``（按能力）/ 具体 provider。"""
        want = str(prefer or "").strip().lower()
        if not want or want in ("auto", "policy", "default"):
            return ""
        if want == "fast":
            # ★按能力挑，不按名字★：从降级链里找第一个"快"且真能起来的后端
            for candidate in self.registry.chain(self.spec):
                provider = self.registry.get(candidate.provider)
                if provider is None or not provider.fast:
                    continue
                if self._engine(provider.id) is not None:
                    return provider.id
            return ""
        resolved = self.registry.get(want)
        if resolved is None:
            self._warn(f"prefer = {want!r} 不认识，按策略走")
            return ""
        return resolved.id

    def _run(self, provider_id: str, audio: np.ndarray, rate: int) -> AsrResult:
        engine = self._engine(provider_id)
        if engine is None:
            return AsrResult(text="", engine=provider_id)
        try:
            result = engine.transcribe(audio, rate)
        except Exception as exc:  # noqa: BLE001 - 一个引擎坏了不该炸一轮对话
            self._warn(f"{provider_id} 推理失败：{exc}")
            return AsrResult(text="", engine=provider_id)
        result.text = self._post_fix(provider_id, result.text) or ""
        return result

    def _single(self, provider_id: str, audio: np.ndarray, rate: int) -> AsrResult:
        result = self._run(provider_id, audio, rate)
        result.score = quality_score(result.text, self.language in ("zh", "auto", ""))
        return result

    def _verify_reason(self, result: AsrResult) -> str | None:
        """按 ``verify_when`` 判断要不要复核；返回原因（给人看），None = 不用。"""
        if not self.spec.verify or not self.spec.verify_when:
            return None
        empty = not result.text.strip()
        for when in self.spec.verify_when:
            if when.hit(empty=empty, seconds=result.audio_seconds, score=result.score):
                if when.kind == "empty":
                    return "主引擎无结果"
                if when.kind == "long":
                    return f"音频较长({result.audio_seconds:.1f}s)"
                if when.kind == "score":
                    return f"结果可疑(score={result.score:.2f})"
                return "总是复核"
        return None

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        prefer: str = "",
    ) -> AsrResult:
        """转写一段音频，返回胜出的结果。

        ``prefer``：
        - 空 → 按策略（主引擎 + 必要时复核择优）
        - ``"fast"`` → 按**能力**挑最快的那个（TTS 回听校验走这条）
        - provider 名字 → 只用它（调试 / 自测用）
        """
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        if audio.size == 0:
            return AsrResult(text="", engine="none")

        target = self._resolve_prefer(prefer)
        if target:
            return self._single(target, audio, sample_rate)

        primary = self.registry.provider_for(self.spec)
        first = self._single(primary, audio, sample_rate)
        reason = self._verify_reason(first)
        if reason is None:
            return first

        verifier = self.registry.provider_for(self.spec.replace(provider=self.spec.verify))
        if verifier == primary:
            return first
        second = self._run(verifier, audio, sample_rate)
        if not second.text.strip() and not first.text.strip():
            return first
        second.score = quality_score(second.text, self.language in ("zh", "auto", ""))
        if second.avg_logprob is not None:
            # 平均对数概率通常在 -1.0 ~ 0 之间，映射成 0~1 的加分项
            second.score = min(1.0, max(0.0, second.score
                                        + 0.15 * (1.0 + max(-1.0, second.avg_logprob))))
        expect_zh = self.language in ("zh", "auto", "")
        if not _heard(first.text) and not _plausible(second.text, expect_zh):
            # ★主引擎什么都没听出，复核却冒出一句不像话的★ —— 典型是 Whisper 在
            # 短音/静音上的幻觉（实测把「阿米娅」听成 "MING PAO CANADA MING PAO TORONTO"，
            # 而且质量分还比"空结果"高，于是被选中）。宁可说没听清，也别凭空编一句。
            self._info(f"复核结果不像话（{second.text[:24]!r}），按没听清处理")
            return first
        best = second if second.score > first.score else first
        if best is second and first.text.strip():
            # 量化导出常不带标点：借用主引擎（SenseVoice）的标点补全
            fused = transplant_punctuation(second.text, first.text)
            if fused != second.text:
                second.text = fused
                second.extra["punctuation_from"] = first.engine
        self._info(f"复核({reason}) {first.engine}={first.score:.2f} "
                   f"{second.engine}={second.score:.2f} -> {best.engine}")
        return best

    def transcribe_both(self, samples: np.ndarray,
                        sample_rate: int = 16000) -> list[AsrResult]:
        """并行跑主引擎与复核引擎，返回全部结果（调试与对比用）。"""
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        ids = [pid for pid in self._wanted() if self._engine(pid) is not None]
        if not ids:
            return []
        futures = [self._pool.submit(self._single, pid, audio, sample_rate) for pid in ids]
        return [f.result() for f in futures]

    def transcribe_file(self, path: str | Path, *, prefer: str = "fast") -> str:
        """转写一个音频文件（给 TTS 自动生成参考文本之类的内部用途）。

        ★只依赖 soundfile + numpy★：不把管线那一套重采样/播放链拖进来，
        免得「TTS 想转写一下参考音频」变成跨模块依赖。
        """
        try:
            import soundfile as sf  # noqa: PLC0415

            data, rate = sf.read(str(path), dtype="float32", always_2d=True)
            mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
            if int(rate) != 16000:
                mono = _resample_linear(mono, int(rate), 16000)
            return self.transcribe(np.ascontiguousarray(mono, dtype=np.float32),
                                   16000, prefer=prefer).text
        except Exception as exc:  # noqa: BLE001 - 转写失败不该让调用方崩
            self._warn(f"转写文件失败（{path}）：{exc}")
            return ""

    # --------------------------------------------------------------- 预热/收尾
    def warmup(self, seconds: float = DEFAULT_WARMUP_SECONDS) -> dict:
        """用一段静音跑一次推理，让 ONNX / OpenVINO 完成图编译。"""
        import time as _time

        silence = np.zeros(int(16000 * max(0.05, seconds)), dtype=np.float32)
        info: dict[str, float] = {}
        for pid in self._wanted():
            provider = self.registry.get(pid)
            if provider is not None and provider.supports(CAP_HEAVY) and not self._heavy_enabled:
                continue
            engine = self._engine(pid)
            if engine is None:
                continue
            t0 = _time.perf_counter()
            try:
                engine.transcribe(silence)
                info[pid] = _time.perf_counter() - t0
            except Exception as exc:  # noqa: BLE001
                self._warn(f"{pid} 预热失败：{exc}")
        return info

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.registry.close_all()

    # ------------------------------------------------------------------ 日志
    def _info(self, line: str) -> None:
        if self.log is not None:
            self.log.info(f"[asr] {line}")

    def _warn(self, line: str) -> None:
        if self.log is not None:
            self.log.warning(f"[asr] {line}")
        else:
            print(f"[asr] {line}", flush=True)


def _heard(text: str) -> bool:
    """真的听出字了吗（★只有一个「。」不算★：短音上主引擎经常只回一个标点）。"""
    return any(ch.isalnum() for ch in str(text or ""))


def _plausible(text: str, expect_zh: bool) -> bool:
    """这句复核结果"像话"吗（防幻觉用）。

    中文场景下要求：真的有汉字、而且汉字占比不至于离谱（Whisper 在静音上会整句吐英文），
    并且长度跟"一段人话"差不太多。
    """
    body = str(text or "").strip()
    if not body:
        return False
    if not expect_zh:
        return True
    cjk = sum(1 for ch in body if "\u3400" <= ch <= "\u9fff")
    if cjk == 0:
        return False
    return cjk / max(1, len(body)) >= 0.15


def _resample_linear(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    """线性重采样（内部工具用：参考音频转写不在乎那点精度）。"""
    if src == dst or samples.size == 0:
        return samples
    ratio = dst / float(src)
    count = max(1, int(round(samples.size * ratio)))
    src_pos = np.linspace(0.0, samples.size - 1, count, dtype=np.float64)
    return np.interp(src_pos, np.arange(samples.size, dtype=np.float64),
                     samples.astype(np.float64)).astype(np.float32)


__all__ = ["Hearing"]
