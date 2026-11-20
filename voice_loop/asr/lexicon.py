"""词库（Lexicon）：★一份数据，四种消费方式★。

为什么单独一层
    识别准确率最便宜的那部分提升来自**告诉引擎"这台机器上会出现哪些词"**：
    角色名（白泽/凯尔希/克莱门莎）、唤醒词别名、领域术语。以前只有一个
    `names.py` 的拼音近似纠错（事后猜），引擎拿不到任何提示 —— 而「白泽」被听成
    「白则」这种事，只有把词喂进去才能从源头减少。

四种消费方式（怎么用由后端能力决定，见 `ProviderSpec.hotwords`）
    1. ``native``  引擎自带热词表（transducer）：生成 ``hotwords.txt`` 交给它
    2. ``prompt``  只能塞提示词（whisper）：生成一句 ``initial_prompt``
    3. ``post``    引擎无能为力（sensevoice）：事后用拼音近似纠正
    4. 唤醒匹配      `WakeWordMatcher` 用同一份词（名字 + aliases）

★保守优先★：纠正只在"同长度窗口 + 只有一个音节不同 + 相似度够高"时才动手，
并且遇到常见虚词/已有规范词一律跳过 —— 把「海尔洗衣机」改成「凯尔希」这种
误伤（实测踩过）比少改几个错字糟得多。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import phonetic
from ..names import NameCorrector, NameHit

KIND_NAME = "name"
KIND_WAKE = "wake"
KIND_DOMAIN = "domain"

#: 常见虚词/高频词：绝不参与"近似纠正"（改了就是误伤）
_STOPWORDS = frozenset(
    "的 了 是 在 我 有 和 就 不 人 都 一 上 也 很 到 说 要 去 你 会 着 没 看 好 想 能 这 那 "
    "什么 怎么 为什么 可以 现在 时候 今天 明天 昨天 一个 一下 一点 这个 那个 我们 你们 "
    "他们 自己 因为 所以 但是 如果 还是 就是 不是 没有 知道 觉得 应该 可能 已经".split()
)

MAX_FIX_LEN = 6          # 只纠正 2~6 个字的词（再长容易误伤、再短没有信息量）


@dataclass(frozen=True)
class Entry:
    """词库里的一条。"""

    word: str
    weight: float = 0.0          # 热词权重（0 = 用默认值）
    kind: str = KIND_DOMAIN      # name / wake / domain
    variants: tuple[str, ...] = ()   # 听错写法 / 别名（只用于唤醒匹配与规范名还原）

    def as_dict(self) -> dict:
        return {"word": self.word, "weight": self.weight, "kind": self.kind,
                "variants": list(self.variants)}


class Lexicon:
    """一个进程一份（按需构建，构建失败只降级，绝不挡住说话）。"""

    def __init__(self, settings: Any, logger=None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("voice_loop.asr.lexicon")
        cfg = getattr(settings, "asr", None)
        self.cfg = getattr(cfg, "lexicon", None)
        self._entries: list[Entry] | None = None
        self._syllables: dict[str, tuple[str, ...]] = {}
        self._names: NameCorrector | None = None
        self._built_files: dict[str, float] = {}
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ 属性
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    @property
    def max_words(self) -> int:
        return max(1, int(getattr(self.cfg, "max_words", 2000) or 2000))

    # ------------------------------------------------------------------ 构建
    def entries(self) -> list[Entry]:
        if self._entries is None:
            self._entries = self._build()
        return self._entries

    def words(self) -> list[str]:
        return [e.word for e in self.entries()]

    def _build(self) -> list[Entry]:
        out: list[Entry] = []
        seen: set[str] = set()

        def add(word: str, *, weight: float = 0.0, kind: str = KIND_DOMAIN,
                variants: tuple[str, ...] = ()) -> None:
            text = str(word or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            out.append(Entry(word=text, weight=weight, kind=kind, variants=variants))

        # ① 词库文件（`data/lexicon/*.txt`）
        for entry in self._from_files():
            add(entry.word, weight=entry.weight, kind=entry.kind, variants=entry.variants)
        # ② 人格文件里的名字 / 唤醒词 / 别名
        if getattr(self.cfg, "from_personas", True):
            for word, weight, kind, variants in self._from_personas():
                add(word, weight=weight, kind=kind, variants=variants)
        if len(out) > self.max_words:
            # 超上限时保留权重高的（名字/唤醒词权重高，稳定留下）
            out.sort(key=lambda e: (-e.weight, e.kind != KIND_NAME, len(e.word)))
            dropped = len(out) - self.max_words
            out = out[: self.max_words]
            self._warn(f"词库超过上限 {self.max_words} 条，丢掉 {dropped} 条低权重词")
        return out

    def _from_files(self) -> list[Entry]:
        out: list[Entry] = []
        default_weight = float(getattr(self.cfg, "weight", 1.5) or 1.5)
        for pattern in list(getattr(self.cfg, "files", []) or []):
            for path in sorted(self._glob(str(pattern))):
                try:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    self._warn(f"词库文件读不了（{path}）：{exc}")
                    continue
                for raw in text.splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    # 「词|变体1,变体2 权重」：竖线前面是规范写法
                    head, _, tail = line.partition("|")
                    bits = head.split()
                    if not bits:
                        continue
                    word = bits[0]
                    weight = default_weight
                    if len(bits) > 1:
                        try:
                            weight = float(bits[1])
                        except ValueError:
                            pass
                    variants = tuple(v.strip() for v in tail.replace("，", ",").split(",")
                                     if v.strip()) if tail else ()
                    kind = KIND_NAME if weight >= default_weight * 1.5 else KIND_DOMAIN
                    out.append(Entry(word=word, weight=weight, kind=kind, variants=variants))
        return out

    def _from_personas(self) -> list[tuple[str, float, str, tuple[str, ...]]]:
        """人格文件里的名字 / 唤醒词 / 别名 → 词条（这些词权重最高）。"""
        weight = float(getattr(self.cfg, "weight", 1.5) or 1.5) * 1.5
        out: list[tuple[str, float, str, tuple[str, ...]]] = []
        try:
            from ..persona import CharacterRegistry  # noqa: PLC0415 - 只在构建时读一次

            registry = CharacterRegistry(self.settings.resolve(self.settings.persona.file))
            chars = list(registry.all(only_enabled=True))
        except Exception as exc:  # noqa: BLE001 - 人格文件坏了就只用词库文件
            self._warn(f"读不到角色名（{exc}），词库只用文件那部分")
            return out
        for char in chars:
            name = str(getattr(char, "name", "") or "").strip()
            if name:
                out.append((name, weight, KIND_NAME, ()))
            for word in getattr(char, "wake_words", []) or []:
                text = str(word or "").strip()
                if not text:
                    continue
                aliases = tuple(str(v).strip() for v in
                                (getattr(char, "aliases", {}) or {}).get(text, []) or []
                                if str(v).strip())
                out.append((text, weight, KIND_WAKE, aliases))
        return out

    def _glob(self, pattern: str) -> list[str]:
        try:
            base = Path(pattern)
            if not base.is_absolute():
                base = Path(str(getattr(self.settings, "root", ".") or ".")) / base
            parent, name = base.parent, base.name
            if not parent.is_dir():
                return []
            return [str(p) for p in parent.glob(name) if p.is_file()]
        except (OSError, ValueError) as exc:
            self._warn(f"词库路径写错了（{pattern}）：{exc}")
            return []

    # ------------------------------------------------------------- 消费 1/2：引擎词表
    def prompt_text(self, limit: int = 60) -> str:
        """Whisper 的 ``initial_prompt``：一句话把词念一遍。"""
        words = [e.word for e in self.entries()][: max(1, int(limit))]
        if not words:
            return ""
        return "以下专有名词请按原样转写：" + "、".join(words) + "。"

    def hotwords_file(self, *, tokens_path: str = "") -> str:
        """transducer 的 ``hotwords.txt``（``词 :权重`` 一行一个）。

        ★长度限制★：热词表越大解码越慢，所以按权重取前 N 个（N = max_words/4）。
        """
        room = self._generated_dir()
        target = room / "hotwords.txt"
        limit = max(8, self.max_words // 4)
        rows = []
        for entry in self.entries()[:limit]:
            if not _all_cjk(entry.word):
                continue                    # cjkchar 切分只认真汉字
            weight = entry.weight or float(getattr(self.cfg, "weight", 1.5) or 1.5)
            rows.append(f"{entry.word} :{weight:g}")
        if not rows:
            return ""
        self._write_if_changed(target, "\n".join(rows) + "\n")
        return str(target)

    def keywords_file(self, *, tokens: list[str] | None = None) -> str:
        """KWS 的 ``keywords.txt``：每行 ``<token> <token> ... @显示词 :权重``。

        sherpa-onnx 的关键词表要的是**模型词表里的 token（拼音/音节）**，
        所以必须给 tokens.txt 的集合；给不了就生成"显示词"版本并**明说**不能直接用。
        """
        words = [e.word for e in self.entries() if _all_cjk(e.word)]
        if not words:
            return ""
        room = self._generated_dir()
        target = room / "keywords.txt"
        rows: list[str] = []
        for word in words[: self.max_words // 4]:
            syl = self._syllables_of(word)
            if not syl or (tokens is not None and not set(syl) <= set(tokens)):
                continue
            rows.append(" ".join(syl) + f" @{word}")
        if not rows:
            self._warn("关键词表生成不出来（缺拼音或模型词表对不上）——"
                       "请手写 data/lexicon/keywords.txt 并在 [asr.kws] 里指过去")
            return ""
        self._write_if_changed(target, "\n".join(rows) + "\n")
        return str(target)

    def _generated_dir(self) -> Path:
        root = Path(str(getattr(self.settings, "root", ".") or "."))
        room = root / "data" / "lexicon" / "_gen"
        room.mkdir(parents=True, exist_ok=True)
        return room

    def _write_if_changed(self, path: Path, text: str) -> None:
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                return
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._warn(f"写词表失败（{path}）：{exc}")

    # ------------------------------------------------------- 消费 3：事后纠正
    def corrector(self) -> NameCorrector:
        """角色名/唤醒词的纠错器（沿用 `names.py` 那套**句首**规则）。"""
        if self._names is None:
            names = [e.word for e in self.entries() if e.kind in (KIND_NAME, KIND_WAKE)]
            aliases: dict[str, list[str]] = {
                e.word: list(e.variants) for e in self.entries() if e.variants
            }
            try:
                self._names = NameCorrector(names, aliases)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"名字纠错器建不起来（{exc}），跳过这一步")
                self._names = NameCorrector([], {})
        return self._names

    def fix(self, text: str) -> tuple[str, list[NameHit]]:
        """事后纠正：①句首名字（含别名/近音）②领域词的拼音近似（保守）。"""
        if not self.enabled or not text:
            return text, []
        if not bool(getattr(self.cfg, "fix", True)):
            return text, []
        fixed, hits = text, []
        try:
            fixed, hits = self.corrector().correct(text)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"名字纠正失败（{exc}）")
        return self._fix_domain(fixed, hits)

    def _fix_domain(self, text: str, hits: list[NameHit]) -> tuple[str, list[NameHit]]:
        threshold = float(getattr(self.cfg, "fix_min_score", 0.78) or 0.78)
        vocab = {e.word for e in self.entries()}
        taken: set[int] = set()
        for hit in hits:
            taken.update(range(hit.start, hit.end))
        out = text
        extra: list[NameHit] = []
        for entry in self.entries():
            word = entry.word
            if entry.kind == KIND_NAME and word in {h.name for h in hits}:
                continue
            size = len(word)
            if not (2 <= size <= MAX_FIX_LEN) or not _all_cjk(word):
                continue
            pattern = self._syllables_of(word)
            if not pattern:
                continue
            start = 0
            while True:
                idx = out.find(_first_char(word), start)   # 粗略预筛，避免全表扫
                if idx < 0:
                    break
                start = idx + 1
                if idx + size > len(out):
                    break
                window = out[idx: idx + size]
                if window == word or window in vocab or window in _STOPWORDS:
                    continue
                if any(pos in taken for pos in range(idx, idx + size)):
                    continue
                if not _all_cjk(window):
                    continue
                score = phonetic.compare(self._syllables_of(window), pattern, allow_diff=1)
                if score >= threshold:
                    out = out[:idx] + word + out[idx + size:]
                    extra.append(NameHit(name=word, spoken=window, start=idx,
                                         end=idx + size, score=score))
                    taken.update(range(idx, idx + size))
        return out, hits + extra

    # ------------------------------------------------------------------ 其它
    def _syllables_of(self, text: str) -> tuple[str, ...]:
        if text not in self._syllables:
            self._syllables[text] = phonetic.syllables(text)
        return self._syllables[text]

    def stats(self) -> dict:
        entries = self.entries()
        by_kind: dict[str, int] = {}
        for entry in entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        return {"total": len(entries), "kinds": by_kind,
                "files": list(getattr(self.cfg, "files", []) or []),
                "fix": bool(getattr(self.cfg, "fix", True))}

    def _warn(self, line: str) -> None:
        if line in self._warned:
            return
        self._warned.add(line)
        if self.log is not None:
            self.log.warning(f"[词库] {line}")
        else:
            print(f"[词库] {line}", flush=True)


def _all_cjk(text: str) -> bool:
    return bool(text) and all("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
                              for ch in text)


def _first_char(word: str) -> str:
    """预筛用：只在首字出现处开窗（同音错字通常首字仍相同或近似）。"""
    return word[:1]


def build_lexicon(settings: Any, logger=None) -> Lexicon | None:
    """按配置建词库；关掉或建不起来就返回 None（调用方按"没有词库"处理）。"""
    cfg = getattr(getattr(settings, "asr", None), "lexicon", None)
    if cfg is None or not getattr(cfg, "enabled", True):
        return None
    try:
        return Lexicon(settings, logger)
    except Exception as exc:  # noqa: BLE001 - 词库坏了不该让人说不出话
        if logger is not None:
            logger.warning(f"[词库] 初始化失败（忽略）：{exc}")
        return None


__all__ = ["Entry", "KIND_DOMAIN", "KIND_NAME", "KIND_WAKE", "Lexicon", "build_lexicon"]
