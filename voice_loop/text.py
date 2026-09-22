"""文本后处理：LLM 输出清洗 + 流式分句（低延迟 TTS 的关键）。"""

from __future__ import annotations

import re
import time

# ---------------------------------------------------------------- 清洗规则
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d+[.、)])\s+", re.M)
_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.M)
_HR = re.compile(r"^\s*[-*_]{3,}\s*$", re.M)
_LATEX = re.compile(r"\$+([^$]*)\$+")
_HTML = re.compile(r"<[^>]{1,80}>")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\u2190-\u21ff\u2b00-\u2bff\ufe0f\u200d]+"
)
_URL = re.compile(r"https?://\S+")
_BRACKET_NOISE = re.compile(r"[（）()【】\[\]<>《》]")
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")
_MULTI_NL = re.compile(r"\n{2,}")

# 不该被朗读的“思考型”前缀
_PREFIX_NOISE = re.compile(r"^\s*(?:当然可以[！!，,。]?|好的[！!，,。]?|没问题[！!，,。]?)\s*")


def clean_for_tts(text: str, strip_lead_in: bool = True) -> str:
    """把 LLM 的 Markdown 文本转成适合 TTS 朗读的纯文本。"""
    if not text:
        return ""
    out = text
    out = _CODE_FENCE.sub(" ", out)
    out = _MD_IMAGE.sub(" ", out)
    out = _MD_LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _HR.sub(" ", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _HEADING.sub("", out)
    out = _LIST_MARK.sub("", out)
    out = _STRIKE.sub(r"\1", out)
    out = _BOLD_ITALIC.sub(r"\2", out)   # group2 才是内容，group1 是 * 本身
    out = _LATEX.sub(r"\1", out)
    out = _HTML.sub(" ", out)
    out = _URL.sub(" ", out)
    out = _EMOJI.sub("", out)
    if strip_lead_in:
        out = _PREFIX_NOISE.sub("", out)
    out = out.replace("\u200b", "").replace("\ufeff", "")
    out = _MULTI_SPACE.sub(" ", out)
    out = _MULTI_NL.sub("\n", out)
    # 去掉成对括号，但保留内容（避免朗读出“括号”）
    out = _BRACKET_NOISE.sub("", out)
    return out.strip()


def prepare_for_reading(text: str) -> str:
    """面向 TTS 的整句清理。

    注意：这里**不**剥掉「好的」「当然可以」这类开头词。
    它们虽然占字数，但确实是模型想说的话，删掉反而让开头变得突兀。
    """
    out = clean_for_tts(text, strip_lead_in=False)
    out = re.sub(r"^[，。、；：,.;:\s]+", "", out)
    out = re.sub(r"[，、,]{2,}", "，", out)
    out = re.sub(r"[。.]{2,}", "。", out)
    return out.strip()


# ---------------------------------------------------------------- 标点移植
PUNCT_CHARS = "，。！？；：、,.!?;:…—"


def _content(text: str) -> str:
    return "".join(ch for ch in text if ch not in PUNCT_CHARS and not ch.isspace())


def transplant_punctuation(primary: str, donor: str, min_ratio: float = 0.55) -> str:
    """把 donor 的标点按位置搬到 primary 上。

    Whisper 的 int8 turbo 导出（以及部分量化模型）不会输出标点，
    而 SenseVoice 的标点质量很好。两者文字接近时，用 Whisper 的字 + SenseVoice 的标点，
    对后续 TTS 的断句与语气帮助很大。
    """
    import difflib

    primary = (primary or "").strip()
    donor = (donor or "").strip()
    if not primary or not donor:
        return primary
    if any(ch in primary for ch in "，。！？；："):
        return primary  # 已有标点，不动

    a, b = _content(primary), _content(donor)
    if not a or not b:
        return primary
    if difflib.SequenceMatcher(None, a, b).ratio() < min_ratio:
        return primary  # 内容差太多，硬套标点反而会错

    # donor 中每个标点对应的「非标点字符下标」
    marks: dict[int, list[str]] = {}
    k = 0
    for ch in donor:
        if ch in PUNCT_CHARS:
            marks.setdefault(k, []).append(ch)
        elif not ch.isspace():
            k += 1
    if not marks:
        return primary

    out: list[str] = []
    k = 0
    for ch in primary:
        out.append(ch)
        if ch in PUNCT_CHARS or ch.isspace():
            continue
        k += 1
        if k in marks:
            out.extend(marks.pop(k))
    return "".join(out).strip()


# ---------------------------------------------------------------- 流式分块
#
# 关于「语调自然」的设计要点：
#   Piper 的中文音色（espeak-ng 的 cmn）**不会把标点变成音素**，也就是说
#   模型看不到任何句读标记，停顿完全靠它自己学到的韵律。
#   因此把文本切得太碎（例如每个逗号都切一块）会让每一小段都单独生成、
#   各自收尾，听起来就又平又顿。
#   正确做法是：只在句末切，并且把过短的句子合并，让每块都是一句完整的话。
#
# 一级断点：句末标点
_SENT_END = "。！？!?…"
# 二级断点：从句标点，只在整句过长时使用
_CLAUSE_END = "，,、：:；;"


class SpeechChunker:
    """把流式 token 增量切成「适合朗读」的文本块。

    切分规则
        1. 优先在句末标点处切 —— 每块都是一句完整的话，韵律最自然
        2. 只有整句长度超过 ``max_chars`` 时，才退到从句标点处切
        3. 长度超过 ``hard_limit`` 时硬切兜底，避免 LLM 不吐标点时卡死

    合并规则
        - 短于 ``min_chunk_chars`` 的句子先攒着，与下一句一起送合成，
          避免「好的。」这种碎片单独成块
        - 首块放宽到 ``first_min_chars``，让声音尽快出来
        - 攒句超过 ``max_hold_seconds`` 秒仍没等到下一句，也先送出去
    """

    def __init__(
        self,
        max_chars: int = 60,
        first_min_chars: int = 8,
        min_chunk_chars: int = 14,
        max_hold_seconds: float = 1.2,
        first_chunk_max_chars: int = 0,
    ) -> None:
        self.max_chars = max(8, int(max_chars))
        self.hard_limit = max(self.max_chars * 2, 40)
        self.first_min_chars = max(1, int(first_min_chars))
        self.min_chunk_chars = max(1, int(min_chunk_chars))
        self.max_hold_seconds = max(0.0, float(max_hold_seconds))
        # 第一块最多多少字（0 = 不限）。★克隆音色很吃「首块多长」★：
        # ZipVoice 一次 generate 要整块生成完才回音频，所以首块 60 字 = 开口前先等 8 秒。
        # 限到十几字 + 允许在逗号处切，开口等待能压到 2 秒上下（后面几块照旧按句切）。
        self.first_chunk_max_chars = max(0, int(first_chunk_max_chars))
        self._buf = ""
        self._pending = ""
        self._emitted = 0
        self._hold_since: float | None = None

    # -------------------------------------------------------------- public
    def feed(self, text: str) -> list[str]:
        """喂入新的增量文本，返回本次可以送去合成的块。"""
        if text:
            self._buf += text
        chunks = self._drain(flush=False)
        if not chunks:
            chunks = self._release_if_held_too_long()
        return chunks

    def flush(self) -> list[str]:
        """流结束时调用，返回剩余内容。"""
        chunks = self._drain(flush=True)
        tail = prepare_for_reading(self._pending)
        self._pending = ""
        self._hold_since = None
        if tail:
            chunks.append(tail)
        return chunks

    def reset(self) -> None:
        self._buf = ""
        self._pending = ""
        self._emitted = 0
        self._hold_since = None

    # ------------------------------------------------------------- private
    def _need_chars(self) -> int:
        return self.first_min_chars if self._emitted == 0 else self.min_chunk_chars

    def _release_if_held_too_long(self) -> list[str]:
        if not self._pending or self._hold_since is None:
            return []
        if self.max_hold_seconds and (time.monotonic() - self._hold_since) >= self.max_hold_seconds:
            text = prepare_for_reading(self._pending)
            self._pending = ""
            self._hold_since = None
            self._emitted += 1
            return [text] if text else []
        return []

    def _emit(self, chunks: list[str]) -> None:
        text = prepare_for_reading(self._pending)
        self._pending = ""
        self._hold_since = None
        self._emitted += 1
        if text:
            chunks.append(text)

    def _drain(self, flush: bool) -> list[str]:
        chunks: list[str] = []
        while True:
            cut, kind = self._find_cut(flush)
            if cut <= 0:
                break
            piece = self._buf[:cut]
            self._buf = self._buf[cut:]
            if piece.strip():
                self._pending += piece
                if self._hold_since is None:
                    self._hold_since = time.monotonic()

            if kind == "sentence":
                # 完整句子：够长就直接送，太短就留着和下一句合并
                # ★首块例外★（只有显式设了 first_chunk_max_chars 才启用）：一遇到句末就送，
                # 不再攒长——ZipVoice 整块生成完才回音频，首块攒到 60 字就是「开口前
                # 先等 8 秒」（实测 8.08s → 1.91s）。默认 0 = 保持老行为（Piper 那边
                # 短句合并是特意调过的，不能被这条改掉）。
                first_chunk = self._emitted == 0 and self.first_chunk_max_chars > 0
                if first_chunk or len(self._pending) >= self._need_chars():
                    self._emit(chunks)
            elif kind in ("clause", "hard"):
                # 长句被从句标点切开 / 硬切：立即送，避免积压
                self._emit(chunks)
            elif kind == "eof":
                break
        return chunks

    def _find_cut(self, flush: bool) -> tuple[int, str]:
        buf = self._buf
        if not buf:
            return 0, "none"

        # 1) 句末标点（无论长短都先切出来，是否马上朗读由 _drain 决定）
        for i, ch in enumerate(buf):
            if ch in _SENT_END or ch == "\n":
                return i + 1, "sentence"

        # 2) 整句过长 -> 从句标点处切
        #    ★第一块例外★：第一块一到 first_chunk_max_chars 就在逗号处切，
        #    否则「首块 60 字」= 开口前等 8 秒（克隆模型整块生成完才出声）。
        limit = self.max_chars
        if self._emitted == 0 and self.first_chunk_max_chars:
            limit = min(limit, max(self.first_min_chars, self.first_chunk_max_chars))
        if len(buf) >= limit:
            best = -1
            for i in range(min(len(buf) - 1, limit)):
                if buf[i] in _CLAUSE_END:
                    best = i
            if best >= 0:
                return best + 1, "clause"

        # 3) 过长且毫无标点 -> 硬切
        if len(buf) >= self.hard_limit:
            return self.max_chars, "hard"

        # 4) 流结束
        if flush:
            return len(buf), "eof"
        return 0, "none"


# 兼容旧名字
SentenceChunker = SpeechChunker


# ---------------------------------------------------------------- 停顿注入
_PAUSE_LONG = "[[,,]]"
_PAUSE_SHORT = "[[,]]"


def inject_pauses(text: str) -> str:
    """把中文标点换成 Piper 的原始音素停顿标记。

    Piper 的中文音色不会把 ``，`` ``。`` 变成音素，模型因而「看不见」句读。
    用 ``[[,]]`` / ``[[,,]]`` 直接注入停顿音素，可以强行制造更清晰的断句。
    代价是停顿可能偏长、语气偏平，所以默认关闭（``[tts] inject_pauses``）。
    """
    out: list[str] = []
    for ch in text:
        if ch in "。！!？?":
            out.append(_PAUSE_LONG)
        elif ch in "，,、；;：:":
            out.append(_PAUSE_SHORT)
        elif ch in "…—":
            out.append(_PAUSE_SHORT)
        else:
            out.append(ch)
    if out and not out[-1].startswith("[["):
        out.append(_PAUSE_LONG)
    return "".join(out)


def ensure_terminal(text: str) -> str:
    """保证文本以句末标点结尾，帮助模型收尾（避免尾部被截断）。"""
    t = (text or "").strip()
    if not t:
        return t
    if t[-1] in _SENT_END:
        return t
    return t + "。"


_JUNK_ONLY = re.compile(r"^[\s\u3002\uff01\uff1f\uff0c\u3001,.?!;:\uff1b\uff1a~\u2026\-\u2014\u00b7\u2018\u2019\u201c\u201d'\"()\uff08\uff09\[\]\u3010\u3011]+$")


def is_meaningful(text: str) -> bool:
    """判断识别结果是不是有意义的句子（过滤掉「。」这种纯标点噪声）。"""
    t = (text or "").strip()
    return bool(t) and not _JUNK_ONLY.match(t)
