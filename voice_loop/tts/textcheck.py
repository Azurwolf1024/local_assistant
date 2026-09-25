"""合成后的「说的是不是这句话」检查（拿本地 ASR 当裁判）。

为什么需要它（2026-09-25 用户实测）：用户听出有两条把「排好了」的**「了」丢了**——
这是**内容错误**，比沙沙声、比音区飘都严重（说的是另一句话）。
ZipVoice 是采样生成，偶发丢字/吞音躲不掉，但**可以检出来重采**。

裁判怎么当（实测数据，别凭感觉改阈值）：

| 合成 | 直接用 ASR 文本比 | ★把中文数字归一成 ASCII 之后★ |
|---|---|---|
| 正常的几条 | 0.929 / 0.964 | **1.000** |
| 丢了「了」的那两条 | 0.909 | **0.982** |

第一列根本没法用：SenseVoice 的 ITN 会把「九」写成 9、「两」写成 2，
正常的合成也只剩 0.93 —— 跟丢字的 0.909 挤在一起。
把中文数字归一（九→9、两→2…）之后**正常全是 1.000、丢字是 0.982**，中间空得很干净
（默认阈值 0.99）。

★两个已知局限（别当成万能的）★：
* ASR 有自己的语言模型，**小的丢字它可能自己补回来**（所以这个检查会漏，不会误报太多）；
* 真要说错话（整句串味）它也能看出来，但那时 ratio 会掉得很低，日志里一眼可见。
"""

from __future__ import annotations

import difflib
import re

# 中文数字 → ASCII：SenseVoice 的 ITN 会做这个转换，不归一就没法比
_DIGITS = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_KEEP = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize(text: str) -> str:
    """只留汉字/字母/数字，并把中文数字归一成 ASCII（标点、空白全丢）。"""
    stripped = _KEEP.sub("", text or "")
    return "".join(_DIGITS.get(ch, ch) for ch in stripped)


def similarity(expected: str, heard: str) -> float:
    """归一化之后的相似度（0~1）。**两边都空**时给 1.0（没内容没法判坏）。"""
    a, b = normalize(expected), normalize(heard)
    if not a and not b:
        return 1.0
    return float(difflib.SequenceMatcher(None, a, b).ratio())


def missing(expected: str, heard: str) -> str:
    """少说了哪些字（给日志/重采理由用，方便人一眼看懂）。"""
    a, b = normalize(expected), normalize(heard)
    sm = difflib.SequenceMatcher(None, a, b)
    return "".join(a[i1:i2] for tag, i1, i2, _j1, _j2 in sm.get_opcodes() if tag == "delete")


def text_guard_verdict(ratio: float | None, min_ratio: float, attempt: int, tries: int) -> bool:
    """这一遍要不要因为**文本不对**丢掉重采？（纯函数，好单测）

    ``ratio`` 为 ``None`` = 这次没查（没有 ASR / 关掉了）→ 不重采
    （★跟音区守卫一个原则：宁可放过一遍，也不能因为查不了而白等★）。
    """
    if ratio is None or not (min_ratio > 0):
        return False
    if attempt + 1 >= max(1, int(tries)):
        return False
    return float(ratio) < float(min_ratio)
