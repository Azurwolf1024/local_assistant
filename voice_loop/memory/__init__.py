"""模型记忆管理：四级记忆 + 自清洁 + 双路检索 + 知识库。设计见 docs/ENGINEERING_LOG.md 第 36 节。

    L1 工作记忆   原始对话（滑动窗口，归档过才清）   sessions/session-*.jsonl
    L2 情景记忆   重要事件（会衰减、会合并、会用旧）  data/memory/<角色>/episodes.jsonl
    L3 语义记忆   事实与身份（pinned 不衰减）        data/memory/<角色>/facts.json
    L4 知识库     世界观/资料（可插拔）              data/knowledge/ + 各角色自己的

三条边界（刻意的）：

1. ★记忆按角色隔离★：L2/L3/L4 全在 `data/memory/<角色>/` 下，白泽不会知道凯尔希跟用户聊过什么。
2. ★日程/备忘是全角色共享的★：那是**事实**，不是某个角色的记忆。记忆层通过注入进来的
   `schedule` 接口读写它（`voice_loop/events.py` 那一份），所以「隔离」和「共享」各归各位。
3. ★写记忆绝不能挡住说话★：归档有异常只记日志；LLM 总结失败就退化成规则摘要（见 extract.py）。

用法（程序里）：

    hub = MemoryHub(settings)                 # 一个进程一个 hub
    mem = hub.for_character("baize")          # 按角色取记忆（自动隔离）
    hits = mem.recall("上次组会改到几点", when="上周")
    print(mem.prompt_block("组会时间"))

命令行：`python main.py memory --help`
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol

from ..store import cross_process_lock
from .extract import Turn, episodes_from_session, facts_from_text, rule_summary, signals
from .knowledge import KnowledgeBase, build_knowledge
from .levels import (
    compact_episodes,
    keywords_of,
    merge_episodes,
    merge_facts,
    parse_ts,
    pick_old_sessions,
    promote_to_facts,
    prune_episodes,
    prune_facts,
    salience_eff,
    touch_episode,
    touch_fact,
)
from .model import Chunk, Episode, Fact, Hit, now_iso
from .retrieve import in_window, parse_when, rank, score_chunk, score_episode, score_fact
from .store import MemoryPaths, append_jsonl, read_json, read_jsonl, rewrite_jsonl, session_key, write_json

# 默认上限（config.toml 的 [memory] 可以改）
MAX_EPISODES = 800
MAX_FACTS = 200
KEEP_SESSION_DAYS = 30
# ★这是「保底留几个」不是「最多留几个」★：写成 200 就会变成「永远都不到 200 个文件 →
# 什么都不删」，滑动窗口直接死掉（实测踩过）。给它一个小数才对：再老也留最近这 10 个。
KEEP_SESSION_FILES = 10


class Schedule(Protocol):
    """日程的读写口子（由 pipeline 把现有的 events/skills 包一层传进来）。

    ★为什么用协议而不是直接 import events★：记忆层不该知道日程存在哪里
    （它只是「有个共享的、能读能写的地方」），这样测试里塞个假的就行。
    """

    def add_event(self, title: str, when: str, **kwargs) -> int | None:
        ...

    def upcoming(self, days: int = 7) -> list[dict]:
        ...


class NullSchedule:
    """没接日程时的替身（记忆照常工作，只是不写日程）。"""

    def add_event(self, title: str, when: str, **kwargs) -> int | None:  # noqa: ARG002
        return None

    def upcoming(self, days: int = 7) -> list[dict]:  # noqa: ARG002
        return []


class Memory:
    """一个角色的四级记忆。★一个角色一个实例★（隔离就是靠这个）。"""

    def __init__(
        self,
        root: str | Path,
        character: str,
        *,
        knowledge: KnowledgeBase | None = None,
        schedule: Schedule | None = None,
        llm_call: Callable[[str], str] | None = None,
        logger: logging.Logger | None = None,
        max_episodes: int = MAX_EPISODES,
        max_facts: int = MAX_FACTS,
        keep_session_days: float = KEEP_SESSION_DAYS,
        keep_session_files: int = KEEP_SESSION_FILES,
    ) -> None:
        self.paths = MemoryPaths(root, character)
        self.character = self.paths.character
        self.knowledge = knowledge or KnowledgeBase()
        self.schedule = schedule or NullSchedule()
        self.llm_call = llm_call
        self.log = logger or logging.getLogger("voice_loop.memory")
        self.max_episodes = max_episodes
        self.max_facts = max_facts
        self.keep_session_days = keep_session_days
        self.keep_session_files = keep_session_files

    # ------------------------------------------------------------------ 读
    def episodes(self) -> list[Episode]:
        return [Episode.from_dict(raw) for raw in read_jsonl(self.paths.episodes)]

    def facts(self, include_weak: bool = False) -> list[Fact]:
        rows = [Fact.from_dict(raw) for raw in read_json(self.paths.facts, [])]
        if include_weak:
            return rows
        now = datetime.now()
        return [f for f in rows if f.pinned or f.confidence >= 0.15]

    def session_states(self) -> dict[str, dict]:
        return read_json(self.paths.sessions, {}) or {}

    def recall(self, query: str = "", when: str | datetime | tuple | None = None,
               limit: int = 8, now: datetime | None = None,
               use_knowledge: bool = True) -> list[Hit]:
        """检索：`when` 可以是中文（"上周三"）、datetime 或 (起, 止)。

        ★命中就给记忆「回血」★：事件 recalls+1、事实 last_seen 刷新 —— 这是「越用越牢」
        的实现点（写回失败也不影响本次返回）。
        """
        moment = now or datetime.now()
        window = self._window(when, moment)
        tokens = set(keywords_of(query)) if query.strip() else set()

        hits: list[Hit] = []
        episodes = self.episodes()
        touched: list[Episode] = []
        for ep in episodes:
            if window is not None and not in_window(ep.ts, window):
                continue
            score, why = score_episode(ep, tokens, moment, window)
            # 只按时间问（没给内容）时，时间窗内的都算候选
            if not tokens and window is not None:
                score, why = max(score, 0.4), {**why, "time_only": 1.0}
            if score >= 0.12:
                hits.append(Hit(kind="episode", score=score, item=ep, why=why))
        for fact in self.facts():
            # ★纯时间查询不返回事实★：事实没有时间（「上周」和「我住在哪」没关系），
            # 混进去只会把时间检索的答案冲淡；带了内容词就照常参与。
            if window is not None and not tokens:
                continue
            score, why = score_fact(fact, tokens, moment)
            if score >= 0.2:
                hits.append(Hit(kind="fact", score=score, item=fact, why=why))
        if use_knowledge:
            for chunk in self.knowledge.chunks():
                score, why = score_chunk(chunk, tokens, moment)
                if score >= 0.12:
                    hits.append(Hit(kind="chunk", score=score, item=chunk, why=why))

        picked = rank(hits, limit=limit)
        # 回血：只给真被返回的（别让「差一点没选中」的也涨分）
        for hit in picked:
            if isinstance(hit.item, Episode):
                touch_episode(hit.item, moment)
                touched.append(hit.item)
            elif isinstance(hit.item, Fact):
                touch_fact(hit.item, moment)
        if touched:
            self._rewrite_episodes(episodes)
        return picked

    def prompt_block(self, query: str = "", max_chars: int = 700,
                     now: datetime | None = None) -> str:
        """拼一段可以塞进提示词的「记忆摘要」（有硬上限，绝不把上下文撑爆）。"""
        moment = now or datetime.now()
        lines: list[str] = []
        facts = self.facts()
        pinned = [f for f in facts if f.pinned]
        if pinned:
            lines.append("身份：" + "；".join(f"{f.key.split('.')[-1]}={f.value}" for f in pinned[:6]))
        others = sorted([f for f in facts if not f.pinned],
                        key=lambda f: (f.confidence, f.evidence), reverse=True)
        if others:
            lines.append("关于你：" + "；".join(f"{f.value}" for f in others[:5]))
        recent = sorted(self.episodes(), key=lambda e: e.ts, reverse=True)[:3]
        if recent:
            lines.append("最近：" + "；".join(f"{e.ts[5:10]} {e.title}" for e in recent))
        if query.strip():
            related = [h for h in self.recall(query, limit=3) if h.kind == "episode"]
            if related:
                lines.append("相关：" + "；".join(f"{h.item.ts[5:10]} {h.item.title}" for h in related))
        upcoming = self.schedule.upcoming(days=2)
        if upcoming:
            lines.append("日程（共享）：" + "；".join(
                f"{str(row.get('start') or '')[:16]} {row.get('title') or ''}" for row in upcoming[:4]))
        if not lines:
            return ""
        text = "【记忆】\n" + "\n".join(f"· {line}" for line in lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n· （记忆太长，已截断）"
        return text

    @staticmethod
    def _window(when, moment: datetime) -> tuple[datetime, datetime] | None:
        if when is None:
            return None
        if isinstance(when, tuple) and len(when) == 2:
            return when[0], when[1]
        if isinstance(when, datetime):
            return when, moment
        return parse_when(str(when), moment)

    # ------------------------------------------------------------------ 写
    def seed_identity(self, name: str, title: str = "", user_title: str = "") -> int:
        """L3 的「深层记忆」起点：自己的名字、身份、对用户的称呼。

        ★这部分必须 pinned★：pinned 的事实不衰减、不会被淘汰 —— 人脑里
        「我叫什么」这种也是永不褪色的那一类。由 pipeline 在角色生效时调一次（幂等）。
        """
        seeds = [
            Fact(key="self.名字", value=str(name).strip(), about="self", confidence=1.0,
                 pinned=True, note="身份（人格文件）"),
            Fact(key="self.身份", value=str(title).strip(), about="self", confidence=1.0,
                 pinned=True, note="身份（人格文件）"),
            Fact(key="user.称呼", value=str(user_title).strip(), about="user", confidence=1.0,
                 pinned=True, note="对用户的称呼（人格文件）"),
        ]
        seeds = [f for f in seeds if f.value]
        if not seeds:
            return 0
        with cross_process_lock(self.paths.facts):
            current = [Fact.from_dict(raw) for raw in read_json(self.paths.facts, [])]
            existing = {f.key: f for f in current}
            added = 0
            for fact in seeds:
                hit = existing.get(fact.key)
                if hit is None:
                    current.append(fact)
                    added += 1
                else:
                    # 人格文件是唯一真相：值变了就跟着改（用户改称呼也应即时生效）
                    if hit.value != fact.value:
                        hit.value = fact.value
                        hit.last_seen = now_iso()
                    hit.pinned = True
                    hit.confidence = 1.0
            write_json(self.paths.facts, [f.to_dict() for f in current])
        return added

    def note(self, title: str, summary: str = "", *, kind: str = "event",
             when: str | None = None, turn: int = 0, detail: str = "",
             event_id: int | None = None, salience: float = 0.75) -> Episode:
        """记一件事（pipeline 在技能真的写动日程/提醒时调它）。"""
        episode = Episode(
            ts=when or now_iso(), kind=kind, title=title[:80], summary=summary[:400],
            detail=detail[:600], keywords=keywords_of(f"{title} {summary}", prefer=title),
            salience=salience, source="", turn=turn, event_id=event_id,
        )
        append_jsonl(self.paths.episodes, [episode.to_dict()])
        return episode

    def remember(self, text: str, *, kind: str = "talk", when: str | None = None,
                 turn: int = 0) -> tuple[Episode, list[Fact]]:
        """把一句话按信号记成事件/事实（用户在对话里说「记住…」时走这条）。"""
        sig = signals(text)
        episode = Episode(
            ts=when or now_iso(), kind=kind if kind != "talk" else (sorted(sig.kinds)[0] if sig.kinds else "talk"),
            title=text.strip()[:60], summary=text.strip()[:300], detail=text.strip()[:600],
            keywords=sig.keywords, salience=max(sig.salience, 0.6), confidence=0.8, turn=turn,
        )
        append_jsonl(self.paths.episodes, [episode.to_dict()])
        facts = facts_from_text(text)
        if facts:
            self._upsert_facts(facts)
        return episode, facts

    def ingest_session(self, session_path: str | Path,
                       llm_call: Callable[[str], str] | None = None,
                       hints: Iterable[dict] | None = None) -> dict:
        """把一段原始对话归档成 L2/L3（幂等：同一个文件不重复提取）。"""
        path = Path(session_path)
        key = session_key(path)
        states = self.session_states()
        if (states.get(key) or {}).get("extracted"):
            return {"session": key, "skipped": True}
        turns = _read_turns(path)
        episodes, facts = episodes_from_session(
            turns, source=key, llm_call=llm_call or self.llm_call, hints=hints)
        if episodes:
            append_jsonl(self.paths.episodes, [e.to_dict() for e in episodes])
        if facts:
            self._upsert_facts(facts)
        stamps = [t for t in (parse_ts(e.ts) for e in episodes) if t]
        states[key] = {
            "extracted": True,
            "extracted_at": now_iso(),
            "turns": len(turns),
            "episodes": len(episodes),
            "facts": len(facts),
            "first_ts": min(stamps).isoformat(sep=" ") if stamps else "",
            "last_ts": max(stamps).isoformat(sep=" ") if stamps else now_iso(),
        }
        write_json(self.paths.sessions, states)
        return {"session": key, "episodes": len(episodes), "facts": len(facts), "turns": len(turns)}

    def consolidate(self, now: datetime | None = None) -> dict:
        """★自清洁／记忆巩固★：合并同类、压缩旧文、按上限淘汰、L2→L3 提升。

        像睡觉时大脑整理记忆那样**一遍过**，不改事实本身，只调整权重与冗余。
        在「会话结束 / 空闲 / 手动跑 CLI」时调用（幂等：跑第二遍基本什么都不动）。
        """
        moment = now or datetime.now()
        before_ep, before_facts = self.episodes(), self.facts(include_weak=True)
        episodes, merged = merge_episodes(before_ep)
        compacted = compact_episodes(episodes, moment)
        episodes, dropped_ep = prune_episodes(episodes, moment, self.max_episodes)
        self._rewrite_episodes(episodes)

        facts = promote_to_facts(episodes, list(before_facts))
        facts = merge_facts(facts)
        facts, dropped_facts = prune_facts(facts, moment, self.max_facts)
        write_json(self.paths.facts, [f.to_dict() for f in facts])

        report = {
            "episodes": len(episodes), "merged": merged, "compacted": compacted,
            "dropped_episodes": dropped_ep, "facts": len(facts),
            "dropped_facts": dropped_facts, "promoted": len(facts) - len(before_facts),
        }
        self.log.info(f"[记忆] 巩固完成：{report}")
        return report

    def prune_raw_sessions(self, sessions_dir: str | Path, now: datetime | None = None,
                           dry_run: bool = True) -> list[str]:
        """L1 滑动窗口：删掉超期的**原始对话**（★只删已归档的★）。

        返回被删（或将被删，`dry_run=True` 时）的文件名列表。
        """
        moment = now or datetime.now()
        states = self.session_states()
        victims = pick_old_sessions(states, moment, self.keep_session_days, self.keep_session_files)
        if dry_run:
            return victims
        removed: list[str] = []
        for name in victims:
            path = Path(sessions_dir) / name
            try:
                path.unlink()
                removed.append(name)
            except OSError as exc:
                self.log.warning(f"[记忆] 删不掉 {name}：{exc}")
        for name in removed:
            states.pop(name, None)
        write_json(self.paths.sessions, states)
        return removed

    def forget(self, key_or_id: str) -> bool:
        """忘掉一条（按 episode id / fact key，或 key 的前缀匹配）。"""
        target = (key_or_id or "").strip()
        if not target:
            return False
        episodes = self.episodes()
        kept = [e for e in episodes if e.id != target]
        if len(kept) != len(episodes):
            self._rewrite_episodes(kept)
            return True
        facts = self.facts(include_weak=True)
        kept_facts = [f for f in facts if f.key != target and not f.key.startswith(target + ".")]
        if len(kept_facts) != len(facts):
            write_json(self.paths.facts, [f.to_dict() for f in kept_facts])
            return True
        return False

    def _upsert_facts(self, incoming: list[Fact]) -> None:
        with cross_process_lock(self.paths.facts):
            current = [Fact.from_dict(raw) for raw in read_json(self.paths.facts, [])]
            for fact in incoming:
                hit = next((f for f in current if f.key == fact.key), None)
                if hit is None:
                    current.append(fact)
                    continue
                hit.evidence += 1
                hit.confidence = min(1.0, max(hit.confidence, fact.confidence) + 0.05)
                hit.last_seen = now_iso()
                hit.sources = sorted(set(hit.sources) | set(fact.sources))[:10]
                if fact.value != hit.value:
                    # 值变了 = 改主意了：以新的为准（旧值留在 note 里可追）
                    hit.note = f"（原 {hit.value}）{fact.note}"[:200]
                    hit.value = fact.value
            write_json(self.paths.facts, [f.to_dict() for f in current])

    def _rewrite_episodes(self, episodes: list[Episode]) -> None:
        rewrite_jsonl(self.paths.episodes, [e.to_dict() for e in episodes])

    # ------------------------------------------------------------------ 其他
    def stats(self) -> dict:
        episodes = self.episodes()
        facts = self.facts(include_weak=True)
        now = datetime.now()
        return {
            "character": self.character,
            "episodes": len(episodes),
            "facts": len(facts),
            "pinned": len([f for f in facts if f.pinned]),
            "sessions_tracked": len(self.session_states()),
            "avg_salience": round(sum(salience_eff(e, now) for e in episodes) / len(episodes), 3)
            if episodes else 0.0,
            "knowledge": self.knowledge.stats(),
            "dir": str(self.paths.dir),
        }


class MemoryHub:
    """按角色分发记忆（★隔离★），并持有共享的日程接口与全局知识库。"""

    def __init__(self, settings=None, *, root: str | Path | None = None,
                 schedule: Schedule | None = None,
                 llm_call: Callable[[str], str] | None = None,
                 logger: logging.Logger | None = None,
                 global_knowledge: KnowledgeBase | None = None) -> None:
        cfg = getattr(settings, "memory", None)
        configured = str(getattr(cfg, "dir", "") or "").strip()
        if configured:
            self.root = Path(configured)
        else:
            # ★留空 = 跟 [skills] data_dir 走★：记忆属于项目数据，应该和别的数据在一起。
            # 好处是「测试/多开把 data_dir 指到临时目录」时记忆自动跟着隔离，
            # 不用让每个测试文件都记得关记忆（不然自测会把测试对话写进真实记忆库）。
            data_dir = str(getattr(getattr(settings, "skills", None), "data_dir", "data") or "data")
            self.root = Path(root) if root else (Path(data_dir) / "memory")
        if settings is not None and not self.root.is_absolute():
            self.root = Path(settings.root) / self.root
        self.settings = settings
        self.schedule = schedule or NullSchedule()
        self.llm_call = llm_call
        self.log = logger or logging.getLogger("voice_loop.memory")
        self.max_episodes = int(getattr(cfg, "max_episodes", MAX_EPISODES) or MAX_EPISODES)
        self.max_facts = int(getattr(cfg, "max_facts", MAX_FACTS) or MAX_FACTS)
        self.keep_session_days = float(getattr(cfg, "keep_session_days", KEEP_SESSION_DAYS) or KEEP_SESSION_DAYS)
        self.keep_session_files = int(getattr(cfg, "keep_session_files", KEEP_SESSION_FILES)
                                      or KEEP_SESSION_FILES)
        self.enabled = bool(getattr(cfg, "enabled", True))
        self.inject = bool(getattr(cfg, "inject", True))
        # 全局知识库：data/knowledge/ + 配置里 inline 的设定（每个角色都能看到）
        paths = list(getattr(cfg, "knowledge_paths", None) or [])
        if not paths:
            paths = ["data/knowledge"]
        inline = [(getattr(cfg, "world_title", "设定"), getattr(cfg, "world", ""))]
        self.knowledge = global_knowledge or build_knowledge(
            [str(Path(settings.root) / p) if settings is not None and not Path(p).is_absolute() else p
             for p in paths], inline)
        self._cache: dict[str, Memory] = {}

    def for_character(self, character: str | None) -> Memory:
        """取某个角色的记忆（同一个角色只建一个实例）。"""
        key = (character or "default").strip() or "default"
        got = self._cache.get(key)
        if got is None:
            got = self._cache[key] = Memory(
                self.root, key, knowledge=self.knowledge, schedule=self.schedule,
                llm_call=self.llm_call, logger=self.log,
                max_episodes=self.max_episodes, max_facts=self.max_facts,
                keep_session_days=self.keep_session_days,
                keep_session_files=self.keep_session_files,
            )
        return got

    def characters(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def consolidate_all(self, now: datetime | None = None) -> dict[str, dict]:
        return {name: self.for_character(name).consolidate(now) for name in self.characters()}

    def stats(self) -> dict:
        return {
            "root": str(self.root), "enabled": self.enabled,
            "characters": self.characters(),
            "knowledge": self.knowledge.stats(),
        }


def _read_turns(path: Path) -> list[Turn]:
    from .extract import read_session, Turn as TurnCls  # noqa: PLC0415

    return [TurnCls(turn=t.turn, user=t.user, answer=t.answer) for t in read_session(path)]


__all__ = [
    "Chunk", "Episode", "Fact", "Hit", "KnowledgeBase", "Memory", "MemoryHub", "NullSchedule",
    "Schedule", "build_knowledge", "rule_summary",
]
