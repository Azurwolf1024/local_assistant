"""流式关键词检测（KWS）—— 让「待唤醒」不再跑完整 ASR。

为什么值得单独一层
    现在待唤醒状态是「每句话都跑一次完整识别，再拿文本去匹配唤醒词」：
    VAD 等静音（~0.5 s）+ 识别（0.3~1 s）之后才知道有没有被叫。所以喊一遍没醒、
    喊两遍才醒是常态，而且**待唤醒时 CPU 一直在跑识别**。

    sherpa-onnx 的 ``KeywordSpotter`` 是流式 transducer KWS：模型只要 30 MB，
    输入几十毫秒的帧就能判断，命中才去跑完整识别。词库直接由
    :class:`voice_loop.asr.lexicon.Lexicon` 生成（``keywords.txt``）。

★默认关（``[asr.kws] enabled = false``）★
    它改变的是唤路路径 —— 这种改动必须先量过（命中率 / 误唤醒）再开。
    没开或模型没下时，这里返回 ``None``，管线自动回到「整句识别 + 文本匹配」。

用法（管线里就一处）
    ``hits = self.kws.detect(chunk)``：返回命中的关键词（空表 = 没被叫）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

#: KWS 模型三件套 + 词表（sherpa-onnx 的 transducer 结构）
FILES = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")


class KwsSpotter:
    """一层薄封装：喂音频 → 命中的关键词。失败一律"没命中"，绝不炸。"""

    def __init__(self, settings: Any, logger=None, *, lexicon: Any = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop.asr.kws")
        self.cfg = getattr(getattr(settings, "asr", None), "kws", None)
        self.lexicon = lexicon
        self._spotter = None
        self._stream = None
        self._why = ""
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self._spotter is None:
            self._load()
        return self._spotter is not None

    @property
    def why(self) -> str:
        """不能用的原因（给日志/控制台看）。"""
        if self._spotter is None:
            self._load()
        return self._why

    # ------------------------------------------------------------------ 加载
    def _paths(self) -> dict[str, Path]:
        root = Path(str(getattr(self.settings, "root", ".") or "."))
        room = Path(str(getattr(self.cfg, "model_dir", "") or "models/asr/kws-zipformer"))
        if not room.is_absolute():
            room = root / room
        return {name.split(".")[0]: room / name for name in FILES}

    def _keywords(self, tokens_path: Path) -> str:
        """关键词表：优先用配置里点名的（手写最可靠），否则让词库生成。"""
        explicit = str(getattr(self.cfg, "keywords_file", "") or "").strip()
        if explicit:
            path = Path(explicit)
            if not path.is_absolute():
                path = Path(str(getattr(self.settings, "root", ".") or ".")) / path
            return str(path) if path.is_file() else ""
        if self.lexicon is None:
            return ""
        tokens: set[str] = set()
        try:
            text = tokens_path.read_text(encoding="utf-8", errors="replace")
            tokens = {line.split()[0] for line in text.splitlines() if line.strip()}
        except OSError:
            return ""
        return self.lexicon.keywords_file(tokens=sorted(tokens))

    def _load(self) -> None:
        if not self.enabled:
            self._why = "没开（[asr.kws] enabled = false）"
            return
        try:
            import sherpa_onnx  # noqa: PLC0415 - 扫目录/待唤醒时都不该加载它
        except Exception as exc:  # noqa: BLE001
            self._why = f"sherpa-onnx 不可用：{exc}"
            return
        paths = self._paths()
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            self._why = "缺模型文件：" + "、".join(Path(m).name for m in missing)
            return
        keywords = self._keywords(paths["tokens"])
        if not keywords:
            self._why = "没有关键词表（词库生成不了就手写 data/lexicon/keywords.txt）"
            return
        try:
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(paths["tokens"]),
                encoder=str(paths["encoder"]),
                decoder=str(paths["decoder"]),
                joiner=str(paths["joiner"]),
                keywords_file=str(keywords),
                num_threads=int(getattr(self.cfg, "num_threads", 2) or 2),
                keywords_score=float(getattr(self.cfg, "keywords_score", 1.0) or 1.0),
                keywords_threshold=float(getattr(self.cfg, "keywords_threshold", 0.25) or 0.25),
                provider=str(getattr(self.cfg, "provider", "cpu") or "cpu"),
            )
            self._stream = self._spotter.create_stream()
            self._why = ""
            self._info(f"KWS 就绪：{paths['encoder'].parent.name}（关键词表 {keywords}）")
        except Exception as exc:  # noqa: BLE001 - 模型/词表不对就当没开
            self._spotter = None
            self._why = f"{type(exc).__name__}: {exc}"
            self._warn(f"KWS 起不来（{self._why}），回到「整句识别 + 文本匹配」")

    # ------------------------------------------------------------------ 检测
    def detect(self, samples: np.ndarray, sample_rate: int = 16000) -> list[str]:
        """喂一段音频，返回命中的关键词（★失败=空表★，调用方当"没被叫"处理）。"""
        if not self.ready or self._spotter is None or self._stream is None:
            return []
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        if audio.size == 0:
            return []
        hits: list[str] = []
        try:
            self._stream.accept_waveform(int(sample_rate), audio)
            self._stream.accept_waveform(int(sample_rate), np.zeros(
                int(sample_rate * 0.3), dtype=np.float32))     # 补一段静音收尾
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                got = str(self._spotter.get_result(self._stream) or "").strip()
                if got:
                    hits.append(got)
                    self._spotter.reset_stream(self._stream)
                else:
                    break
        except Exception as exc:  # noqa: BLE001
            self._warn(f"KWS 检测失败（{exc}），这一句按没命中处理")
            self.reset()
        return hits

    def reset(self) -> None:
        """丢掉当前流的状态（换角色、重新开始听时要调）。"""
        if self._spotter is None or self._stream is None:
            return
        try:
            self._spotter.reset_stream(self._stream)
        except Exception:  # noqa: BLE001
            self._stream = None

    def close(self) -> None:
        self._stream = None
        self._spotter = None

    # ------------------------------------------------------------------ 日志
    def _info(self, line: str) -> None:
        if self.log is not None:
            self.log.info(f"[kws] {line}")

    def _warn(self, line: str) -> None:
        if line in self._warned:
            return
        self._warned.add(line)
        if self.log is not None:
            self.log.warning(f"[kws] {line}")
        else:
            print(f"[kws] {line}", flush=True)


def build_kws(settings: Any, logger=None, lexicon: Any = None) -> KwsSpotter | None:
    """按配置建 KWS；没开就返回 None（管线据此回到文本匹配）。"""
    cfg = getattr(getattr(settings, "asr", None), "kws", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    try:
        return KwsSpotter(settings, logger, lexicon=lexicon)
    except Exception as exc:  # noqa: BLE001 - KWS 起不来不该让人说不出话
        if logger is not None:
            logger.warning(f"[kws] 初始化失败（忽略）：{exc}")
        return None


__all__ = ["FILES", "KwsSpotter", "build_kws"]
