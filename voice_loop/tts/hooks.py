"""TTS 与「谁帮我转写参考音频」之间的一个空位。

为什么需要它
    ZipVoice 克隆声线时，参考音频如果没配逐字文本，要用识别引擎转写一遍。
    以前 ``zipvoice_tts.py`` 直接 ``from ..asr import Hearing`` —— 于是
    **TTS 认识了 ASR**，反过来 ASR 的评测又要用 TTS 合成素材，两个模块互相依赖，
    谁也没法单独拿走、单独优化。

    现在改成**注入**：管线（或任何调用方）在启动时把「转写能力」放进来，
    `voice_loop/tts/` 一个 `asr` 的 import 都没有。没注入时就是「没有这个能力」，
    参考文本留空即可（可以让用户手填 `.txt`，那本来就是更准的做法）。

约束
    ★这里只放一个函数指针，不放任何依赖★：不要让这个文件 import numpy 之外的
    项目内模块，否则「解耦」就又破了一次。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: 转写函数：``(samples, sample_rate) -> 文本``
Transcriber = Callable[[Any, int], str]

_transcriber: Transcriber | None = None
_source: str = ""


def set_transcriber(fn: Transcriber | None, *, source: str = "") -> None:
    """注入转写能力（``None`` = 关掉）。``source`` 只给日志用。"""
    global _transcriber, _source
    _transcriber = fn
    _source = str(source or "")


def get_transcriber() -> Transcriber | None:
    return _transcriber


def transcriber_source() -> str:
    """现在这把能力是谁给的（日志/自检里说清楚，免得「自动转写不可用」无从查起）。"""
    return _source


def transcribe(samples: Any, sample_rate: int) -> str:
    """用注入的转写能力转一段音频；没注入就返回空串（调用方当"没有文本"处理）。"""
    if _transcriber is None:
        return ""
    try:
        return str(_transcriber(samples, int(sample_rate)) or "").strip()
    except Exception:  # noqa: BLE001 - 转写失败绝不能影响出声
        return ""


__all__ = ["Transcriber", "get_transcriber", "set_transcriber", "transcribe",
           "transcriber_source"]
