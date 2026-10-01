"""拼音音近匹配的公共核心（人名校正与唤醒词**共用一份**）。

★为什么要有这一层★（2026-10-01，工程日志 §53）

唤醒词原来靠「人工攒别名」：ASR 把「能天使」听成「能天史」，
**字符级**相似度只有 2/3 = 0.667 < 0.75 阈值 → 认不出来 → 只能一条条加别名
（能天使的资料卡里已经攒了 5 条，凯尔希那边攒了 58 条）。

业界的做法不是攒别名，而是**把文本转成拼音/音素再比**：

- **声学侧**：专用关键词模型（sherpa-onnx 的 open-vocabulary KWS、Porcupine、openWakeWord），
  关键词以「声母 + 韵母」的 token 序列给出（`n ǐ h ǎo j ūn g ē @你好军哥`），
  每个关键词还能单独给 `:boosting` 与 `#threshold` 调「误触发 ↔ 漏触发」。
  用户说的「忽略辅音比韵母」是这条路的粗糙版 —— 真做法是**音素级**匹配，不是真丢辅音。
- **文本侧**（本项目的位置）：ASR 出文本后再归一到拼音比较，
  sherpa-onnx 也提供同音词替换（pinyin homophone replacer）用的就是这套。

所以我们走文本侧：待唤醒时本来就在跑 ASR，**零新模型、零新依赖**（pypinyin 早就装着）。

★规则是实测出来的，不是拍的★（数据来自 `voice_loop/names.py`：凯尔希 58 条 + 阿米娅 7 条
真人耳听的听错，外加 22 条日常负样本）：

1. **只信有信息量的韵母**：韵母长度 ≥2 才算「同韵母」。单韵母 a/i/u 太常见，
   拿它当相似会把「阿米**巴**」认成「阿米**娅**」。
2. **相似度只有三档**：全同 1.0 / 同声母 0.7 / 同韵母 0.7 / 否则 0。
3. **拼音撞车的常用词要挡**：「海尔洗」(hai-er-xi) 和「凯尔希」拼音一模一样，
   可它是洗衣机 —— 这种只能查表（`BLOCK`）。
4. **两处宽严不同**（都是实测换来的）：
   - 人名：只允许**第一个**音节不同（听错都错在首字），其余必须精确相同；
   - 唤醒词：允许**恰好一个**音节不同（听错会落在任何位置，如「能天**史**」）。
"""

from __future__ import annotations

import re

# 声母表（长的在前，zh/ch/sh 优先于 z/c/s）
INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w",
)
CJK = re.compile(r"[\u4e00-\u9fff]")
# 语气词白名单：允许它们出现在被喊的名字/唤醒词前面（「呃，凯尔西」）
FILLERS = set("呃嗯诶欸哎唉呀哦噢那我说喂")
# ★拼音撞车的常用词，绝不能认★（`海尔洗` = 洗衣机的海尔，拼音与「凯尔希」全同；
#   `开尔文` = kai-er-wen，只有一个音节不同，但那是人名/单位）
BLOCK = ("海尔洗", "开尔文")

FULL = 1.0      # 音节完全相同
NEAR = 0.7      # 同声母 或 同韵母（韵母要有信息量）
NEAR_MIN_FINAL = 2   # 韵母至少这么长才算「有信息量」（挡住 a/i/u 这种太常见的）

# ★同类混淆组★：组内视为「同一个声母/韵母」。都是中文 ASR 的经典错法，
# 而且实测遇得上 —— 「白哲」(bai-zhe) 与「白泽」(bai-ze) 差的就是平翘舌，
# 不加这组就只能认得下白则/百泽，认不下白哲。
# ★加宽松度必须重新验负样本★（scripts/test_phonetic.py 第 [4] 节，9 条日常话 0 误触发）。
_INITIAL_ALIKE = (
    {"z", "zh"}, {"c", "ch"}, {"s", "sh"},   # 平翘舌
    {"n", "l"},                                    # n/l（南方口音）
)
_FINAL_ALIKE = (
    {"an", "ang"}, {"en", "eng"}, {"in", "ing"},   # 前后鼻音
    {"ian", "iang"}, {"uan", "uang"},
)


def alike(value: str, other: str, groups) -> bool:
    """value 与 other 是否落在同一个混淆组里。"""
    return any(value in group and other in group for group in groups)


# --------------------------------------------------------------------------- #
def split(syllable: str) -> tuple[str, str]:
    """把一个拼音音节拆成（声母, 韵母）。"""
    got = str(syllable or "").lower()
    for ini in INITIALS:
        if got.startswith(ini):
            return ini, got[len(ini) :]
    return "", got


def similarity(a: str, b: str) -> float:
    """两个音节的相似度：全同 1.0；同声母/同韵母（含同类混淆）0.7；否则 0。

    「同韵母」要求韵母至少 ``NEAR_MIN_FINAL`` 个字母：只比较 ai/en/ian 这种
    有信息量的韵母，就能收 泰(tai)/海(hai) 对 凯(kai)，而放过「阿米**巴**」（韵母 a）。
    声母/韵母的**同类混淆组**（平翘舌 z/zh、n/l、前后鼻音 an/ang…）也算相似 ——
    都是 ASR 的经典错法（«白泽→白哲» 就靠它）。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return FULL
    ia, fa = split(a)
    ib, fb = split(b)
    if ia and (ia == ib or alike(ia, ib, _INITIAL_ALIKE)):
        return NEAR
    if len(fa) >= NEAR_MIN_FINAL and (fa == fb or alike(fa, fb, _FINAL_ALIKE)):
        return NEAR
    return 0.0


def syllables(text: str) -> tuple[str, ...]:
    """逐字取拼音音节（不带声调、小写）。非汉字给空串。

    没装 pypinyin 就返回空表 —— 调用方看到「空」就当这个功能不存在（自动降级）。
    """
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ()
    out: list[str] = []
    for ch in str(text or ""):
        if CJK.match(ch):
            py = lazy_pinyin(ch, style=Style.NORMAL, errors="ignore")
            out.append((py[0] if py else "").lower())
        else:
            out.append("")
    return tuple(out)


def compare(window: tuple[str, ...], pattern: tuple[str, ...], *,
            allow_diff: int = 1, only_first: bool = False) -> float:
    """音节序列比对，返回 0（不像）或相似度（0.7~1.0）。

    :param allow_diff: 允许多少个音节「不同」（其余必须精确相同）
    :param only_first: True = 只允许**第一个**音节不同（人名用的严版）
    """
    if not window or len(window) != len(pattern):
        return 0.0
    if any(not s for s in window) or any(not s for s in pattern):
        return 0.0
    diffs: list[float] = []
    for idx, (got, want) in enumerate(zip(window, pattern)):
        if got == want:
            continue
        if only_first and idx != 0:
            return 0.0
        score = similarity(got, want)
        if score < NEAR:
            return 0.0
        diffs.append(score)
    if len(diffs) > max(0, int(allow_diff)):
        return 0.0
    if not diffs:
        return FULL
    # 有几个音节不同就按比例摊（都 ≥0.7，所以结果落在 0.7~1.0）
    return sum(diffs) / len(diffs)


def blocked(text: str) -> bool:
    """这段文本里有没有拼音撞车的常用词（有就一律不认）。"""
    return any(bad in str(text or "") for bad in BLOCK)
