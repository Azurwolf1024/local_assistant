"""记忆的数据模型：四级记忆里流动的就是这几种东西。

对应关系（和 ``docs/ENGINEERING_LOG.md`` 第 36 节的表一致）：

    L1 工作记忆   原始对话         sessions/session-*.jsonl（滑动窗口 + 定期清）
    L2 情景记忆   重要事件        Episode（按角色隔离）
    L3 语义记忆   事实/身份       Fact（按角色隔离，身份类 pinned）
    L4 知识库     世界观/资料     Chunk（本地文件或外部提供者）

★为什么不直接在 jsonl 里堆字典★：这四个东西都有「会被自清洁改写」的字段
（salience / confidence / recalls / last_recall），用 dataclass 写清默认值和
序列化方式，才不会出现「这个字段只有一半记录里有」的烂账。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _dataclass_from(cls, raw: dict[str, Any]):
    """只取 dataclass 认识的键（外部手改过的 json 常带多余字段，别炸）。"""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Episode:
    """L2：一件「发生过的事」（聊到的、定下来的、承诺的、感受的）。"""

    id: str = field(default_factory=lambda: new_id("ep"))
    ts: str = field(default_factory=now_iso)     # 事情发生的时间
    kind: str = "talk"                          # talk | event | decision | promise | feeling
    title: str = ""                             # 一句话标题（检索时先看它）
    summary: str = ""                           # 结构化摘要（2~4 句）
    detail: str = ""                            # 原文；旧的、不重要的会被清掉（压缩）
    who: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    salience: float = 0.5                       # 0~1 重要度；会随时间衰减
    confidence: float = 1.0                     # 摘要的可信度（LLM 总结失败的会低）
    source: str = ""                            # 来自哪个原始对话文件
    turn: int = 0                               # 原始对话里的第几轮
    event_id: int | None = None                 # 顺手写进日程/提醒的话，记下它的 id（共享层）
    links: list[str] = field(default_factory=list)   # 合并过的同类事件
    recalls: int = 0                            # 被检索命中几次（越常用越不会被清）
    last_recall: str = ""
    merged: int = 1                             # 由几条合并而来
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Episode:
        return _dataclass_from(cls, raw)


@dataclass
class Fact:
    """L3：一条「一直成立的事」——身份、称呼、喜好、习惯、忌讳。"""

    key: str = ""                               # 稳定键：self.名字 / user.称呼 / user.喜好.饮料
    value: str = ""
    about: str = "user"                         # self | user | world
    confidence: float = 0.5
    evidence: int = 1                           # 被确认过几次（反复出现 = 更可信）
    pinned: bool = False                        # ★身份类不衰减、不会被清★
    first_seen: str = field(default_factory=now_iso)
    last_seen: str = field(default_factory=now_iso)
    sources: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Fact:
        return _dataclass_from(cls, raw)


@dataclass
class Chunk:
    """L4：知识库里的一段（世界观设定、资料、外部检索结果都归它）。"""

    id: str = ""
    source: str = ""                            # 文件路径 / 外部来源标识
    title: str = ""
    text: str = ""
    keywords: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=now_iso)
    # ★这份资料「属于谁」★（空 = 共享/无主）：
    # 全知的角色会看到别人的世界观，这时必须能告诉它「这不是你的身份」（见 prompt_block）。
    owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Chunk:
        return _dataclass_from(cls, raw)


@dataclass
class Hit:
    """检索结果：一条记忆 + 它为什么被选中（分数拆开写，方便调试「它怎么没记住」）。"""

    kind: str                                   # episode | fact | chunk
    score: float
    item: Episode | Fact | Chunk
    why: dict[str, float] = field(default_factory=dict)

    @property
    def text(self) -> str:
        item = self.item
        if isinstance(item, Episode):
            return f"[{item.ts[:16]}] {item.title}：{item.summary}"
        if isinstance(item, Fact):
            return f"{item.key}：{item.value}"
        assert isinstance(item, Chunk)
        return f"{item.title or item.source}：{item.text}"
