"""识别后端注册表：发现、健康检查、能力查询、配置副本、降级链、实例缓存。

这个文件是「加后端 / 换策略不用改别处」的关键。对外只有这几件事：

1. :meth:`AsrRegistry.describe` —— 有哪些后端、各有什么能力（控制台/日志直接用它）
2. :meth:`AsrRegistry.default_spec` —— `[asr]`（含老写法 ``strategy``）→ 一份 :class:`AsrSpec`
3. :meth:`AsrRegistry.effective` —— spec + `[asr.providers.<id>]` 合成一份**配置副本**
4. :meth:`AsrRegistry.engine` —— 拿一个**能用**的引擎（``heavy`` 可以按状态拒绝）
5. :meth:`AsrRegistry.missing` —— 还缺哪些文件（`require_models` / doctor 用）

★两条不变量★
- 待唤醒状态（``allow_heavy=False``）**绝不**构造 ``heavy`` 引擎（Whisper 是 1 GB / 首次编译 40 s）
- 配置问题（缺模型 / 名字写错）只**提示一次**，不会每句话刷一行日志
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import pkgutil
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .spec import (CAP_HEAVY, CAP_LOCAL, AsrConfigError, AsrSpec, ProviderSpec,
                   WHEN_EMPTY, WHEN_LONG, WHEN_SCORE, parse_when)

#: 老的 ``[asr] strategy`` → (主引擎, 复核引擎, 复核条件)
#: ★这是唯一还认识"老名字"的地方★：其他地方一律走 spec
LEGACY_STRATEGY = {
    "sensevoice": ("sensevoice", "", ()),
    "whisper": ("whisper", "", ()),
    "hybrid": ("sensevoice", "whisper",
               (WHEN_EMPTY, f"{WHEN_LONG}:6.0", f"{WHEN_SCORE}<0.45")),
}

#: provider 别名（配置里可能写历史名字）
ALIASES: dict[str, str] = {
    "whisper-ov": "whisper",
    "openvino": "whisper",
    "sv": "sensevoice",
}


@dataclass(frozen=True)
class ResolvedAsr:
    """一个 spec 解出来的东西：哪个 provider、这份配置副本、原始 spec。"""

    provider: ProviderSpec
    settings: Any
    spec: AsrSpec

    def describe(self) -> str:
        return self.spec.display()


class AsrRegistry:
    def __init__(self, settings: Any, logger=None) -> None:
        self.settings = settings
        self.log = logger
        self.providers: dict[str, ProviderSpec] = {}
        self._cache: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._warned: set[str] = set()
        self._discovered = False

    def normalize(self, provider_id: str) -> str:
        """别名归一（``whisper-ov`` → ``whisper``），不做数据库查询。"""
        key = str(provider_id or "").strip().lower()
        return ALIASES.get(key, key)

    # ------------------------------------------------------------------ 注册
    def register(self, provider: ProviderSpec, *, replace: bool = False) -> None:
        pid = provider.id.strip().lower()
        if not pid:
            raise AsrConfigError("provider 的 id 不能为空")
        if pid in self.providers and not replace:
            raise AsrConfigError(f"provider {pid!r} 已经注册过了")
        self.providers[pid] = dataclasses.replace(provider, id=pid)

    def discover(self, package: str = "voice_loop.asr.providers") -> int:
        """扫 `voice_loop/asr/providers/*.py`：每个模块交出 ``PROVIDER`` 就算一个后端。

        ★按需导入★：provider 模块顶层只写声明，sherpa-onnx / openvino / torch
        都在 ``create()`` 里才 import，所以「扫一遍」不会把它们拉进内存。
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
        """`[asr] provider` → 没写就用 sensevoice → 再没有就随便一个。"""
        spec = self.default_spec()
        if spec.provider and spec.provider in self.providers:
            return spec.provider
        if "sensevoice" in self.providers:
            return "sensevoice"
        return next(iter(self.providers), "")

    def provider_for(self, spec: AsrSpec) -> str:
        """这个 spec 落到哪个 provider（空 = 跟全局）。"""
        raw = (spec.provider or "").strip().lower()
        key = ALIASES.get(raw, raw)
        if key and key in self.providers:
            return key
        if raw:
            self._warn(f"识别配置里的 provider = {raw!r} 不认识，改用默认的")
        return self.default_provider()

    # --------------------------------------------------------------- 默认 spec
    def default_spec(self) -> AsrSpec:
        """`[asr]` → spec。

        ★兼容老写法★：``strategy`` 非空时以它为准（老 config.toml / 命令行
        ``--strategy`` 仍然有效），否则读新字段 ``provider/verify/verify_when``。
        两条路最后都变成同一份 :class:`AsrSpec`，下游只认 spec。
        """
        cfg = getattr(self.settings, "asr", None)
        if cfg is None:
            return AsrSpec()
        legacy = str(getattr(cfg, "strategy", "") or "").strip().lower()
        if legacy and legacy not in ("", "auto"):
            got = LEGACY_STRATEGY.get(legacy)
            if got is None:
                self._warn(f"[asr] strategy = {legacy!r} 不认识（认得：{'、'.join(LEGACY_STRATEGY)}）")
            else:
                provider, verify, whens = got
                whens = tuple(
                    w.replace(":6.0", f":{float(getattr(cfg, 'whisper_min_duration', 6.0) or 6.0):g}")
                    if w.startswith(WHEN_LONG) else w
                    for w in whens
                )
                return AsrSpec(
                    provider=provider, verify=verify, verify_when=parse_when(whens),
                    language=str(getattr(cfg, "language", "") or ""),
                    fallback=self._global_fallback(),
                )
        return AsrSpec(
            provider=str(getattr(cfg, "provider", "") or "").strip().lower(),
            language=str(getattr(cfg, "language", "") or ""),
            verify=str(getattr(cfg, "verify", "") or "").strip().lower(),
            verify_when=parse_when(getattr(cfg, "verify_when", ()) or ()),
            fallback=self._global_fallback(),
        )

    def _global_fallback(self) -> tuple[str, ...]:
        raw = getattr(self.settings.asr, "fallback", ()) or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(str(x).strip() for x in raw if str(x).strip())

    # --------------------------------------------------------------- 配置合成
    def effective(self, spec: AsrSpec) -> ResolvedAsr:
        """★把 spec 解成一份**副本**配置★（绝不改全局 settings）。

        优先级（低 → 高）：
        ``AsrConfig`` 默认值 → ``provider.defaults`` → ``[asr.providers.<id>]``
        → ``spec.params``。这样「常驻引擎用 6 线程、复核引擎用 GPU」能同时成立。
        """
        provider = self.get(self.provider_for(spec))
        if provider is None:
            raise AsrConfigError("没有任何可用的识别后端（voice_loop/asr/providers 是空的？）")
        base = getattr(self.settings, "asr", None)
        cfg = dataclasses.replace(base) if dataclasses.is_dataclass(base) else copy.copy(base)
        block = (getattr(self.settings.asr, "providers", {}) or {}).get(provider.id) or {}
        for source, values in (("defaults", provider.defaults), ("config", block),
                               ("params", spec.params)):
            for name, value in dict(values or {}).items():
                if not hasattr(cfg, name):
                    # provider 自己的参数（device / threads…）：允许存在，只是不落 dataclass
                    continue
                try:
                    setattr(cfg, name, _coerce(value, getattr(cfg, name)))
                except (TypeError, ValueError) as exc:
                    self._warn(f"[asr.providers.{provider.id}] {name} 值不对（{exc}），忽略")
        if spec.language:
            cfg.language = spec.language
        if spec.model and hasattr(cfg, "model"):
            cfg.model = spec.model
        settings = _with(self.settings, asr=cfg)
        return ResolvedAsr(provider=provider, settings=settings, spec=spec)

    def options(self, spec: AsrSpec) -> dict:
        """某个 spec 的**私有参数**（provider 默认 → `[asr.providers.<id>]` → spec.params）。"""
        provider = self.get(self.provider_for(spec))
        if provider is None:
            return dict(spec.params or {})
        out = dict(provider.defaults or {})
        out.update((getattr(self.settings.asr, "providers", {}) or {}).get(provider.id) or {})
        out.update(dict(spec.params or {}))
        return out

    # ------------------------------------------------------------------ 降级链
    def chain(self, spec: AsrSpec) -> list[AsrSpec]:
        """这个 spec 的降级顺序（去重）：自己 → spec.fallback → `[asr] fallback` → 一个快的本地引擎。"""
        first = spec.replace(provider=self.provider_for(spec))
        entries = [first]
        for raw in (*spec.fallback, *self._global_fallback()):
            got = AsrSpec.parse(raw)
            if got.provider:
                entries.append(got)
        for pid, provider in self.providers.items():
            if provider.supports(CAP_LOCAL) and provider.fast:
                entries.append(AsrSpec(provider=pid))
                break
        seen: set[str] = set()
        out: list[AsrSpec] = []
        for item in entries:
            key = item.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    # ------------------------------------------------------------- 引擎实例
    def engine(self, provider_id: str, spec: AsrSpec | None = None, *,
               allow_heavy: bool = True) -> Any | None:
        """拿一个能用的引擎；用不了就返回 ``None``（★不抛★：少一个引擎不该炸一轮对话）。

        ``allow_heavy=False`` 时，``heavy`` 后端直接跳过 —— 待唤醒状态靠这条
        保证 Whisper 不会被「顺手」加载（见 pipeline 的两级加载）。
        """
        provider = self.get(provider_id)
        if provider is None:
            self._warn(f"识别后端 {provider_id!r} 没注册")
            return None
        if provider.heavy and not allow_heavy:
            return None
        base = spec or AsrSpec(provider=provider.id)
        candidate = base.replace(provider=provider.id)
        key = (provider.id, candidate.key())
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        ok, why = self._ready(provider)
        if not ok:
            self._warn(f"{provider.id} 用不了（{why}）")
            return None
        resolved = self.effective(candidate)
        try:
            engine = provider.create(resolved.settings, candidate) if provider.create else None
        except Exception as exc:  # noqa: BLE001 - 一个引擎起不来，换个引擎继续
            self._warn(f"{provider.id} 构造失败（{type(exc).__name__}: {exc}）")
            return None
        if engine is None:
            return None
        self._cache[key] = engine
        self._evict()
        self._info(f"[asr] 加载 {provider.id}：{candidate.display()}")
        return engine

    def unload(self, provider_id: str) -> bool:
        """卸掉某个后端的**所有**实例（``heavy`` 回到待唤醒时用）。"""
        pid = self.normalize(provider_id)
        gone = False
        for key in [k for k in self._cache if k[0] == pid]:
            engine = self._cache.pop(key)
            self._closing(engine)
            gone = True
        return gone

    def loaded(self) -> list[str]:
        return sorted({pid for pid, _ in self._cache})

    def is_loaded(self, provider_id: str) -> bool:
        pid = self.normalize(provider_id)
        return any(k[0] == pid for k in self._cache)

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
        limit = max(1, int(getattr(self.settings.asr, "max_loaded", 2) or 2))
        while len(self._cache) > limit:
            key, engine = self._cache.popitem(last=False)
            self._closing(engine)
            self._info(f"[asr] 卸载（缓存到上限 {limit}）：{key[0]}")

    # ------------------------------------------------------------------ 自检
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

    def missing(self, spec: AsrSpec | None = None) -> list[str]:
        """这条 spec（主引擎 + 复核 + 兜底）还缺哪些文件 —— 给 require_models / doctor 用。"""
        spec = spec or self.default_spec()
        ids: list[str] = [self.provider_for(spec)]
        if spec.verify:
            ids.append(self.provider_for(spec.replace(provider=spec.verify)))
        ids.extend(self.provider_for(AsrSpec(provider=x)) for x in spec.fallback)
        out: list[str] = []
        seen: set[str] = set()
        for pid in ids:
            if pid in seen:
                continue
            seen.add(pid)
            provider = self.get(pid)
            if provider is None or provider.missing is None:
                continue
            try:
                out.extend(str(x) for x in (provider.missing(self.settings) or []))
            except Exception as exc:  # noqa: BLE001
                self._warn(f"{pid} 的缺文件检查失败：{exc}")
        return out

    def describe(self) -> list[dict]:
        """给控制台 / 启动横幅用：每个后端的能力 + 该不该常驻。"""
        spec = self.default_spec()
        out = []
        for provider in self.providers.values():
            item = provider.as_dict()
            item["ready"], item["why"] = self._ready(provider)
            item["primary"] = provider.id == self.provider_for(spec)
            item["verify"] = bool(spec.verify) and provider.id == self.provider_for(
                spec.replace(provider=spec.verify))
            item["loaded"] = self.is_loaded(provider.id)
            out.append(item)
        return out

    def summary(self) -> str:
        """一行字：``sensevoice(常驻) + whisper(按需)``。"""
        spec = self.default_spec()
        bits = []
        primary = self.get(self.provider_for(spec))
        if primary is not None:
            bits.append(primary.id + ("(常驻)" if not primary.heavy else ""))
        if spec.verify:
            verifier = self.get(self.provider_for(spec.replace(provider=spec.verify)))
            if verifier is not None:
                bits.append(verifier.id + ("(按需)" if verifier.heavy else ""))
        return " + ".join(bits) or "未配置"

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
            self.log.warning(f"[asr] {line}")
        else:
            print(f"[asr] {line}", flush=True)


def _with(settings: Any, **changes: Any) -> Any:
    """造一份改了值的配置副本（★真 settings 是 dataclass，自测里是轻壳子★）。"""
    if dataclasses.is_dataclass(settings):
        return dataclasses.replace(settings, **changes)
    clone = copy.copy(settings)
    for key, value in changes.items():
        setattr(clone, key, value)
    return clone


def _coerce(value: Any, hint: Any) -> Any:
    """按当前值的类型把配置里的值转成对的类型（TOML 里 "6" 也要认）。"""
    if isinstance(hint, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if isinstance(hint, int) and not isinstance(hint, bool):
        return int(value)
    if isinstance(hint, float):
        return float(value)
    if isinstance(value, (dict, list)):
        return value
    return str(value).strip()


def iter_providers(registry: AsrRegistry) -> Iterator[ProviderSpec]:
    yield from registry.providers.values()


__all__ = ["ALIASES", "AsrRegistry", "LEGACY_STRATEGY", "ResolvedAsr", "iter_providers"]
