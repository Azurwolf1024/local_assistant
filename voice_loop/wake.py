"""唤醒词匹配。

唤醒词存放在 ``data/wakewords.json``，可以随时用编辑器改，程序会在每次
监听循环时自动检测文件变化并重新加载（不需要重启）。

文件格式
    {
      "enabled": true,
      "words": ["小助手", "你好助手"],        // 主唤醒词
      "aliases": {                            // 语音识别常见的听错写法
        "小助手": ["小主手", "小主受", "小厨师", "小竹手"],
        "你好助手": ["你好小助手"]
      },
      "ack": "在的",                          // 唤醒后的应答，留空则不播报
      "idle_timeout": 180,                  // 唤醒后多少秒没有指令就回到待唤醒
      "min_silence": 0.30,                    // 待唤醒时的静音判定时长（秒），越短越灵敏
      "fuzzy_ratio": 0.75                     // 模糊匹配阈值，越小越宽松
    }
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_PUNCT = re.compile(r"[\s，。、；：！？,.!?;:\"'“”‘’()（）\[\]【】~—\-]")
_LATIN = re.compile(r"[a-zA-Z]+")

DEFAULT_CONFIG: dict = {
    "_说明": "唤醒词配置。改完保存即可生效，程序会自动重新加载。",
    "enabled": True,
    "words": ["凯尔希"],
    "aliases": {
        "凯尔希": ["凯尔西", "凯尔茜", "凯尔惜", "凯尔锡", "凯尔溪", "凯尔熙", "凯尔兮", "凯儿希", "开尔希", "卡尔希"],
    },
    "ack": "在的",
    "idle_timeout": 180,
    "min_silence": 0.3,
    "fuzzy_ratio": 0.75,
}


def normalize(text: str) -> str:
    """去掉空格、标点，统一小写，方便比对。"""
    return _PUNCT.sub("", (text or "")).lower()


@dataclass
class WakeHit:
    word: str = ""          # 命中的唤醒词
    remainder: str = ""     # 唤醒词之后的内容（同一句里直接说了请求时非空）
    fuzzy: bool = False     # 是否是模糊匹配命中


@dataclass
class WakeSettings:
    enabled: bool = True
    words: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    ack: str = "在的"
    idle_timeout: float = 0.0     # 0 表示 json 里没写，用 config.toml 的值
    min_silence: float = 0.3
    fuzzy_ratio: float = 0.75


class WakeWordMatcher:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mtime: float = 0.0
        self._settings = WakeSettings()
        self._patterns: list[tuple[str, list[str]]] = []  # (主唤醒词, [所有变体])
        self.load()

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def write_default(path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load(self, force: bool = False) -> WakeSettings:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if not force and mtime and mtime == self._mtime:
            return self._settings

        if not self.path.exists():
            self.write_default(self.path)
            mtime = self.path.stat().st_mtime

        raw: dict = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            print(f"[wake] {self.path.name} 解析失败（{exc}），沿用上一次的配置")

        words = [str(w).strip() for w in raw.get("words", DEFAULT_CONFIG["words"]) if str(w).strip()]
        aliases_raw = raw.get("aliases", DEFAULT_CONFIG["aliases"]) or {}
        aliases = {
            str(k).strip(): [str(v).strip() for v in (vals or []) if str(v).strip()]
            for k, vals in aliases_raw.items()
        }
        self._settings = WakeSettings(
            enabled=bool(raw.get("enabled", True)),
            words=words,
            aliases=aliases,
            ack=str(raw.get("ack", DEFAULT_CONFIG["ack"])),
            # 兼容旧字段名 session_timeout；为 0 时交给 config.toml 决定
            idle_timeout=float(
                raw.get("idle_timeout", raw.get("session_timeout", 0)) or 0
            ),
            min_silence=float(raw.get("min_silence", DEFAULT_CONFIG["min_silence"])),
            fuzzy_ratio=float(raw.get("fuzzy_ratio", DEFAULT_CONFIG["fuzzy_ratio"])),
        )
        self._patterns = []
        for word in self._settings.words:
            variants = [word] + list(self._settings.aliases.get(word, []))
            norm_variants = sorted({normalize(v) for v in variants if normalize(v)}, key=len, reverse=True)
            self._patterns.append((word, norm_variants))
        self._mtime = mtime
        return self._settings

    def maybe_reload(self) -> bool:
        """文件被改过就重新加载；返回「内容是否真的变了」。"""
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            return False
        if not mtime or mtime == self._mtime:
            return False
        before = (tuple(self._settings.words), self._settings.ack, self._settings.idle_timeout)
        self.load(force=True)
        after = (tuple(self._settings.words), self._settings.ack, self._settings.idle_timeout)
        return before != after

    @property
    def settings(self) -> WakeSettings:
        return self._settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled and bool(self._patterns)

    # ------------------------------------------------------------------ 匹配
    def match(self, text: str) -> WakeHit | None:
        """判断这句话里有没有唤醒词。"""
        if not self.enabled:
            return None
        norm = normalize(text)
        if not norm:
            return None

        # 1) 精确 / 别名包含
        for word, variants in self._patterns:
            for variant in variants:
                idx = norm.find(variant)
                if idx >= 0:
                    return WakeHit(word=word, remainder=text.strip(), fuzzy=False)

        # 2) 模糊匹配：滑窗逐段比对
        #    窗口最小长度取 主唤醒词长度-1（但不能少于 3），
        #    否则「凯尔希」这种 3 字词会被任意两字窗口误命中。
        ratio = self._settings.fuzzy_ratio
        for word, _ in self._patterns:
            for variant in [normalize(word)]:
                if not variant:
                    continue
                min_size = max(3, len(variant) - 1) if len(variant) >= 3 else len(variant)
                for size in range(min_size, len(variant) + 2):
                    if size > len(norm):
                        break
                    for i in range(0, len(norm) - size + 1):
                        window = norm[i : i + size]
                        if difflib.SequenceMatcher(None, window, variant).ratio() >= ratio:
                            return WakeHit(word=word, remainder=text.strip(), fuzzy=True)
        return None

    def strip_word(self, text: str, hit: WakeHit | None = None) -> str:
        """去掉文本里的唤醒词，返回剩下的请求内容。"""
        if hit is None:
            hit = self.match(text)
        if hit is None:
            return text.strip()
        raw = text or ""
        norm = normalize(raw)
        for _, variants in self._patterns:
            for variant in variants:
                idx = norm.find(variant)
                if idx < 0:
                    continue
                # norm 与 raw 的下标可能不同（标点/空格），用长度差粗略换算
                approx = min(idx, len(raw))
                for cut in range(approx, -1, -1):
                    if normalize(raw[cut:]).startswith(variant):
                        return raw[:cut].strip() or raw[cut + len(variant) :].strip()
        # 模糊命中：无法精确定位，直接返回原文
        return raw.strip()


class WakeSession:
    """唤醒后的会话窗口：窗口内不需要再说唤醒词。"""

    def __init__(self, timeout: float) -> None:
        self.timeout = max(1.0, float(timeout))
        self._until: float = 0.0

    @property
    def active(self) -> bool:
        return time.monotonic() < self._until

    @property
    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    def open(self) -> None:
        self._until = time.monotonic() + self.timeout

    def close(self) -> None:
        self._until = 0.0
