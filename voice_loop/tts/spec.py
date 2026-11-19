"""TTS 的两份「描述」：一个声音是什么（:class:`VoiceSpec`）、一个后端怎么接（:class:`ProviderSpec`）。

★为什么要这两份数据★：以前「用哪个后端 / 哪把嗓子」是靠 `if backend == "piper"` 这类
名字判断 + 往 `settings.tts.*` 写字段实现的。加一个新模型要同时改四处
（`tts/__init__.py` 的 BACKENDS、`pipeline._apply_voice`、`settings.TtsConfig`、控制台表单），
而且同一次运行里两个角色没法各自持有不同配置（只能改全局再 unload）。

现在分成两层数据：

- :class:`VoiceSpec` —— **角色卡里存的东西**：provider + 音色 + 参考音频 + 私有参数。
  它是「我要谁的声音」，跟后端怎么实现无关。
- :class:`ProviderSpec` —— **后端自己交上来的东西**：能力声明、参数表、健康检查、构造函数。
  加后端 = 加一个文件（`voice_loop/tts/providers/xxx.py`），别的地方一行不用改。

`VoiceSpec` 是 dataclass 而不是 dict 的原因：它要能当缓存键（`key()`）、要能比相等，
而且写进人格文件时得稳定（`as_dict()` 只写非空字段，空的不落盘）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# 能力名。★别用后端名字做判断★：所有分支都走这里。
CAP_CLONE = "clone"          # 支持「一段参考音频 + 逐字文本」的音色克隆
CAP_VOICE_ID = "voice_id"    # 有一组官方音色 id 可选（stella / zh-CN-XiaoxiaoNeural …）
CAP_SPEED = "speed"          # 支持语速
CAP_STREAM = "stream"        # 边合成边吐音频（否则是整段回来再播）
CAP_CLOUD = "cloud"          # ★会把文本发到网上★（隐私不变量靠它守）
CAP_STYLE = "style"          # 支持按风格换参考音（多档参考）

CAPABILITIES = (CAP_CLONE, CAP_VOICE_ID, CAP_SPEED, CAP_STREAM, CAP_CLOUD, CAP_STYLE)


class TtsConfigError(ValueError):
    """声音描述写错了（人格文件 / 配置里）。"""


@dataclass(frozen=True)
class VoiceSpec:
    """「谁的声音」。空字段 = 跟着上一层（`[tts]` 或 provider 自己的默认值）。"""

    provider: str = ""                       # 后端 id；空 = 跟 [tts] provider
    voice_id: str = ""                       # 官方音色 / piper 声线名；空 = provider 默认
    model: str = ""                          # 模型目录或名字（provider 自己解释）
    ref_audio: str = ""                      # 克隆参考音频
    ref_text: str = ""                       # 参考音频的逐字文本
    params: Mapping[str, Any] = field(default_factory=dict)   # provider 私有参数
    fallback: tuple[str, ...] = ()           # ("piper",) 或 ("piper:zh_CN-huayan-medium",)
    note: str = ""                           # 给人看的备注，不参与键

    # ------------------------------------------------------------------ 序列化
    def as_dict(self) -> dict:
        """写进人格文件的样子：★空的字段不写★（文件干净，人也好改）。"""
        out: dict[str, Any] = {}
        for key in ("provider", "voice_id", "model", "ref_audio", "ref_text", "note"):
            value = getattr(self, key)
            if str(value or "").strip():
                out[key] = str(value).strip()
        if self.params:
            out["params"] = dict(self.params)
        if self.fallback:
            out["fallback"] = list(self.fallback)
        return out

    def key(self) -> str:
        """缓存键 / 比较键：不含 ``note``（它只是备注，换了也不用重建引擎）。"""
        return json.dumps(self._key_payload(), ensure_ascii=False, sort_keys=True)

    def _key_payload(self) -> dict:
        return {
            "provider": self.provider.strip().lower(),
            "voice_id": self.voice_id.strip(),
            "model": self.model.strip(),
            "ref_audio": self.ref_audio.strip(),
            "ref_text": self.ref_text.strip(),
            "params": dict(sorted((self.params or {}).items())),
            "fallback": list(self.fallback),
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VoiceSpec) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def replace(self, **kwargs) -> VoiceSpec:
        return replace(self, **kwargs)

    def display(self) -> str:
        """一行字，给日志 / 控制台看。"""
        bits = [self.provider or "跟全局"]
        if self.voice_id:
            bits.append(self.voice_id)
        if self.model:
            bits.append("模型 " + self.model.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        if self.ref_audio:
            bits.append("参考 " + self.ref_audio.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        return " / ".join(bits)

    # -------------------------------------------------------------------- 解析
    @classmethod
    def parse(cls, raw: Any) -> VoiceSpec:
        """把人格文件 / 配置里的写法解析成 VoiceSpec。

        认三种：
        - ``None`` / ``""`` / ``{}`` → 空 spec（跟全局）
        - 字符串 → ``"provider"`` 或 ``"provider:voice_id"``
        - 字典 → 直接读（未知键忽略，`params` 原样保留）

        也认 ``"audio"``/``"text"`` 这两个短名（写在 ``ref`` 子字典里更清楚，
        但手写时 ``{"ref": {"audio": "...", "text": "..."}}`` 和
        ``{"ref_audio": "...", "ref_text": "..."}`` 都收）。
        """
        if raw is None or raw == "" or raw == {}:
            return cls()
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return cls()
            provider, _, voice = text.partition(":")
            return cls(provider=provider.strip().lower(), voice_id=voice.strip())
        if not isinstance(raw, Mapping):
            raise TtsConfigError(f"声音配置应该是字符串或对象，现在是 {type(raw).__name__}")

        def text_of(key: str) -> str:
            return str(raw.get(key) or "").strip()

        ref = raw.get("ref") if isinstance(raw.get("ref"), Mapping) else {}
        params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
        fallback_raw = raw.get("fallback") or ()
        if isinstance(fallback_raw, str):
            fallback_raw = [fallback_raw]
        fallback = tuple(str(x).strip() for x in fallback_raw if str(x).strip())
        voice = text_of("voice_id") or text_of("voice")
        return cls(
            provider=text_of("provider").lower(),
            voice_id=voice,
            model=text_of("model"),
            ref_audio=text_of("ref_audio") or str(ref.get("audio") or "").strip(),
            ref_text=text_of("ref_text") or str(ref.get("text") or "").strip(),
            params=dict(params),
            fallback=fallback,
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
    # 写到 TtsConfig 的哪个字段（默认同名）。要写进 [api] 这种别的 section 时，
    # 由 provider 自己的 apply() 处理，不在这里硬塞。
    target: str = ""

    def field_name(self) -> str:
        return self.target or self.name

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label or self.name, "kind": self.kind,
                "default": self.default, "options": list(self.options), "hint": self.hint}


@dataclass(frozen=True)
class ProviderSpec:
    """一个 TTS 后端怎么描述自己（注册表就靠它工作）。"""

    id: str
    label: str
    capabilities: frozenset[str] = frozenset()
    fields: tuple[FieldSpec, ...] = ()
    # 「在 settings.tts 拷贝上」先打的底（provider 自己的默认值，优先级最低）
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # 缺模型 / 缺 key 的判定：返回 (能不能用, 为什么不能)
    ready: Callable[[Any], tuple[bool, str]] | None = None
    # 造引擎：settings 是**这一把嗓子的副本**（不是全局那份）
    create: Callable[[Any, VoiceSpec], Any] | None = None
    # 把 VoiceSpec 映射到 settings（voice_id → voice / api.tts_voice，ref → clone_audio…）
    apply: Callable[[Any, VoiceSpec], Any] | None = None
    # 可选音色列表（控制台下拉）；拿不到就返回空
    voices: Callable[[Any], list[str]] | None = None
    hint: str = ""
    # 引擎加载好后要不要注入「退路」工厂（云端后端中途失败时用）
    wants_fallback_factory: bool = False

    @property
    def cloud(self) -> bool:
        return CAP_CLOUD in self.capabilities

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label,
            "cloud": self.cloud,
            "capabilities": sorted(self.capabilities),
            "fields": [f.as_dict() for f in self.fields],
            "voices": [],
            "hint": self.hint,
        }


__all__ = [
    "CAPABILITIES", "CAP_CLONE", "CAP_CLOUD", "CAP_SPEED", "CAP_STREAM", "CAP_STYLE",
    "CAP_VOICE_ID", "FieldSpec", "ProviderSpec", "TtsConfigError", "VoiceSpec",
]
