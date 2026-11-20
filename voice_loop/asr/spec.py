"""ASR 的两份「描述」：怎么听这一句（:class:`AsrSpec`）、一个后端怎么接（:class:`ProviderSpec`）。

为什么要有这两份数据（对应 TTS 的 ``tts/spec.py``）
    以前「用哪个识别引擎」是靠 ``strategy`` + ``if name == "sensevoice"`` 这类名字判断，
    加一个后端要同时改 `router._engine` / `router._wanted_engines` / `AsrConfig` /
    `pipeline`（``lazy_whisper``、``prefer="sensevoice"``）/ `main.require_models` /
    控制台 —— 而且**词库和唤醒判定根本插不进去**（引擎名字一硬编码，就只能事后猜）。

    ★关键差别（跟 TTS 不一样的地方）★：ASR 的价值不只是"换个引擎"，而是
    **「快路径 + 按需复核」这个策略**。所以策略必须变成数据（:attr:`AsrSpec.verify`
    / :attr:`AsrSpec.verify_when`），跟着引擎名一起搬走就会丢掉它。

两个数据类
    :class:`AsrSpec`      —— 配置里写的「怎么听」：主引擎 + 复核引擎 + 复核条件 + 兜底
    :class:`ProviderSpec` —— 后端自己交上来的：能力、参数表、缺什么、怎么造引擎

★能力不是名字★：``heavy``（要不要两级加载）、``fast``（能不能当回听校验的快路径）、
``hotwords``（词库怎么喂给它）。所有分支都查能力，不再查 id。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# --------------------------------------------------------------------------- #
# 能力与词库模式
# --------------------------------------------------------------------------- #
CAP_LOCAL = "local"          # 在这台机器上算（不联网）
CAP_CLOUD = "cloud"          # ★会把音频发到网上★（隐私不变量靠它守）
CAP_HEAVY = "heavy"          # 很大 / 首次编译很慢 → 走两级加载（待唤醒时不常驻）
CAP_FAST = "fast"            # 够快，能当「合成回听」这类内部校验的快路径
CAP_PARTIAL = "partial"      # 能边听边出字（现在没人实现，留占位）

CAPABILITIES = (CAP_LOCAL, CAP_CLOUD, CAP_HEAVY, CAP_FAST, CAP_PARTIAL)

# 词库怎么喂给这个引擎（决定 Lexicon 走哪条路）
HOTWORDS_NATIVE = "native"   # 引擎自己有热词表（zipformer / paraformer）
HOTWORDS_PROMPT = "prompt"   # 只能塞进提示词（whisper 的 initial_prompt）
HOTWORDS_POST = "post"       # 引擎无能为力，只能事后纠正（SenseVoice 就是这种）
HOTWORDS_NONE = "none"       # 连事后都不做（例如纯测试用的假引擎）

#: 复核条件的关键字（``AsrSpec.verify_when`` 里写的字符串）
WHEN_EMPTY = "empty"         # 主引擎什么都没听出来
WHEN_LONG = "long"           # 音频比 ``long:<秒>`` 长
WHEN_SCORE = "score"         # 质量分低于 ``score<0.45``
WHEN_ALWAYS = "always"       # 总是复核（调试用）


class AsrConfigError(ValueError):
    """识别配置写错了（config.toml 里的 [asr]）。"""


# --------------------------------------------------------------------------- #
# 「怎么听」
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class When:
    """一条复核条件。``kind`` 是上面那几个关键字，``value`` 是阈值（可空）。"""

    kind: str
    value: float = 0.0

    def as_text(self) -> str:
        if self.kind == WHEN_SCORE:
            return f"{self.kind}<{self.value:g}"
        return self.kind if not self.value else f"{self.kind}:{self.value:g}"

    def hit(self, *, empty: bool, seconds: float, score: float) -> bool:
        if self.kind == WHEN_EMPTY:
            return empty
        if self.kind == WHEN_LONG:
            return seconds >= self.value
        if self.kind == WHEN_SCORE:
            return score < self.value
        if self.kind == WHEN_ALWAYS:
            return True
        return False


def parse_when(raw: Any) -> tuple[When, ...]:
    """把配置里写的条件解析成 :class:`When`。

    认 ``"empty"`` / ``"long:6.0"`` / ``"score<0.45"`` / ``"always"``，
    也认 list（多个条件是"或"的关系）。写坏了**不抛**，只忽略那一条 ——
    识别配置坏掉不该让人一句话都说不出来。
    """
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, Mapping):
        # 也认 {"long": 6.0, "score<": 0.45} 这种写法
        items = [f"{k}:{v}" for k, v in raw.items()]
    else:
        items = list(raw)
    out: list[When] = []
    for item in items:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if "<" in text:                                  # score<0.45
            left, _, right = text.partition("<")
            try:
                out.append(When(left.strip() or WHEN_SCORE, float(right)))
            except ValueError:
                continue
            continue
        if ":" in text:                                  # long:6.0
            left, _, right = text.partition(":")
            try:
                out.append(When(left.strip(), float(right)))
            except ValueError:
                out.append(When(left.strip()))
            continue
        if text in (WHEN_EMPTY, WHEN_LONG, WHEN_SCORE, WHEN_ALWAYS):
            out.append(When(text))
    return tuple(out)


@dataclass(frozen=True)
class AsrSpec:
    """「怎么听这一句」。空字段 = 跟上一层（``[asr]`` 或 provider 默认值）。"""

    provider: str = ""                        # 主引擎；空 = 跟 [asr] provider
    language: str = ""                        # zh / en / auto；空 = 跟 [asr] language
    model: str = ""                           # 模型目录或名字（provider 自己解释）
    verify: str = ""                          # 复核引擎；空 = 不复核
    verify_when: tuple[When, ...] = ()         # 什么情况下复核
    fallback: tuple[str, ...] = ()             # 主引擎起不来时的兜底顺序
    params: Mapping[str, Any] = field(default_factory=dict)   # provider 私有参数
    note: str = ""                            # 给人看的备注，不参与键

    # ------------------------------------------------------------------ 序列化
    def as_dict(self) -> dict:
        """写回配置时的样子：★空的字段不写★。"""
        out: dict[str, Any] = {}
        for key in ("provider", "language", "model", "verify", "note"):
            value = str(getattr(self, key) or "").strip()
            if value:
                out[key] = value
        if self.verify_when:
            out["verify_when"] = [w.as_text() for w in self.verify_when]
        if self.params:
            out["params"] = dict(self.params)
        if self.fallback:
            out["fallback"] = list(self.fallback)
        return out

    def key(self) -> str:
        """缓存键 / 比较键：不含 ``note``（换备注不用重建引擎）。"""
        return json.dumps(self._key_payload(), ensure_ascii=False, sort_keys=True)

    def _key_payload(self) -> dict:
        return {
            "provider": self.provider.strip().lower(),
            "language": self.language.strip().lower(),
            "model": self.model.strip(),
            "verify": self.verify.strip().lower(),
            "verify_when": [w.as_text() for w in self.verify_when],
            "params": dict(sorted((self.params or {}).items())),
            "fallback": list(self.fallback),
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AsrSpec) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def replace(self, **kwargs) -> AsrSpec:
        return replace(self, **kwargs)

    def display(self) -> str:
        """一行字，给日志 / 启动横幅 / 控制台看。"""
        bits = [self.provider or "跟全局"]
        if self.language:
            bits.append(self.language)
        if self.verify:
            whens = "、".join(w.as_text() for w in self.verify_when) or "总是"
            bits.append(f"复核 {self.verify}（{whens}）")
        return " / ".join(bits)

    # -------------------------------------------------------------------- 解析
    @classmethod
    def parse(cls, raw: Any) -> AsrSpec:
        """把配置里的写法解析成 AsrSpec。

        认三种：``None``/``""``/``{}`` → 空 spec（跟全局）；
        字符串 → ``"provider"``（也可以 ``"provider:model"``）；
        字典 → 直接读（未知键忽略，``params`` 原样保留）。
        """
        if raw is None or raw == "" or raw == {}:
            return cls()
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return cls()
            provider, _, model = text.partition(":")
            return cls(provider=provider.strip().lower(), model=model.strip())
        if not isinstance(raw, Mapping):
            raise AsrConfigError(f"识别配置应该是字符串或对象，现在是 {type(raw).__name__}")

        def text_of(key: str) -> str:
            return str(raw.get(key) or "").strip()

        params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
        fallback_raw = raw.get("fallback") or ()
        if isinstance(fallback_raw, str):
            fallback_raw = [fallback_raw]
        return cls(
            provider=text_of("provider").lower(),
            language=text_of("language"),
            model=text_of("model"),
            verify=text_of("verify").lower(),
            verify_when=parse_when(raw.get("verify_when")),
            fallback=tuple(str(x).strip() for x in fallback_raw if str(x).strip()),
            params=dict(params),
            note=text_of("note"),
        )


# --------------------------------------------------------------------------- #
# Provider 的自描述
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldSpec:
    """一个 provider 参数（控制台表单、校验、默认值都从这里来）。"""

    name: str
    label: str = ""
    kind: str = "text"        # text | number | bool | select | json | path
    default: Any = None
    options: tuple[str, ...] = ()
    hint: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label or self.name, "kind": self.kind,
                "default": self.default, "options": list(self.options), "hint": self.hint}


@dataclass(frozen=True)
class ProviderSpec:
    """一个识别后端怎么描述自己（注册表就靠它工作）。"""

    id: str
    label: str
    capabilities: frozenset[str] = frozenset()
    # 词库怎么喂（native / prompt / post / none，见 HOTWORDS_*）
    hotwords: str = HOTWORDS_POST
    fields: tuple[FieldSpec, ...] = ()
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # 缺模型 / 缺库的判定：返回 (能不能用, 为什么不能)
    ready: Callable[[Any], tuple[bool, str]] | None = None
    # 造引擎：settings 是**这一份配置的副本**（不是全局那份）
    create: Callable[[Any, AsrSpec], Any] | None = None
    # 缺哪些文件（给 require_models / doctor 用）
    missing: Callable[[Any], list[str]] | None = None
    hint: str = ""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def cloud(self) -> bool:
        return CAP_CLOUD in self.capabilities

    @property
    def heavy(self) -> bool:
        return CAP_HEAVY in self.capabilities

    @property
    def fast(self) -> bool:
        return CAP_FAST in self.capabilities

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "cloud": self.cloud,
            "heavy": self.heavy, "fast": self.fast, "hotwords": self.hotwords,
            "capabilities": sorted(self.capabilities),
            "fields": [f.as_dict() for f in self.fields],
            "hint": self.hint,
        }


__all__ = [
    "AsrConfigError", "AsrSpec", "CAPABILITIES", "CAP_CLOUD", "CAP_FAST", "CAP_HEAVY",
    "CAP_LOCAL", "CAP_PARTIAL", "FieldSpec", "HOTWORDS_NATIVE", "HOTWORDS_NONE",
    "HOTWORDS_POST", "HOTWORDS_PROMPT", "ProviderSpec", "WHEN_ALWAYS", "WHEN_EMPTY",
    "WHEN_LONG", "WHEN_SCORE", "When", "parse_when",
]
