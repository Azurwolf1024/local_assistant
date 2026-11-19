"""TTS 的「当前这一把嗓子」：一个可换声线的引擎外壳。

对管线来说它长得跟以前的 :class:`~voice_loop.tts.lazy.LazyTts` 一模一样
（``synth`` / ``sample_rate`` / ``load`` / ``unload`` / ``configure`` / ``on_load``），
区别只有一个：**它持有一个 :class:`~voice_loop.tts.spec.VoiceSpec`**。

换角色 / 换风格 = :meth:`VoiceShell.use` 换一个 spec：

- spec 的键没变 → 什么都不做（不白重载，也不打断正在说的话）；
- 变了 → 卸掉当前引擎，下次出声时由注册表按**新的** spec 重新造一个。

★为什么不再改 `settings.tts`★：以前「换嗓子」是往全局配置里写字段再 unload，
同一时刻只能有一个角色成立（两个角色想用不同后端就得来回切，还得靠
`on_load` 补丁的时序把它救回来）。现在配置是**每个 spec 一份副本**，
全局那份只表示「默认值」，谁也不会被别人的切换影响。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np

from ..settings import Settings
from .lazy import LazyTts
from .registry import TtsRegistry
from .spec import VoiceSpec


class VoiceShell:
    def __init__(self, settings: Settings, registry: TtsRegistry,
                 spec: VoiceSpec | None = None, logger=None) -> None:
        self.settings = settings
        self.registry = registry
        self.log = logger
        self._spec = VoiceSpec.parse(spec)
        # 工厂里读 self._spec（而不是闭包捕获）：use() 换 spec 之后，
        # 下一次 load() 自然就按新的造 —— 不需要重建外壳，补丁也都在。
        self._lazy = LazyTts(settings, lambda _s: registry.open(self._spec), logger, name="tts")

    # ------------------------------------------------------------------ 声线
    @property
    def spec(self) -> VoiceSpec:
        return self._spec

    @property
    def provider_id(self) -> str:
        return self.registry.provider_for(self._spec)

    def describe(self) -> str:
        """一行字：`deepseek / stella`、`zipvoice / 参考 a.wav`。"""
        spec = self._spec
        provider = self.registry.get(self.registry.provider_for(spec))
        label = provider.label if provider is not None else (spec.provider or "跟全局")
        detail = spec.display()
        return f"{label}（{detail}）" if detail and provider is not None else label

    def use(self, spec: VoiceSpec | None, *, reason: str = "") -> bool:
        """换这把嗓子。返回「真的换了没有」（键相同 → False，不会白重载）。"""
        new = VoiceSpec.parse(spec)
        if new.key() == self._spec.key():
            return False
        old = self._spec
        self._spec = new
        if self._lazy.loaded:
            self._lazy.unload()
        if self.log is not None:
            self.log.info(f"[tts] 声线：{old.display()} → {new.display()}"
                          f"（{reason or '切换'}）")
        return True

    def refresh(self) -> None:
        """配置改过（控制台改了声线文件）：丢掉当前引擎，下次按同一 spec 重建。"""
        if self._lazy.loaded:
            self._lazy.unload()

    # -------------------------------------------------------- 转发给懒加载壳
    @property
    def loaded(self) -> bool:
        return self._lazy.loaded

    def load(self):
        return self._lazy.load()

    def unload(self) -> None:
        self._lazy.unload()

    def configure(self, fn) -> None:
        self._lazy.configure(fn)

    def on_load(self, fn) -> None:
        self._lazy.on_load(fn)

    @property
    def name(self) -> str:
        return self._lazy.name

    @property
    def sample_rate(self) -> int:
        return int(self._lazy.sample_rate)

    def synth(self, text: str) -> Iterator[tuple[int, np.ndarray]]:
        yield from self._lazy.synth(text)

    def synth_bytes(self, text: str) -> tuple[int, np.ndarray]:
        return self._lazy.synth_bytes(text)

    def benchmark(self, text: str = "你好，这是一次语音合成的速度测试。") -> dict:
        return self._lazy.benchmark(text)

    @property
    def inject_pauses(self) -> bool:
        return self._lazy.inject_pauses

    @inject_pauses.setter
    def inject_pauses(self, value: bool) -> None:
        self._lazy.inject_pauses = bool(value)

    def __getattr__(self, item: str) -> Any:
        """其它可选方法（比如 ``set_reference``）转发给已加载的引擎。

        ★只转发「已经加载」的引擎★：没加载时抛 AttributeError 更安全 ——
        否则 ``hasattr`` 检查会把「加载一次引擎」当成副作用。
        """
        engine = self._lazy._engine  # noqa: SLF001 - 同一个包里的合成层
        if engine is None:
            raise AttributeError(item)
        return getattr(engine, item)


__all__ = ["VoiceShell"]
