"""（保留的旧入口）``AsrRouter`` 现在只是一个别名。

它原来把五件事揉在一个类里：引擎分支、hybrid 策略、质量评分、两级加载、推理调度。
现在拆成：

    asr/spec.py      「怎么听」的数据（主引擎 / 复核引擎 / 复核条件 / 兜底）
    asr/registry.py  发现后端、合成**配置副本**、降级链、实例缓存
    asr/quality.py   质量评分与幻觉判定（择优 + 触发复核都用它）
    asr/session.py   Hearing：策略 + 两级加载 + 择优（``AsrRouter`` 就是它）

★保留这个模块只为兼容★：``from voice_loop.asr import AsrRouter`` 这类老写法还能用，
新代码请直接用 :class:`voice_loop.asr.Hearing`，``prefer`` 也请写能力（``"fast"``）
而不是引擎名。
"""

from __future__ import annotations

from .quality import quality_score, repetition_penalty
from .session import Hearing

#: 老名字 → 新实现（同一个类，行为一致）
AsrRouter = Hearing

__all__ = ["AsrRouter", "Hearing", "quality_score", "repetition_penalty"]
