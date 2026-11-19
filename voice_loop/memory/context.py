"""L1 的连续上下文：**不可变段 + 打开中的尾**。

以前 L1 是「每次进程启动一个 `session-*.jsonl`」——早上喊一次、晚上喊一次就是两个
互不知情的文件，模型进提示词的只有 700 字摘要，答不上「我刚才说啥」。

现在 L1 长这样（每个角色一份）：

```
data/memory/<角色>/context/
  segments/seg-000001.jsonl   ← 已封存：★永不改写★（前缀缓存能命中就靠它）
  segments/seg-000002.jsonl   ← 打开中：每轮只往后追加
  digests/2026-11-19.json     ← 做梦产出：摘要 / 没聊完的事 / 决定
  dreams/2026-11-19.json      ← 那次梦改了什么（含 undo 用的前镜像）
  state.json                  ← 段号 / 序号 / 上次做梦到哪儿 / 缓存记账
```

★为什么必须「封存后不可变」★：云端的前缀缓存（DeepSeek 的上下文缓存）是按**前缀
字节**匹配的，命中部分价格约 1/10。只要旧的段一个字节都不动、新的内容只往后追加，
白天的每一轮都能吃到缓存；一夜只有「追加 digest」那一次会失效。
反过来，如果每轮都重写整段历史（以前就是这样），缓存永远命不中。

★压缩 ≠ 删除★：被封存的段仍然留在磁盘上（`recall` 照样检索得到），
只是不再逐字进提示词 —— 用 `digests/` 里那几行摘要代替。

跨进程/重启的连续性也在这里：醒来时读 `state.json`，把最后几段 + 打开中的尾放回提示词。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .model import now_iso
from .store import append_jsonl, read_json, read_jsonl, write_json

SEG_DIR = "segments"
DIGEST_DIR = "digests"
DREAM_DIR = "dreams"
STATE_FILE = "state.json"

DEFAULT_SEGMENT_TOKENS = 1200      # 一段多大就封存
DEFAULT_SEGMENT_TURNS = 30         # 或者多少轮就封存
DEFAULT_DIGEST_DAYS = 3            # 提示词里往前带几天的摘要
DEFAULT_TAIL_TOKENS = 2000         # 未压缩的尾最多占多少 token


def est_tokens(text: str) -> int:
    """粗估 token 数（中文≈1 token/字，其它≈1 token/3.5 字符）。

    ★只用来做预算和统计，不要求精确★：真值由服务端 `usage` 报，两者都记在
    `state.json` 里，可以对一下偏差。
    """
    text = str(text or "")
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    return max(1, int(cjk + (len(text) - cjk) / 3.5))


def turn_uid(ts: str, role: str, text: str, session: str = "") -> str:
    """一条 turn 的稳定 id（★同一句重复 append 不会写两遍★）。

    用内容哈希而不是自增号：这样「补写一次」是幂等的，两台机器各自记录同一句话
    也会得到同一个 uid（合并时不会重复）。
    """
    raw = f"{ts}\x1f{role}\x1f{text}\x1f{session}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class CtxTurn:
    uid: str = ""
    seq: int = 0
    ts: str = ""
    role: str = "user"                 # user | assistant
    text: str = ""
    session: str = ""                  # 来自哪个 L1 文件（溯源用）
    tools: list[str] = field(default_factory=list)
    tokens: int = 0

    def as_dict(self) -> dict:
        return {"uid": self.uid, "seq": self.seq, "ts": self.ts, "role": self.role,
                "text": self.text, "session": self.session,
                "tools": list(self.tools or []), "tokens": self.tokens}

    @classmethod
    def from_dict(cls, raw: dict) -> CtxTurn:
        text = str(raw.get("text") or "")
        ts = str(raw.get("ts") or "")
        role = str(raw.get("role") or "user")
        session = str(raw.get("session") or "")
        return cls(
            uid=str(raw.get("uid") or turn_uid(ts, role, text, session)),
            seq=int(raw.get("seq") or 0), ts=ts, role=role, text=text, session=session,
            tools=[str(t) for t in (raw.get("tools") or []) if str(t).strip()],
            tokens=int(raw.get("tokens") or est_tokens(text)),
        )

    def line(self) -> str:
        """进提示词的一行（★assistant 也要标出来★，否则模型会把自己的话当成用户说的）。"""
        who = "你" if self.role == "assistant" else "对方"
        clock = self.ts[11:16] if len(self.ts) >= 16 else ""
        return f"{clock} {who}：{self.text}"


class ContextLog:
    """一个角色的 L1 连续上下文。线程安全由调用方保证（管线里本来就串行）。"""

    def __init__(self, root: str | Path, character: str, *,
                 segment_tokens: int = DEFAULT_SEGMENT_TOKENS,
                 segment_turns: int = DEFAULT_SEGMENT_TURNS,
                 logger=None) -> None:
        self.dir = Path(root) / str(character) / "context"
        self.segments_dir = self.dir / SEG_DIR
        self.digests_dir = self.dir / DIGEST_DIR
        self.dreams_dir = self.dir / DREAM_DIR
        self.state_path = self.dir / STATE_FILE
        self.character = str(character)
        self.segment_tokens = max(200, int(segment_tokens or DEFAULT_SEGMENT_TOKENS))
        self.segment_turns = max(4, int(segment_turns or DEFAULT_SEGMENT_TURNS))
        self.log = logger

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict:
        raw = read_json(self.state_path, {}) or {}
        raw.setdefault("seg", 1)
        raw.setdefault("seq", 0)
        raw.setdefault("turns", 0)          # 打开中的段里有多少轮
        raw.setdefault("tokens", 0)         # 打开中的段有多少 token
        raw.setdefault("last_dream_seq", 0)
        raw.setdefault("last_dream_at", "")
        raw.setdefault("cache", {})
        return raw

    def _save(self, state: dict) -> None:
        write_json(self.state_path, state)

    def _ensure(self) -> None:
        self.segments_dir.mkdir(parents=True, exist_ok=True)

    def _open_path(self, state: dict | None = None) -> Path:
        seg = int((state or self.state())["seg"])
        return self.segments_dir / f"seg-{seg:06d}.jsonl"

    def seal(self, reason: str = "") -> str:
        """封存当前段：段号 +1，之后所有追加都进新段。返回被封存的文件名。

        ★封存只改段号，不动旧文件★ —— 这是「前缀不变」的全部秘密。
        """
        state = self.state()
        path = self._open_path(state)
        state["seg"] = int(state["seg"]) + 1
        state["turns"] = 0
        state["tokens"] = 0
        state["sealed_at"] = now_iso()
        state["sealed_reason"] = str(reason or "")
        self._save(state)
        if self.log is not None:
            self.log.info(f"[记忆] 封存上下文段 {path.name}（{reason or '到量'}）")
        return path.name

    # ------------------------------------------------------------------ 写
    def append(self, turns: Iterable[dict | CtxTurn], *, session: str = "") -> int:
        """追加若干轮（★uid 去重，重复调用不会写两遍★）。返回真正新增的条数。"""
        self._ensure()
        state = self.state()
        known = self.seen_uids()
        new: list[dict] = []
        for item in turns:
            turn = item if isinstance(item, CtxTurn) else CtxTurn.from_dict(item)
            if not turn.text.strip():
                continue
            if not turn.uid:
                turn.uid = turn_uid(turn.ts or now_iso(), turn.role, turn.text,
                                    turn.session or session)
            if turn.uid in known:
                continue
            known.add(turn.uid)
            state["seq"] = int(state["seq"]) + 1
            turn.seq = state["seq"]
            turn.tokens = turn.tokens or est_tokens(turn.text)
            turn.session = turn.session or session
            new.append(turn.as_dict())
            state["turns"] = int(state["turns"]) + 1
            state["tokens"] = int(state["tokens"]) + turn.tokens
        if not new:
            return 0
        append_jsonl(self._open_path(state), new)
        self._save(state)
        # 到量就封存（下一次追加自动开新段）
        if (int(state["tokens"]) >= self.segment_tokens
                or int(state["turns"]) >= self.segment_turns):
            self.seal("到量")
        return len(new)

    def seen_uids(self) -> set[str]:
        """已经记过的 uid（只读最近的段就够：同一句不会隔很久才补写）。"""
        out: set[str] = set()
        for path in self.segment_paths()[-3:]:
            for row in read_jsonl(path):
                uid = str(row.get("uid") or "")
                if uid:
                    out.add(uid)
        return out

    # ------------------------------------------------------------------ 读
    def segment_paths(self) -> list[Path]:
        if not self.segments_dir.is_dir():
            return []
        return sorted(p for p in self.segments_dir.glob("seg-*.jsonl") if p.is_file())

    def turns(self, *, after_seq: int = 0, limit: int = 0) -> list[CtxTurn]:
        """按顺序读上下文；``after_seq`` 只取这个序号之后的；``limit`` 只取最后 N 条。"""
        rows: list[CtxTurn] = []
        for path in self.segment_paths():
            for raw in read_jsonl(path):
                turn = CtxTurn.from_dict(raw)
                if turn.seq <= 0 or turn.seq <= after_seq:
                    continue
                rows.append(turn)
        rows.sort(key=lambda t: t.seq)
        if limit > 0:
            rows = rows[-limit:]
        return rows

    def new_turns(self, state: dict | None = None) -> list[CtxTurn]:
        """上次做梦之后新长出来的那些轮（★做梦的输入★）。"""
        st = state or self.state()
        return self.turns(after_seq=int(st.get("last_dream_seq") or 0))

    def counts(self) -> dict:
        st = self.state()
        return {"turns": len(self.turns()), "open_tokens": int(st["tokens"]),
                "open_turns": int(st["turns"]), "segments": len(self.segment_paths()),
                "undreamed": len(self.new_turns(st))}

    # ------------------------------------------------------------------ 摘要
    def write_digest(self, day: str, data: dict) -> Path:
        """写一天的摘要（做梦产出）。同一天再写就版本 +1。"""
        self.digests_dir.mkdir(parents=True, exist_ok=True)
        path = self.digests_dir / f"{day}.json"
        old = read_json(path, {}) or {}
        payload = {
            "date": day,
            "summary": str(data.get("summary") or ""),
            "open_threads": [str(x) for x in (data.get("open_threads") or []) if str(x).strip()],
            "decisions": [str(x) for x in (data.get("decisions") or []) if str(x).strip()],
            "turns": int(data.get("turns") or 0),
            "tokens_before": int(data.get("tokens_before") or 0),
            "tokens_after": int(data.get("tokens_after") or 0),
            "source": str(data.get("source") or "rule"),
            "dream_at": now_iso(),
            "version": int(old.get("version") or 0) + 1,
        }
        write_json(path, payload)
        return path

    def digests(self, days: int = DEFAULT_DIGEST_DAYS) -> list[dict]:
        if not self.digests_dir.is_dir():
            return []
        rows = []
        for path in sorted(self.digests_dir.glob("*.json")):
            got = read_json(path, {}) or {}
            if isinstance(got, dict) and got.get("date"):
                rows.append(got)
        return rows[-max(1, int(days)):]

    def render(self, facts_text: str = "", *, digest_days: int = DEFAULT_DIGEST_DAYS,
               tail_tokens: int = DEFAULT_TAIL_TOKENS) -> str:
        """拼提示词里的三块：事实 → 往日摘要 → 今天/最近的原文。

        ★顺序就是缓存的前缀顺序★：事实和摘要都是「一天只变一次」的东西，
        原文段在最后、只追加。所以白天后续每一轮的前缀都不动。
        """
        blocks: list[str] = []
        if facts_text.strip():
            blocks.append(facts_text.strip())
        digests = self.digests(digest_days)
        if digests:
            lines = []
            for got in digests:
                head = f"· {got.get('date')}：{got.get('summary') or ''}"
                lines.append(head)
                threads = got.get("open_threads") or []
                if threads:
                    lines.append("  （没聊完：" + "；".join(threads[:3]) + "）")
            blocks.append("【这几天】\n" + "\n".join(lines))
        tail = self._tail(tail_tokens)
        if tail:
            blocks.append("【刚才聊的】\n" + "\n".join(t.line() for t in tail))
        return "\n\n".join(blocks)

    def _tail(self, max_tokens: int) -> list[CtxTurn]:
        """取最近的若干个 turn，按 token 预算从头砍（★绝不把一句砍成半句★）。"""
        rows = self.turns()
        if not rows:
            return []
        kept: list[CtxTurn] = []
        used = 0
        for turn in reversed(rows):
            cost = turn.tokens or est_tokens(turn.text)
            if kept and used + cost > max_tokens:
                break
            kept.append(turn)
            used += cost
        return list(reversed(kept))

    # ------------------------------------------------------------------ 记账
    def record_usage(self, prompt_tokens: int = 0, hit_tokens: int = 0,
                     day: str = "") -> None:
        """记一次真实用量（云端 `usage` 里的数）——用来验证「前缀稳定」到底省了多少。"""
        state = self.state()
        bucket = state.setdefault("cache", {})
        key = day or date_cls.today().isoformat()
        row = bucket.setdefault(key, {"prompt": 0, "hit": 0, "calls": 0})
        row["prompt"] = int(row.get("prompt") or 0) + max(0, int(prompt_tokens or 0))
        row["hit"] = int(row.get("hit") or 0) + max(0, int(hit_tokens or 0))
        row["calls"] = int(row.get("calls") or 0) + 1
        # 只留最近 30 天
        for old in sorted(bucket)[:-30]:
            bucket.pop(old, None)
        self._save(state)

    def cache_stats(self, day: str = "") -> dict:
        state = self.state()
        key = day or date_cls.today().isoformat()
        row = dict((state.get("cache") or {}).get(key) or {})
        prompt = int(row.get("prompt") or 0)
        hit = int(row.get("hit") or 0)
        return {"date": key, "prompt": prompt, "hit": hit, "calls": int(row.get("calls") or 0),
                "ratio": round(hit / prompt, 3) if prompt else 0.0}

    # ------------------------------------------------------------- 做梦的账
    def mark_dreamed(self, *, seq: int, day: str) -> None:
        state = self.state()
        state["last_dream_seq"] = max(int(state.get("last_dream_seq") or 0), int(seq or 0))
        state["last_dream_at"] = now_iso()
        state["last_dream_day"] = str(day or "")
        self._save(state)

    def pending_tokens(self) -> int:
        """上次做梦之后新增内容折算的 token —— 长度阈值触发就看它。"""
        return sum(t.tokens or est_tokens(t.text) for t in self.new_turns())


__all__ = [
    "CtxTurn", "ContextLog", "DEFAULT_DIGEST_DAYS", "DEFAULT_SEGMENT_TOKENS",
    "DEFAULT_SEGMENT_TURNS", "DEFAULT_TAIL_TOKENS", "est_tokens", "turn_uid",
]
