"""ASR 路由：SenseVoice 快路径 + Whisper 按需校验。

三种策略
    sensevoice : 只用 SenseVoice（最低延迟，中文场景推荐）
    whisper    : 只用 Whisper（中英混说 / 长句 / 噪声环境更稳）
    hybrid     : 默认。先跑 SenseVoice，若结果可疑或音频偏长，再让 Whisper 复核并择优。
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..settings import Settings
from ..text import transplant_punctuation
from .base import AsrResult
from .sensevoice import SenseVoiceEngine
from .whisper_ov import WhisperOpenVinoEngine

# Whisper 在静音/噪声上常见的幻觉句
_HALLUCINATION_HINTS = (
    "请不吝点赞",
    "订阅",
    "转发",
    "打赏",
    "字幕由",
    "字幕组",
    "明镜与点点",
    "amara.org",
    "谢谢观看",
    "谢谢大家观看",
    "请关注",
    "感谢观看",
    "subscribe",
    "thanks for watching",
    "♪",
)

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_NON_WORD = re.compile(r"[^\w\u3400-\u9fff\uf900-\ufaff]")


def _repetition_penalty(text: str) -> float:
    """检测逐字/短语复读（解码退化的典型特征），返回 0~1。"""
    t = _NON_WORD.sub("", text or "")
    if len(t) < 6:
        return 0.0
    worst = 0.0
    for size in (1, 2, 3, 4):
        grams = [t[i : i + size] for i in range(len(t) - size + 1)]
        if not grams:
            continue
        _, count = Counter(grams).most_common(1)[0]
        if size == 1 and count >= 6:
            worst = max(worst, 1.0)
        elif size >= 2 and count >= 4:
            worst = max(worst, 0.8)
    return worst


def quality_score(text: str, expect_zh: bool = True) -> float:
    """启发式质量分（0~1），用于在两个引擎之间择优。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    score = 0.6
    if len(t) < 2:
        score -= 0.4

    cjk = len(_CJK.findall(t))
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    total = cjk + latin
    if total and expect_zh:
        score += 0.2 * (cjk / total - 0.5)

    if t[-1] in "。！？!?….;；":
        score += 0.08
    if any(ch in t for ch in "，,、"):
        score += 0.04

    score -= 0.6 * _repetition_penalty(t)
    low = t.lower()
    if any(hint in low for hint in _HALLUCINATION_HINTS):
        score -= 0.35
    return max(0.0, min(1.0, score))


class AsrRouter:
    """对外只暴露一个 ``transcribe``，屏蔽双引擎细节。"""

    def __init__(
        self,
        settings: Settings,
        logger=None,
        preload: bool = True,
        whisper_enabled: bool = True,
    ) -> None:
        self.settings = settings
        self.cfg = settings.asr
        self.log = logger
        self.strategy = (self.cfg.strategy or "hybrid").lower()
        self._sv: SenseVoiceEngine | None = None
        self._wh: WhisperOpenVinoEngine | None = None
        self._sv_error: str | None = None
        self._wh_error: str | None = None
        # Whisper 很重（约 1 GB + 首次编译 40s），允许待唤醒时先不加载
        self._wh_enabled = bool(whisper_enabled)
        self._wh_loading = False
        self._wh_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr")
        if preload:
            for name in self._wanted_engines():
                self._engine(name)

    # ------------------------------------------------------------ 加载 / 卸载
    @property
    def whisper_enabled(self) -> bool:
        return self._wh_enabled

    @property
    def whisper_loaded(self) -> bool:
        return self._wh is not None

    def set_whisper_enabled(self, enabled: bool) -> bool:
        """打开/关闭 Whisper。关闭时尽快卸载，释放内存。

        返回是否真的已经卸掉。

        这里**不能阻塞等加载完成**：首次加载要几十秒，而本方法会在回到待唤醒、
        停机等路径上被调用，阻塞会把整个服务卡死。如果后台正在加载，就只是把
        开关关掉，让加载线程加载完自己发现「已经不需要了」再卸载。
        """
        self._wh_enabled = bool(enabled)
        if enabled:
            return self._wh is None
        return self.request_unload_whisper()

    def request_unload_whisper(self) -> bool:
        """不阻塞地请求卸载 Whisper，返回是否真的卸掉了。"""
        if self._wh is None:
            return not self._wh_loading
        if not self._wh_lock.acquire(blocking=False):
            # 后台正在加载（或卸载），交给它收尾
            return False
        try:
            if self._wh is not None:
                self._wh = None
                self._wh_error = None
                self._log("info", "Whisper 已卸载，内存已释放")
            return True
        finally:
            self._wh_lock.release()

    def load_whisper(self) -> bool:
        """显式加载 Whisper（耗时长，建议在后台线程调用）。

        注意：这里**不会**把 ``_wh_enabled`` 打开——开关由
        :meth:`set_whisper_enabled` 控制。否则会出现在待唤醒状态下
        被后台预热线程“顺手”打开，导致待唤醒时也在跑几秒一次的 Whisper。
        """
        with self._wh_lock:
            if self._wh is not None:
                return True
            self._wh_loading = True
            try:
                self._wh_error = None
                ok = self._engine("whisper") is not None
            finally:
                self._wh_loading = False
            # 加载途中可能已经回到待唤醒（或用户把 Whisper 关了）：别白白占着内存
            if self._wh is not None and not self._wh_enabled:
                self._wh = None
                self._wh_error = None
                self._log("warning", "Whisper 加载完成时已回到待唤醒，立即卸载")
                return False
            return ok

    # ------------------------------------------------------------------ 加载
    def _wanted_engines(self) -> list[str]:
        if self.strategy == "sensevoice":
            return ["sensevoice"]
        if self.strategy == "whisper":
            return ["whisper"]
        return ["sensevoice", "whisper"]

    def _log(self, level: str, msg: str) -> None:
        if self.log:
            getattr(self.log, level)(msg)
        else:
            print(f"[asr] {msg}")

    def _engine(self, name: str):
        if name == "sensevoice":
            if self._sv is None and self._sv_error is None:
                try:
                    self._sv = SenseVoiceEngine(
                        model_path=self.settings.resolve(self.cfg.sensevoice_model),
                        tokens_path=self.settings.resolve(self.cfg.sensevoice_tokens),
                        num_threads=self.cfg.num_threads,
                        use_itn=self.cfg.sensevoice_use_itn,
                        language=self.cfg.language,
                    )
                    self._log("info", f"SenseVoice 就绪：{self.cfg.sensevoice_model}")
                except Exception as exc:  # noqa: BLE001
                    self._sv_error = f"{type(exc).__name__}: {exc}"
                    self._log("warning", f"SenseVoice 不可用：{self._sv_error}")
            return self._sv
        if name == "whisper":
            if not self._wh_enabled:
                return None
            if self._wh is None and self._wh_error is None:
                # 按「GPU 优先、CPU 兜底」的顺序试：设备选错、驱动不灵时不至于整条路没了
                from ..accel import pick_whisper_devices

                errors: list[str] = []
                for device in pick_whisper_devices(self.cfg.whisper_device):
                    try:
                        self._wh = WhisperOpenVinoEngine(
                            model_dir=self.settings.resolve(self.cfg.whisper_model),
                            device=device,
                            language=self.cfg.language,
                            num_threads=self.cfg.num_threads,
                            word_timestamps=self.cfg.whisper_word_timestamps,
                        )
                        self._log("info", f"Whisper 就绪：{self.cfg.whisper_model}（{device}）")
                        break
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{device}: {type(exc).__name__}: {exc}")
                        self._log("warning", f"Whisper 在 {device} 上没起来，换下一个：{exc}")
                if self._wh is None:
                    self._wh_error = " | ".join(errors) or "没有可用设备"
                    self._log("warning", f"Whisper 不可用：{self._wh_error}")
            return self._wh
        raise ValueError(name)

    @property
    def available(self) -> list[str]:
        out = []
        if self._sv is not None:
            out.append("sensevoice")
        if self._wh is not None:
            out.append("whisper")
        return out

    def warmup(self, seconds: float = 0.5) -> dict:
        """用一段静音跑一次推理，让 OpenVINO / ONNX 完成图编译。"""
        import time as _time

        silence = np.zeros(int(16000 * seconds), dtype=np.float32)
        info: dict[str, float] = {}
        for name in self.available:
            # 回到待唤醒后别再白跑一遍成本很高的编译
            if name == "whisper" and not self._wh_enabled:
                continue
            t0 = _time.perf_counter()
            try:
                self._engine(name).transcribe(silence)
                info[name] = _time.perf_counter() - t0
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"{name} 预热失败：{exc}")
        return info
    # ------------------------------------------------------------------ 推理
    def _run(self, name: str, samples: np.ndarray, rate: int) -> AsrResult:
        engine = self._engine(name)
        if engine is None:
            return AsrResult(text="", engine=name)
        try:
            return engine.transcribe(samples, rate)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"{name} 推理失败：{exc}")
            return AsrResult(text="", engine=name)

    def _should_verify(self, sv: AsrResult) -> str | None:
        """判断是否需要 Whisper 复核，返回原因字符串；None 表示不需要。"""
        if self._wh is None:
            return None
        if not sv.text.strip():
            return "SenseVoice 无结果"
        if sv.audio_seconds >= float(self.cfg.whisper_min_duration):
            return f"音频较长({sv.audio_seconds:.1f}s)"
        if sv.score < 0.45:
            return f"结果可疑(score={sv.score:.2f})"
        return None

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        prefer: str | None = None,
    ) -> AsrResult:
        """转写一段音频，返回胜出的结果。

        ``prefer`` 指定 "sensevoice" / "whisper" 时强制使用该引擎，
        否则按 ``[asr] strategy`` 的规则决定。
        """
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        if audio.size == 0:
            return AsrResult(text="", engine="none")
        expect_zh = self.cfg.language in ("zh", "auto")

        def _single(name: str) -> AsrResult:
            result = self._run(name, audio, sample_rate)
            result.score = quality_score(result.text, expect_zh)
            return result

        if prefer in ("sensevoice", "whisper"):
            return _single(prefer)

        if self.strategy == "whisper":
            return _single("whisper")

        # SenseVoice 快路径（sensevoice / hybrid 都先跑它）
        sv = _single("sensevoice")

        if self.strategy == "sensevoice":
            return sv

        # ---------------- hybrid ----------------
        reason = self._should_verify(sv)
        if reason is None:
            return sv

        wh = self._run("whisper", audio, sample_rate)
        wh.score = quality_score(wh.text, expect_zh)
        if wh.avg_logprob is not None:
            # 平均对数概率通常在 -1.0 ~ 0 之间，映射为 0~1 的加分项
            wh.score = min(1.0, max(0.0, wh.score + 0.15 * (1.0 + max(-1.0, wh.avg_logprob))))

        best = wh if wh.score > sv.score else sv
        if best is wh and sv.text.strip():
            # Whisper 的量化导出常不带标点，借用 SenseVoice 的标点补全
            fused = transplant_punctuation(wh.text, sv.text)
            if fused != wh.text:
                wh.text = fused
                wh.extra["punctuation_from"] = "sensevoice"
        self._log(
            "info",
            f"hybrid 复核({reason}) sensevoice={sv.score:.2f} whisper={wh.score:.2f} -> {best.engine}",
        )
        return best

    def transcribe_both(self, samples: np.ndarray, sample_rate: int = 16000) -> list[AsrResult]:
        """并行跑两个引擎，返回全部结果（用于调试与对比）。"""
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        names = [n for n in ("sensevoice", "whisper") if self._engine(n) is not None]
        if not names:
            return []
        futures = [self._pool.submit(self._run, n, audio, sample_rate) for n in names]
        expect_zh = self.cfg.language in ("zh", "auto")
        results = [f.result() for f in futures]
        for r in results:
            r.score = quality_score(r.text, expect_zh)
        return results

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
