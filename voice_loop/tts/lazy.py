"""按需加载 / 可卸载的 TTS 包装（后端无关）。

克隆类 TTS（ZipVoice / IndexTTS 之类）动辄几百 MB 到几 GB，
不能让它在待唤醒状态常驻内存；被唤醒时才加载，睡回去就释放。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

import numpy as np

from ..settings import Settings
from .base import TtsEngine

# 工厂：拿 settings 造一个真正的引擎
EngineFactory = Callable[[Settings], TtsEngine]


class LazyTts:
    """懒加载外壳，对外接口与 :class:`TtsEngine` 完全一致。

    待唤醒时内存里只有这个壳；``load()`` 之后才真正构造引擎。
    """

    def __init__(
        self,
        settings: Settings,
        factory: EngineFactory,
        logger=None,
        name: str = "tts",
    ) -> None:
        self.settings = settings
        self._factory = factory
        self.log = logger
        self._name = name
        self._engine: TtsEngine | None = None
        self._lock = threading.Lock()
        # 每次引擎加载好都要打一遍的补丁（见 on_load）
        self._pending: list[Callable[[TtsEngine], None]] = []
        self.inject_pauses = bool(getattr(settings.tts, "inject_pauses", False))

    # ------------------------------------------------------------- 加载/卸载
    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def load(self) -> TtsEngine:
        with self._lock:
            if self._engine is None:
                t0 = time.perf_counter()
                self._engine = self._factory(self.settings)
                for fn in self._pending:  # ★懒加载也要吃上补丁★
                    fn(self._engine)
                if self.log:
                    self.log.info(
                        f"TTS 已加载：{getattr(self._engine, 'name', self._name)}"
                        f"（{time.perf_counter() - t0:.1f}s）"
                    )
            return self._engine

    def unload(self) -> None:
        with self._lock:
            if self._engine is None:
                return
            close = getattr(self._engine, "close", None)
            if callable(close):
                close()
            self._engine = None
            if self.log:
                self.log.info("TTS 已卸载")

    def configure(self, fn: Callable[[TtsEngine], None]) -> None:
        """给「已加载的引擎」打补丁；没加载就什么都不做（等下次加载自然生效）。

        换角色声线时用：声音正在用就不能白等一次重载，没用着就别为它加载。
        """
        with self._lock:
            if self._engine is not None:
                fn(self._engine)

    def on_load(self, fn: Callable[[TtsEngine], None]) -> None:
        """登记一个「**每次**引擎加载好都打一遍」的补丁；已加载就当场打。

        跟 :meth:`configure` 的分别（踩过坑，写在这以免又被静默坑一次）：
        ``configure`` 在引擎没加载时是**丢掉**的——那是因为它服务的「换参考音频」
        有配置来源（构造函数会自己按 ``clone_audio`` 设好）。而有些补丁**只存在于
        运行时**（比如管线把「合成回听」的校验器接进来），靠配置回不来；
        启动时引擎还没加载，用 configure 就会**一声不吭地失效**。
        这类补丁必须用 ``on_load``（``unload`` 后再加载也不会丢）。
        """
        with self._lock:
            self._pending.append(fn)
            if self._engine is not None:
                fn(self._engine)

    @property
    def name(self) -> str:
        engine = self._engine
        return str(getattr(engine, "name", None) or self._name)

    # --------------------------------------------------------------------- api
    @property
    def sample_rate(self) -> int:
        return int(self.load().sample_rate)

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        yield from self.load().synth(text)

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        engine = self.load()
        fn = getattr(engine, "synth_bytes", None)
        if callable(fn):
            return fn(text)
        parts: list[np.ndarray] = []
        rate = self.sample_rate
        for r, pcm in self.synth(text):
            rate = r
            parts.append(pcm)
        if not parts:
            return rate, np.zeros(0, dtype=np.int16)
        return rate, np.concatenate(parts)

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        engine = self.load()
        fn = getattr(engine, "benchmark", None)
        if callable(fn):
            return fn(text)
        t0 = time.perf_counter()
        rate, pcm = self.synth_bytes(text)
        elapsed = time.perf_counter() - t0
        audio_s = pcm.size / float(rate) if rate else 0.0
        return {
            "engine": self.name,
            "text_len": len(text),
            "audio_seconds": audio_s,
            "synth_seconds": elapsed,
            "rtf": elapsed / audio_s if audio_s else 0.0,
            "sample_rate": rate,
        }
