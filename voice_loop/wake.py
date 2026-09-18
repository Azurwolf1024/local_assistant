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
        # 真人说话常被听成这些；后面几个「开儿戏 / 胎儿西 / 台儿西」是
        # 用 TTS 合成的「凯尔希」（scripts/say.py 那种用法）被听成的样子。
        "凯尔希": [
            "凯尔西", "凯尔茜", "凯尔惜", "凯尔锡", "凯尔溪", "凯尔熙", "凯尔兮", "凯儿希",
            "凯尔信", "凯尔心", "凯尔新", "凯尔辛", "凯尔席", "凯尔戏", "卡尔希", "卡尔西",
            "开尔希", "开尔信", "开尔心", "开尔新", "开尔西", "开尔辛", "开尔戏", "开儿戏",
            "太尔希", "太尔西", "太儿西", "台儿西", "泰尔希", "泰尔西", "泰儿西",
            "海尔西", "胎儿西", "胎儿戏",
        ],
    },
    "ack": "在的",
    "idle_timeout": 180,
    "min_silence": 0.3,
    "fuzzy_ratio": 0.75,
}


def _scan(text: str) -> tuple[str, list[int]]:
    """归一化，同时记下每个归一化字符在**原文**里的下标。

    有了这个映射，才能在原文里精确地切掉唤醒词（否则下标对不上，
    一旦原文里有标点/空格就会切错地方）。
    """
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text or ""):
        if _PUNCT.fullmatch(ch):
            continue
        c = ch.lower()
        if len(c) != 1:      # 少数语言里 lower() 会变长，只取首字符
            c = c[0]
        out.append(c)
        pos.append(i)
    return "".join(out), pos


def normalize(text: str) -> str:
    """去掉空格、标点，统一小写，方便比对。"""
    return _scan(text)[0]


# 唤醒词前后常见的口头禅：「那个凯尔西」不应该把「那个」当成请求
_EDGE_FILLER = (
    "那个", "这个", "那", "这", "哎", "呃", "嗯", "啊", "喂", "嘿", "嗨", "诶", "哦", "呀",
)
_EDGE_PUNCT = " \u3000\t\r\n，。、；：！？,.!?;:…~—-·"


def _trim(text: str) -> str:
    """剥掉两端的标点和口头禅。"""
    t = (text or "").strip(_EDGE_PUNCT)
    changed = True
    while changed and t:
        changed = False
        for w in _EDGE_FILLER:
            if t.startswith(w):
                t = t[len(w) :].strip(_EDGE_PUNCT)
                changed = True
                break
    return t.strip()


# 「收回唤醒」的整句判断：只认这几个语气词，别的字一概不算
_STANDBY_TAIL = frozenset({"了", "啦", "啊", "呀", "哈", "哦", "噢", "嗯", "谢谢", "多谢", "好了"})
_STANDBY_HEAD = frozenset({"那", "哦", "噢", "嗯", "唔", "呃", "好", "好的", "行", "我", "就"})


def _plain(text: str) -> str:
    """去标点 + 剥两端口头禅，用于整句判断。"""
    return _trim(normalize(text))


def is_standby(text: str, phrases: list[str] | None, slack: int = 3) -> bool:
    """这句话是不是「收回唤醒」（「没事了」这类）？

    要求**整句**就是那句话，只允许前后多一个语气词（「那没事了」「没事了，谢谢」）。
    故意不像 ``exit_phrases`` 那样「包含就算」：那样「他没事了」「没事吧」都会误判。
    返回 True 时调用方应该关掉这次会话，回到待唤醒。
    """
    t = _plain(text)
    if len(t) < 2:
        return False
    for raw in phrases or ():
        p = _plain(str(raw))
        if len(p) < 2 or not (len(p) <= len(t) <= len(p) + slack):
            continue
        if t == p:
            return True
        if t.startswith(p) and t[len(p) :] in _STANDBY_TAIL:
            return True
        if t.endswith(p) and t[: len(t) - len(p)] in _STANDBY_HEAD:
            return True
    return False


@dataclass
class WakeHit:
    word: str = ""                 # 命中的唤醒词
    remainder: str = ""            # 唤醒词之后的内容（同一句里直接说了请求时非空）
    fuzzy: bool = False            # 是否是模糊匹配命中
    span: tuple[int, int] | None = None   # 命中区间在**原文**里的下标 [起, 止)


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
        norm, raw_pos = _scan(text)
        if not norm:
            return None

        def hit_of(word: str, a: int, b: int, fuzzy: bool) -> WakeHit:
            span = (raw_pos[a], raw_pos[b] + 1)
            return WakeHit(
                word=word,
                remainder=(text or "")[span[1] :].strip(),
                fuzzy=fuzzy,
                span=span,
            )

        # 1) 精确 / 别名包含（variants 已按长度倒序，长的优先）
        for word, variants in self._patterns:
            for variant in variants:
                idx = norm.find(variant)
                if idx >= 0:
                    return hit_of(word, idx, idx + len(variant) - 1, False)

        # 2) 模糊匹配：滑窗逐段比对，取最像的那一段
        #    窗口最小长度取 主唤醒词长度-1（但不能少于 3），
        #    否则「凯尔希」这种 3 字词会被任意两字窗口误命中。
        ratio, word, a, b = self._best_window(norm)
        if ratio >= self._settings.fuzzy_ratio:
            return hit_of(word, a, b, True)
        return None

    def _best_window(self, norm: str) -> tuple[float, str, int, int]:
        """在 norm 里找与唤醒词最像的窗口，返回 (相似度, 主唤醒词, 起, 止)。"""
        best: tuple[float, str, int, int] = (0.0, "", 0, 0)
        for word, _ in self._patterns:
            variant = normalize(word)
            if not variant:
                continue
            min_size = max(3, len(variant) - 1) if len(variant) >= 3 else len(variant)
            for size in range(min_size, len(variant) + 2):
                if size > len(norm):
                    break
                for i in range(0, len(norm) - size + 1):
                    window = norm[i : i + size]
                    r = difflib.SequenceMatcher(None, window, variant).ratio()
                    if r > best[0]:
                        best = (r, word, i, i + size - 1)
        return best

    def best_ratio(self, text: str) -> float:
        """这句话与唤醒词最接近的相似度（0~1）。

        和 :meth:`match` 用同一套滑窗，所以「相似度 ≥ fuzzy_ratio 却没命中」
        这种情况不会出现，提示用户调阈值时不会自相矛盾。
        """
        norm = normalize(text)
        return self._best_window(norm)[0] if norm else 0.0

    def strip_word(self, text: str, hit: WakeHit | None = None) -> str:
        """去掉唤醒词，返回剩下的「请求内容」。

        汉语的习惯是「名字 + 请求」（「凯尔希，现在几点了」），所以**后半句优先**；
        只有后半句为空时才看前半句（「现在几点了凯尔希」这种倒装）。
        两边都只是口头禅（「那个凯尔西」）就返回空，免得把「那个」当成请求丢给大模型。
        """
        if hit is None:
            hit = self.match(text)
        raw = (text or "").strip()
        if hit is None or hit.span is None:
            return raw
        start, end = hit.span
        tail = _trim(raw[end:])
        if tail:
            return tail
        return _trim(raw[:start])


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
