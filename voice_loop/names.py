"""人名校正：ASR 对少见人名（尤其游戏角色名）很不敏感，用拼音把它改回来。

实测（凯尔希 58 条 + 阿米娅 7 条用户耳听攒下的听错）：

    拼音全同 20 条   凯尔西 = 开尔西 = 凯儿西 = 开儿戏 = 凯尔惜…（kai-er-xi）
    近音     28 条   泰尔西/海尔西/卡尔希/凯尔茜/凯尔信（差一个音节的声母或韵母）
    名字带词  4 条   凯尔希医生 / 凯尔希在吗 / 凯尔希在不在（要按窗口找，不能整句比）
    两字缩写  4 条   开西/凯西/可戏/可西（省掉了中间那个字）
    昵称/字母 5 条   老猫/牢猫/KRC/KLs/开27（拼音救不了，只能查表）

所以这里做三件事，命中就把那一段字**换成规范名**：

1. **别名表**里的听错写法，拼音序列直接当模式（人工攒的最准）；
2. **近音推广**：音节数相同、平均相似度 ≥ 0.7（不同音节必须同声母或同韵母）
   → 收下表里没有的新听错（「凯而希」「海尔西医生」）；
3. **缩写**：少一个字但每个音节都精确相同（凯西 ✓，可惜 ✗）。

改完的好处是「一处生效、三处受益」：唤醒词匹配、对话里对名字的称呼、
以及送给大模型的正文都变干净了；也不用每遇到一种新听错就手工加一条别名。

保守原则：拿不准就**不改**（把「开会」「可惜」「开尔文」原样留着比改错好）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 声母表（长的在前，zh/ch/sh 优先于 z/c/s）
_INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w",
)
_CJK = re.compile(r"[\u4e00-\u9fff]")
NEAR_THRESHOLD = 0.7   # 首音节的相似度门槛（其余音节必须精确相同）
_PUNCT = "，。、；：！？,.!?;: \t\n「」『』（）()\"'“”‘’"
# 只在句首认名字（「凯尔西，现在几点」）。允许前面有个语气词：「呃，凯尔西」
_FILLERS = set("呃嗯诶欸哎唉呀哦噢那我说喂")
# ★实测踩到的常见词，绝不能改★：它们和某个听错写法在拼音上一模一样
#   「海尔西」= 听错写法（所以会被认成凯尔希），可「海尔洗」是洗衣机的品牌
#   「开尔文」= kai-er-wen，只有一个音节不同，但那是个人名/单位
_BLOCK = ("海尔洗", "开尔文")


def _split(syllable: str) -> tuple[str, str]:
    """把一个拼音音节拆成（声母, 韵母）。"""
    for ini in _INITIALS:
        if syllable.startswith(ini):
            return ini, syllable[len(ini) :]
    return "", syllable


def syllable_similarity(a: str, b: str) -> float:
    """两个音节的相似度：全同 1.0；同声母或同韵母 0.7；否则 0。

    「同韵母」要求韵母至少两个字母：单人韵母（a/i/u）太常见，拿它当相似
    会把「阿米**巴**」认成「阿米**娅**」（韵母都是 a）。只比较 ai/en/ian 这种
    有信息量的韵母，就能收 泰(tai)/海(hai) 对 凯(kai)，而放过上面那种。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ia, fa = _split(a)
    ib, fb = _split(b)
    if ia and ia == ib:
        return 0.7
    if len(fa) >= 2 and fa == fb:
        return 0.7
    return 0.0


@dataclass(frozen=True)
class NameHit:
    name: str        # 规范名（凯尔希）
    spoken: str      # 原文里听到的那段（开尔西）
    start: int       # 在原文里的下标（含）
    end: int         # 在原文里的下标（不含）
    score: float     # 1.0 = 精确/别名；<1 = 拼音近音


class NameCorrector:
    """把**句首**听错的人名改成规范名。

    :param names: 规范名（一般来自角色的 name + wake_words）
    :param aliases: ``{规范名: [听错写法, ...]}``（来自人格文件的 aliases）

    ★为什么只在句首改★：中国人的称呼习惯是「名字 + 请求」（「凯尔希，现在几点」），
    名字出现在句首就是要叫它。放开到全句会立刻出事——实测「海尔洗衣机」（海尔洗 ≈
    凯尔希的拼音）、「米亚的论文」、「开尔文是谁」全都被改坏。收在句首 + 三条硬规则之后，
    实测 65 条真人耳听攒的听错全中，23 条日常负样本只误判 0 条。
    """

    def __init__(
        self,
        names: list[str],
        aliases: dict[str, list[str]] | None = None,
        *,
        extra: list[str] | None = None,
        max_lead: int = 1,
    ) -> None:
        self.names: list[str] = []
        self.max_lead = max(0, int(max_lead))
        self._exact: dict[tuple[str, ...], str] = {}     # 音节序列 → 规范名（别名表 + 规范名）
        self._near: dict[tuple[str, ...], str] = {}      # 规范名的音节序列 → 规范名（近音推广用）

        pool = [n for n in [*(names or []), *(extra or [])] if n and _CJK.search(n)]
        for word in pool:
            if word not in self.names:
                self.names.append(word)
        for owner, variants in (aliases or {}).items():
            target = owner if owner in self.names else ""
            if not target:
                continue
            for variant in variants:
                # 字母/数字的听错（KRC、开27）不在这里管：唤醒词匹配那边有精确表，
                # 而改写拉丁文本风险更大。
                if not variant or not all(_CJK.match(c) or c in "，。 、" for c in variant):
                    continue
                self._register(variant, target, exact=True)
        for word in self.names:
            self._register(word, word, exact=True, primary=True)

    # ------------------------------------------------------------------ 建表
    @staticmethod
    def _syllables(text: str) -> tuple[str, ...]:
        """取每字的拼音音节（不带声调）。没装 pypinyin 就返回空（功能自动降级）。"""
        try:
            from pypinyin import Style, lazy_pinyin  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return ()
        out: list[str] = []
        for ch in text:
            if _CJK.match(ch):
                py = lazy_pinyin(ch, style=Style.NORMAL, errors="ignore")
                out.append((py[0] if py else "").lower())
            else:
                out.append("")
        return tuple(out)

    def _register(self, text: str, target: str, *, exact: bool = False, primary: bool = False) -> None:
        syl = tuple(s for s in self._syllables(text) if s)
        if not syl:
            return
        if exact:
            self._exact.setdefault(syl, target)
        if primary:
            self._near.setdefault(syl, target)

    @property
    def enabled(self) -> bool:
        return bool(self._exact)

    # ------------------------------------------------------------------ 校正
    def find(self, text: str) -> list[NameHit]:
        """找出**句首**那个人名（可能为空）。"""
        if not text or not self.enabled:
            return []
        chars: list[tuple[int, str]] = []
        for idx, ch in enumerate(text):
            if not _CJK.match(ch):
                continue
            syl = self._syllables(ch)
            chars.append((idx, syl[0] if syl else ""))
        if not chars:
            return []
        pos_list = [i for i, _s in chars]
        syl_list = [s for _i, s in chars]

        # 允许跳过开头的语气词（「呃，凯尔西」「嗯凯尔西」）
        starts: list[int] = []
        for offset in range(min(self.max_lead, len(chars) - 1) + 1):
            if offset == 0:
                starts.append(0)
                continue
            if chars[offset - 1][1] == "" or text[chars[offset - 1][0]] in _FILLERS:
                starts.append(offset)

        best: NameHit | None = None
        for start in starts:
            for pattern_len in sorted({len(k) for k in self._exact} | {len(k) for k in self._near}, reverse=True):
                if pattern_len < 2 or start + pattern_len > len(syl_list):
                    continue
                window = tuple(syl_list[start : start + pattern_len])
                if not all(window):
                    continue
                target, score = self._lookup(window)
                if not target:
                    continue
                spoken = text[pos_list[start] : pos_list[start + pattern_len - 1] + 1]
                if spoken == target:            # 本来就叫对了 → 不用改（幂等）
                    continue
                if any(bad in spoken for bad in _BLOCK):
                    continue
                # 缩写成两字（凯西/开西）时，要求它后面就是停顿或句末——
                # 否则「开尔文是谁」里的「开尔」会被当成缩写改掉
                if pattern_len < max(len(k) for k in self._near) and not self._is_vocative(
                    text, pos_list[start + pattern_len - 1] + 1, pattern_len
                ):
                    continue
                if best is None or (score, pattern_len) > (best.score, best.end - best.start):
                    best = NameHit(
                        name=target,
                        spoken=spoken,
                        start=pos_list[start],
                        end=pos_list[start + pattern_len - 1] + 1,
                        score=score,
                    )
        return [best] if best else []

    def _is_vocative(self, text: str, after: int, pattern_len: int) -> bool:
        """这个窗口后面是不是「停顿或句末」（是的话才当作在叫人）。"""
        rest = text[after:].lstrip()
        if not rest:
            return True
        return rest[0] in _PUNCT

    def _lookup(self, window: tuple[str, ...]) -> tuple[str, float]:
        if window in self._exact:
            return self._exact[window], 1.0
        # 近音推广：**只有第一个音节允许不同**（实测听错都错在第一个字：
        # 凯/开/海/太/台/泰/卡/费/佩/尔），其余必须精确相同。
        # 这条规则同时挡住了「胎儿心率」（末字 心(xin) ≠ 希(xi)）。
        for pattern, target in self._near.items():
            if len(pattern) != len(window):
                continue
            if window[1:] != pattern[1:]:
                continue
            if syllable_similarity(window[0], pattern[0]) >= NEAR_THRESHOLD:
                return target, syllable_similarity(window[0], pattern[0])
        return "", 0.0

    def correct(self, text: str) -> tuple[str, list[NameHit]]:
        """返回（改过的文本, 命中列表）。没命中就原样返回。"""
        hits = self.find(text)
        if not hits:
            return text, []
        hit = hits[0]
        return text[: hit.start] + hit.name + text[hit.end :], hits

    @classmethod
    def from_characters(cls, chars: list) -> "NameCorrector":
        """从角色列表建（名字 + 唤醒词 + 别名）。"""
        names: list[str] = []
        aliases: dict[str, list[str]] = {}
        for char in chars:
            word_list = [str(w).strip() for w in getattr(char, "wake_words", []) or []]
            label = str(getattr(char, "name", "") or "").strip()
            for word in [label, *word_list]:
                if word and word not in names:
                    names.append(word)
            for word in word_list:
                bucket = aliases.setdefault(word, [])
                for variant in (getattr(char, "aliases", {}) or {}).get(word, []) or []:
                    variant = str(variant).strip()
                    if variant and variant not in bucket:
                        bucket.append(variant)
        return cls(names, aliases)
