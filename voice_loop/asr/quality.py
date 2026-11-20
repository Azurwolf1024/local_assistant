"""识别质量的启发式评分（L2「解码策略」的一部分，不属于任何引擎）。

用途有两个：
1. **择优**：主引擎和复核引擎各给一句，得挑一个（以前写在 ``router.transcribe`` 里）
2. **触发复核**：``score<0.45`` 就是一种复核条件（`AsrSpec.verify_when`）

★为什么单独一个文件★：这套规则（复读惩罚 / 幻觉句 / 中英比例）跟"用哪个引擎"无关，
以前埋在 ``router.py`` 里，导致换引擎时容易连规则一起丢掉。
"""

from __future__ import annotations

import re
from collections import Counter

# Whisper 在静音/噪声上常见的幻觉句
_HALLUCINATION_HINTS = (
    "请不吝点赞",
    "订阅",
    "转发",
    "打赏",
    "字幕由",
    "字幕组",
    "明镜与点点",
    "amara.org",
    "谢谢观看",
    "谢谢大家观看",
    "请关注",
    "感谢观看",
    "subscribe",
    "thanks for watching",
    "♪",
)

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_NON_WORD = re.compile(r"[^\w\u3400-\u9fff\uf900-\ufaff]")


def repetition_penalty(text: str) -> float:
    """检测逐字/短语复读（解码退化的典型特征），返回 0~1。"""
    t = _NON_WORD.sub("", text or "")
    if len(t) < 6:
        return 0.0
    worst = 0.0
    for size in (1, 2, 3, 4):
        grams = [t[i: i + size] for i in range(len(t) - size + 1)]
        if not grams:
            continue
        _, count = Counter(grams).most_common(1)[0]
        if size == 1 and count >= 6:
            worst = max(worst, 1.0)
        elif size >= 2 and count >= 4:
            worst = max(worst, 0.8)
    return worst


def quality_score(text: str, expect_zh: bool = True) -> float:
    """启发式质量分（0~1），用于在两个引擎之间择优、以及触发复核。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    score = 0.6
    if len(t) < 2:
        score -= 0.4

    cjk = len(_CJK.findall(t))
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    total = cjk + latin
    if total and expect_zh:
        score += 0.2 * (cjk / total - 0.5)

    if t[-1] in "。！？!?….;；":
        score += 0.08
    if any(ch in t for ch in "，,、"):
        score += 0.04

    score -= 0.6 * repetition_penalty(t)
    low = t.lower()
    if any(hint in low for hint in _HALLUCINATION_HINTS):
        score -= 0.35
    return max(0.0, min(1.0, score))


__all__ = ["quality_score", "repetition_penalty"]
