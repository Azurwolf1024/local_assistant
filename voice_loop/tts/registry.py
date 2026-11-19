"""TTS 提供者注册表：发现、健康检查、能力查询、降级链、实例缓存。

这个文件是「加后端不用改别处」的关键。它对外只有四件事：

1. :meth:`TtsRegistry.describe` —— 有哪些后端、各有什么能力、要哪些参数（控制台表单直接用它画）
2. :meth:`TtsRegistry.effective` —— 把「一个 :class:`VoiceSpec`」+ 全局 `[tts]` 合成一份**副本**配置
3. :meth:`TtsRegistry.open` —— 按降级链拿一个能用的引擎（带 LRU 缓存）
4. :meth:`TtsRegistry.voices` —— 某个后端的可选音色

★「不会把嘴弄哑」这条不变量收在这里★：降级链的末尾永远有 `piper`
（除非连 piper 都没注册），并且配置问题（缺模型 / 缺 key）只**提示一次**，
不会每句话都刷一行日志。运行时失败则由引擎自己处理（这一句降级，不伤整轮）。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import pkgutil
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .spec import FieldSpec, ProviderSpec, TtsConfigError, VoiceSpec

#: 老名字 → 新名字（配置和人格文件都可能有历史写法）
ALIASES = {"api": "deepseek"}          # backend = "api" 就是 DeepSeek 官方 TTS


@dataclass(frozen=True)
class ResolvedVoice:
    """一个 spec 解出来的三样东西：哪个 provider、这份配置副本、原始 spec。"""

    provider: ProviderSpec
    settings: Any
    spec: VoiceSpec

    @property
    def cloud(self) -> bool:
        return self.provider.cloud

    def describe(self) -> str:
        return self.spec.display()


class TtsRegistry:
    def __init__(self, settings: Any, logger=None) -> None:
        self.settings = settings
        self.log = logger
        self.providers: dict[str, ProviderSpec] = {}
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._warned: set[str] = set()
        self._discovered = False

    # ------------------------------------------------------------------ 注册
    def register(self, provider: ProviderSpec, *, replace: bool = False) -> None:
        pid = provider.id.strip().lower()
        if not pid:
            raise TtsConfigError("provider 的 id 不能为空")
        if pid in self.providers and not replace:
            raise TtsConfigError(f"provider {pid!r} 已经注册过了")
        self.providers[pid] = dataclasses.replace(provider, id=pid)

    def discover(self, package: str = "voice_loop.tts.providers") -> int:
        """扫 `voice_loop/tts/providers/*.py`：每个模块交出 `PROVIDER` 就算一个后端。

        ★按需导入★：provider 模块顶层只写声明，引擎/第三方库都在 `create()` 里才 import，
        所以「扫一遍」不会把 piper / sherpa / edge-tts 都拉进内存。
        """
        if self._discovered:
            return len(self.providers)
        self._discovered = True
        try:
            pkg = importlib.import_module(package)
        except ImportError as exc:            # 目录被删了也不该让服务起不来
            self._warn(f"无法导入 {package}：{exc}")
            return 0
        found = 0
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{package}.{info.name}")
            except Exception as exc:  # noqa: BLE001 - 一个写坏的后端不该拖垮全部
                self._warn(f"provider 模块 {info.name} 导入失败：{exc}")
                continue
            provider = getattr(mod, "PROVIDER", None)
            if not isinstance(provider, ProviderSpec):
                continue
            self.register(provider, replace=True)
            found += 1
        return found

    # ------------------------------------------------------------------ 查询
    def ids(self) -> list[str]:
        return list(self.providers)

    def get(self, provider_id: str) -> ProviderSpec | None:
        key = str(provider_id or "").strip().lower()
        return self.providers.get(ALIASES.get(key, key))

    def default_provider(self) -> str:
        """`[tts] provider` → 没写就用 piper → 再没有就随便一个（别返回空）。"""
        raw = str(getattr(self.settings.tts, "provider", "") or "").strip().lower()
        want = ALIASES.get(raw, raw)
        if want and want in self.providers:
            return want
        if want:
            self._warn(f"[tts] provider = {want!r} 不认识（已注册：{'、'.join(self.providers) or '无'}）")
        if "piper" in self.providers:
            return "piper"
        return next(iter(self.providers), "")

    def provider_for(self, spec: VoiceSpec) -> str:
        """这个 spec 落到哪个 provider（空 = 跟全局）。"""
        want = ALIASES.get((spec.provider or "").strip().lower(),
                           (spec.provider or "").strip().lower())
        if want and want in self.providers:
            return want
        if want:
            self._warn(f"声音配置里的 provider = {want!r} 不认识，改用默认的")
        return self.default_provider()

    def voices(self, provider_id: str) -> list[str]:
        provider = self.get(provider_id) or self.get(self.default_provider())
        if provider is None or provider.voices is None:
            return []
        try:
            return list(provider.voices(self.settings))
        except Exception as exc:  # noqa: BLE001 - 列不出来不影响出声
            self._warn(f"{provider.id} 的音色列表取不到：{exc}")
            return []

    def describe(self) -> list[dict]:
        """给控制台用：每个后端的能力 + 参数表 + 可选音色。"""
        out = []
        for provider in self.providers.values():
            item = provider.as_dict()
            item["voices"] = self.voices(provider.id)
            item["ready"], item["why"] = self._ready(provider)
            item["default"] = provider.id == self.default_provider()
            out.append(item)
        return out

    # --------------------------------------------------------------- 配置合成
    def effective(self, spec: VoiceSpec) -> ResolvedVoice:
        """★把 spec 解成一份**副本**配置★（绝不改全局 settings）。

        优先级（低 → 高）：
        `TtsConfig` 默认值 → provider.defaults → `[tts.providers.<id>]` → `spec.params`
        → `provider.apply()`（把 voice_id / ref 映射成引擎真正读的字段）。

        这样一个角色用克隆、另一个用官方 TTS，可以**同时**各持一份配置，
        不再靠「改全局 + unload」串行切换。
        """
        provider = self.get(self.provider_for(spec))
        if provider is None:
            raise TtsConfigError("没有任何可用的 TTS 后端（voice_loop/tts/providers 是空的？）")
        tts = dataclasses.replace(self.settings.tts)
        declared: dict[str, FieldSpec] = {f.field_name(): f for f in provider.fields}
        block = (getattr(self.settings.tts, "providers", {}) or {}).get(provider.id) or {}
        for source, values in (("defaults", provider.defaults), ("config", block),
                               ("params", spec.params)):
            for name, value in dict(values or {}).items():
                self._assign(tts, provider, declared, str(name), value, source)
        settings = dataclasses.replace(self.settings, tts=tts)
        if provider.apply is not None:
            settings = provider.apply(settings, spec) or settings
        return ResolvedVoice(provider=provider, settings=settings, spec=spec)

    def _assign(self, tts: Any, provider: ProviderSpec, declared: dict[str, FieldSpec],
                name: str, value: Any, source: str) -> None:
        spec_field = declared.get(name)
        if spec_field is not None:
            if not hasattr(tts, name):
                # provider 自己的参数（url / rate / steps…）：引擎通过 options 读，
                # 不往 TtsConfig 上摁，不算错误
                return
            try:
                setattr(tts, name, _coerce(value, spec_field))
            except (TypeError, ValueError) as exc:
                self._warn(f"[tts.providers.{provider.id}] {name} 值不对（{exc}），忽略")
            return
        if hasattr(tts, name):
            # 没在能力表里声明、但 TtsConfig 认识的字段：照样允许（配置能直接搬）
            try:
                setattr(tts, name, _coerce(value, None, hint=getattr(tts, name)))
            except (TypeError, ValueError) as exc:
                self._warn(f"[tts.providers.{provider.id}] {name} 值不对（{exc}），忽略")
            return
        self._warn(f"{provider.id} 不认识参数 {name!r}（来自 {source}），忽略")

    def options(self, spec: VoiceSpec) -> dict:
        """某个 spec 的**私有参数**（provider 默认 → `[tts.providers.<id>]` → spec.params）。

        给那些不落在 `TtsConfig` 上的后端用（edge 的 rate、http 的 url/body…）。
        """
        provider = self.get(self.provider_for(spec))
        if provider is None:
            return dict(spec.params or {})
        out = dict(provider.defaults or {})
        block = (getattr(self.settings.tts, "providers", {}) or {}).get(provider.id) or {}
        out.update(block)
        out.update(dict(spec.params or {}))
        return out

    # ------------------------------------------------------------------ 降级链
    def chain(self, spec: VoiceSpec) -> list[VoiceSpec]:
        """这个 spec 的降级顺序（去重）：自己 → spec.fallback → `[tts] fallback` → piper。"""
        first = spec.replace(provider=self.provider_for(spec))
        entries = [first]
        for raw in (*spec.fallback, *self._global_fallback()):
            got = VoiceSpec.parse(raw)
            if got.provider:
                entries.append(got)
        if "piper" in self.providers:
            entries.append(VoiceSpec(provider="piper"))
        seen: set[str] = set()
        out: list[VoiceSpec] = []
        for item in entries:
            key = item.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _global_fallback(self) -> tuple[str, ...]:
        raw = getattr(self.settings.tts, "fallback", ()) or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(str(x).strip() for x in raw if str(x).strip())

    def _ready(self, provider: ProviderSpec) -> tuple[bool, str]:
        if provider.create is None:
            return False, "这个后端没有实现 create()"
        if provider.ready is None:
            return True, ""
        try:
            ok, why = provider.ready(self.settings)
        except Exception as exc:  # noqa: BLE001 - 自检自己崩了也要能回答
            return False, f"自检失败：{exc}"
        return bool(ok), str(why or "")

    # ------------------------------------------------------------- 引擎实例
    def open(self, spec: VoiceSpec) -> Any:
        """按降级链拿一个**能用**的引擎（同一个 spec 命中缓存就复用）。"""
        problems: list[str] = []
        chain = self.chain(spec)
        for candidate in chain:
            provider = self.get(candidate.provider)
            if provider is None:
                problems.append(f"{candidate.provider or '(空)'}：没注册")
                continue
            ok, why = self._ready(provider)
            if not ok:
                problems.append(f"{provider.id}：{why}")
                self._warn(f"{provider.id} 用不了（{why}）")
                continue
            key = candidate.key()
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            resolved = self.effective(candidate)
            try:
                engine = provider.create(resolved.settings, candidate)
            except Exception as exc:  # noqa: BLE001 - 换下一个，别把嘴弄哑
                problems.append(f"{provider.id}：构造失败 {type(exc).__name__}: {exc}")
                self._warn(f"{provider.id} 构造引擎失败（{exc}），试下一个")
                continue
            if provider.wants_fallback_factory:
                factory = getattr(engine, "set_fallback_factory", None)
                if callable(factory):
                    factory(lambda c=candidate: self.fallback_engine(c))
            self._cache[key] = engine
            self._evict()
            self._info(f"[tts] 加载 {provider.id}：{candidate.display()}")
            return engine
        raise TtsConfigError("没有可用的 TTS 后端：" + "；".join(problems or ["注册表是空的"]))

    def fallback_engine(self, spec: VoiceSpec) -> Any:
        """运行期退路（云端 TTS 中途失败时用）：跳过自己，取链上的下一个。"""
        for candidate in self.chain(spec)[1:]:
            try:
                return self.open(candidate)
            except TtsConfigError as exc:
                self._warn(f"退路 {candidate.provider} 也不可用：{exc}")
        raise TtsConfigError("连退路都没有可用的 TTS 后端")

    def has(self, spec: VoiceSpec) -> bool:
        try:
            self.open(spec)
            return True
        except TtsConfigError:
            return False

    def loaded(self) -> list[str]:
        return list(self._cache)

    def close_all(self) -> None:
        for engine in self._cache.values():
            self._closing(engine)
        self._cache.clear()

    @staticmethod
    def _closing(engine: Any) -> None:
        close = getattr(engine, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 收尾失败不影响别的引擎
                pass

    def _evict(self) -> None:
        limit = max(1, int(getattr(self.settings.tts, "max_loaded", 1) or 1))
        while len(self._cache) > limit:
            key, engine = self._cache.popitem(last=False)
            self._closing(engine)
            self._info(f"[tts] 卸载（缓存到上限 {limit}）：{key[:60]}")

    # ------------------------------------------------------------------ 日志
    def _info(self, line: str) -> None:
        if self.log is not None:
            self.log.info(line)

    def _warn(self, line: str) -> None:
        """★同一句只打一次★：配置问题不该每说一句话刷一行。"""
        if line in self._warned:
            return
        self._warned.add(line)
        if self.log is not None:
            self.log.warning(f"[tts] {line}")
        else:
            print(f"[tts] {line}", flush=True)


def _coerce(value: Any, field: FieldSpec | None, *, hint: Any = None) -> Any:
    """按声明（或当前值的类型）把配置里的值转成对的类型。"""
    kind = field.kind if field is not None else None
    if kind == "bool" or (kind is None and isinstance(hint, bool)):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if kind == "number" or (kind is None and isinstance(hint, (int, float))
                            and not isinstance(hint, bool)):
        number = float(value)
        return int(number) if kind != "number" and isinstance(hint, int) else number
    if kind == "json":
        if isinstance(value, (dict, list)):
            return value
        return json.loads(str(value))
    if kind == "select":
        text = str(value).strip()
        if field is not None and field.options and text not in field.options:
            raise ValueError(f"只能是 {'/'.join(field.options)} 之一（现在是 {text!r}）")
        return text
    return str(value).strip() if not isinstance(value, (dict, list)) else value


def iter_providers(registry: TtsRegistry) -> Iterator[ProviderSpec]:
    yield from registry.providers.values()


def specs_for(registry: TtsRegistry, specs: Iterable[VoiceSpec]) -> list[ResolvedVoice]:
    return [registry.effective(spec) for spec in specs]


__all__ = ["ResolvedVoice", "TtsRegistry", "iter_providers", "specs_for"]
