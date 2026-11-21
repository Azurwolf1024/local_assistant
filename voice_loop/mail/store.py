"""本地索引（SQLite）：邮件元数据、分类结果、通知去重、待批草稿。

为什么必须有本地库（而不是每次问 Graph）
    - Graph 的 ``$search`` 对中文很弱，``$filter`` 也不支持全文；想要「上周导师发的报销」
      这种查询，只能本地先建索引。
    - 重要度分类要**留结果**：同一封只分类一次，模型换了才重算（省 token、也稳）。
    - 「同一封只提醒一次」「这封已经回过了」这类判断需要持久状态。
    - 正文按需再拉（``body_text`` 有 TTL），索引里默认只存 ``bodyPreview``。

不用 FTS5：中文分词在 SQLite 里没有开箱即用的实现，``LIKE '%词%'`` 对几千封邮件足够快，
而且行为完全可预测（不会出现「搜不到但明明有」这种 FTS 分词坑）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT DEFAULT '',
    internet_id TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    sender TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    to_json TEXT DEFAULT '[]',
    cc_json TEXT DEFAULT '[]',
    received_at TEXT DEFAULT '',
    is_read INTEGER DEFAULT 0,
    has_attachments INTEGER DEFAULT 0,
    importance TEXT DEFAULT '',
    preview TEXT DEFAULT '',
    web_link TEXT DEFAULT '',
    flagged INTEGER DEFAULT 0,
    body_text TEXT DEFAULT '',
    body_cached_at REAL DEFAULT 0,
    score INTEGER DEFAULT 0,
    level TEXT DEFAULT '',
    category TEXT DEFAULT '',
    needs_reply INTEGER DEFAULT 0,
    deadline TEXT DEFAULT '',
    one_line TEXT DEFAULT '',
    triaged_at REAL DEFAULT 0,
    triaged_model TEXT DEFAULT '',
    notified_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_received ON messages(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_level ON messages(level, received_at DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS attachments (
    message_id TEXT NOT NULL,
    att_id TEXT NOT NULL,
    name TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    is_inline INTEGER DEFAULT 0,
    text TEXT DEFAULT '',
    cached_at REAL DEFAULT 0,
    PRIMARY KEY (message_id, att_id)
);
CREATE INDEX IF NOT EXISTS idx_att_name ON attachments(name);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    message_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    when_text TEXT DEFAULT '',
    when_iso TEXT DEFAULT '',
    place TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    state TEXT DEFAULT 'pending',
    created_at REAL DEFAULT 0,
    decided_at REAL DEFAULT 0,
    event_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    created_at REAL DEFAULT 0,
    to_json TEXT DEFAULT '[]',
    cc_json TEXT DEFAULT '[]',
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    reply_to TEXT DEFAULT '',
    graph_draft_id TEXT DEFAULT '',
    state TEXT DEFAULT 'pending',
    body_hash TEXT DEFAULT '',
    approved_at REAL DEFAULT 0,
    approved_by TEXT DEFAULT '',
    expires_at REAL DEFAULT 0,
    sent_at REAL DEFAULT 0,
    error TEXT DEFAULT ''
);
"""

FIELDS = (
    "id", "conversation_id", "internet_id", "subject", "sender", "sender_name",
    "to_json", "cc_json", "received_at", "is_read", "has_attachments", "importance",
    "preview", "web_link", "flagged", "body_text", "body_cached_at",
    "score", "level", "category", "needs_reply", "deadline", "one_line",
    "triaged_at", "triaged_model", "notified_at",
)


def _split_tokens(query: str) -> list[str]:
    """把查询拆成关键词：中文按字块、英文按词全保留（不做词干化，够用）。"""
    parts = [p for p in re.split(r"[\s,，。;；、]+", str(query or "")) if p]
    return parts[:8]


class MailStore:
    def __init__(self, path: str | Path, log: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    def _exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cur

    def _query(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, tuple(args)))

    # ------------------------------------------------------------------ meta
    def meta_get(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else default

    def meta_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    # ------------------------------------------------------------------ 邮件
    def upsert_messages(self, rows: list[dict]) -> tuple[int, int]:
        """写入/更新一批邮件。返回 ``(新增, 更新)``。"""
        added = updated = 0
        for raw in rows:
            mid = str(raw.get("id") or "")
            if not mid:
                continue
            exists = self._query("SELECT id FROM messages WHERE id = ?", (mid,))
            data = {
                "id": mid,
                "conversation_id": str(raw.get("conversation_id") or ""),
                "internet_id": str(raw.get("internet_id") or ""),
                "subject": str(raw.get("subject") or ""),
                "sender": str(raw.get("sender") or "").lower(),
                "sender_name": str(raw.get("sender_name") or ""),
                "to_json": json.dumps(raw.get("to") or [], ensure_ascii=False),
                "cc_json": json.dumps(raw.get("cc") or [], ensure_ascii=False),
                "received_at": str(raw.get("received_at") or ""),
                "is_read": 1 if raw.get("is_read") else 0,
                "has_attachments": 1 if raw.get("has_attachments") else 0,
                "importance": str(raw.get("importance") or ""),
                "preview": str(raw.get("preview") or ""),
                "web_link": str(raw.get("web_link") or ""),
                "flagged": 1 if raw.get("flagged") else 0,
            }
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            if exists:
                sets = ", ".join(f"{k} = ?" for k in data if k != "id")
                values = [v for k, v in data.items() if k != "id"] + [mid]
                self._exec(f"UPDATE messages SET {sets} WHERE id = ?", values)
                updated += 1
            else:
                self._exec(f"INSERT INTO messages({cols}) VALUES({marks})", list(data.values()))
                added += 1
        return added, updated

    def get(self, message_id: str) -> dict | None:
        rows = self._query("SELECT * FROM messages WHERE id = ?", (message_id,))
        return self._row(rows[0]) if rows else None

    def by_internet_id(self, internet_id: str) -> dict | None:
        rows = self._query("SELECT * FROM messages WHERE internet_id = ?", (internet_id,))
        return self._row(rows[0]) if rows else None

    def set_body(self, message_id: str, body_text: str) -> None:
        self._exec("UPDATE messages SET body_text = ?, body_cached_at = ? WHERE id = ?",
                   (str(body_text or ""), time.time(), message_id))

    def set_triage(self, message_id: str, info: dict) -> None:
        self._exec(
            "UPDATE messages SET score = ?, level = ?, category = ?, needs_reply = ?,"
            " deadline = ?, one_line = ?, triaged_at = ?, triaged_model = ? WHERE id = ?",
            (
                int(info.get("score") or 0),
                str(info.get("level") or ""),
                str(info.get("category") or ""),
                1 if info.get("needs_reply") else 0,
                str(info.get("deadline") or ""),
                str(info.get("one_line") or ""),
                time.time(),
                str(info.get("model") or ""),
                message_id,
            ),
        )

    def mark_notified(self, message_id: str) -> None:
        self._exec("UPDATE messages SET notified_at = ? WHERE id = ?",
                   (time.time(), message_id))

    def search(
        self,
        query: str = "",
        *,
        limit: int = 20,
        unread_only: bool = False,
        level: str = "",
        since_iso: str = "",
        sender: str = "",
    ) -> list[dict]:
        sql = "SELECT * FROM messages WHERE 1 = 1"
        args: list[Any] = []
        if unread_only:
            sql += " AND is_read = 0"
        if level:
            sql += " AND level = ?"
            args.append(level)
        if since_iso:
            sql += " AND received_at >= ?"
            args.append(since_iso)
        if sender:
            sql += " AND sender LIKE ?"
            args.append(f"%{sender.lower()}%")
        for token in _split_tokens(query):
            # 附件名也参与检索：问「那个 PDF」「附件里有什么」时得找得到
            sql += (" AND (subject LIKE ? OR sender LIKE ? OR sender_name LIKE ?"
                    " OR preview LIKE ? OR body_text LIKE ?"
                    " OR id IN (SELECT message_id FROM attachments WHERE name LIKE ?))")
            like = f"%{token}%"
            args.extend([like, like, like, like, like, like])
        sql += " ORDER BY received_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [self._row(r) for r in self._query(sql, args)]

    # ------------------------------------------------------------------ 附件
    def set_attachments(self, message_id: str, items: list[dict]) -> None:
        """整批替换这封邮件的附件清单（Graph 每次都返回全量，replace 最简单）。"""
        self._exec("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        for item in items or []:
            self._exec(
                "INSERT OR REPLACE INTO attachments(message_id, att_id, name, content_type,"
                " size, is_inline, text, cached_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    message_id, str(item.get("id") or ""), str(item.get("name") or ""),
                    str(item.get("content_type") or ""), int(item.get("size") or 0),
                    1 if item.get("is_inline") else 0, str(item.get("text") or ""),
                    float(item.get("cached_at") or 0.0),
                ),
            )

    def attachments_of(self, message_id: str) -> list[dict]:
        rows = self._query(
            "SELECT * FROM attachments WHERE message_id = ? ORDER BY is_inline, name",
            (message_id,),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["is_inline"] = bool(item.get("is_inline"))
            out.append(item)
        return out

    def set_attachment_text(self, message_id: str, att_id: str, text: str) -> None:
        self._exec(
            "UPDATE attachments SET text = ?, cached_at = ? WHERE message_id = ? AND att_id = ?",
            (str(text or ""), time.time(), message_id, att_id),
        )

    def recent(self, *, limit: int = 20, level: str = "") -> list[dict]:
        return self.search("", limit=limit, level=level)

    def untriaged(self, *, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT * FROM messages WHERE triaged_at = 0 ORDER BY received_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [self._row(r) for r in rows]

    def unnotified(self, *, levels: tuple[str, ...] = ("high",), limit: int = 20) -> list[dict]:
        marks = ", ".join("?" for _ in levels)
        rows = self._query(
            f"SELECT * FROM messages WHERE notified_at = 0 AND level IN ({marks})"
            " ORDER BY score DESC, received_at DESC LIMIT ?",
            (*levels, max(1, int(limit))),
        )
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, Any]:
        rows = self._query(
            "SELECT level, COUNT(*) AS n FROM messages GROUP BY level")
        by_level = {str(r["level"] or "?"): int(r["n"]) for r in rows}
        total = self._query("SELECT COUNT(*) FROM messages")[0][0]
        unread = self._query("SELECT COUNT(*) FROM messages WHERE is_read = 0")[0][0]
        drafts = self._query(
            "SELECT COUNT(*) FROM drafts WHERE state IN ('pending','approved')")[0][0]
        return {"total": int(total), "unread": int(unread),
                "by_level": by_level, "pending_drafts": int(drafts),
                "broken": self.broken_count(), "newest_at": self.newest_at()}

    def newest_at(self) -> str:
        """索引里最新一封的时间（ISO，UTC）。空库返回空串。"""
        row = self._query("SELECT MAX(received_at) AS m FROM messages")
        return str((row[0]["m"] if row else "") or "")

    def broken_count(self) -> int:
        """「只有 id、没有时间」的空行有多少（正常应该是 0）。"""
        row = self._query(
            "SELECT COUNT(*) FROM messages WHERE COALESCE(received_at, '') = ''")
        return int(row[0][0] if row else 0)

    # ------------------------------------------------------------------ 删除 / 清理
    def delete_messages(self, ids: list[str]) -> int:
        """按 id 删本地行（Graph 的删除通知）。返回真的删了几行。"""
        gone = 0
        for message_id in ids or []:
            mid = str(message_id or "")
            if not mid:
                continue
            hit = self._query("SELECT id FROM messages WHERE id = ?", (mid,))
            if not hit:
                continue
            self._exec("DELETE FROM attachments WHERE message_id = ?", (mid,))
            self._exec("DELETE FROM messages WHERE id = ?", (mid,))
            gone += 1
        return gone

    def drop_broken(self) -> int:
        """清掉历史遗留的空行（只有 id：没有时间、没有发件人）。

        为什么要有它：2026-11-21 那次「游标形状不对」的事故，把几十封新邮件写成了
        只有 id 的行 —— 搜不到、也不能分级，只会在每次同步里被反复挑出来烧 token。
        另外这类行**不该被补**：它们的 id 还在，下次真变化时会被正常覆盖。
        """
        rows = self._query(
            "SELECT id FROM messages WHERE COALESCE(received_at, '') = ''")
        return self.delete_messages([str(r["id"]) for r in rows])

    def prune(self, days: int) -> int:
        """删掉本地索引里 ``days`` 天以前的邮件（**只动本地库**，不碰 Outlook）。

        为什么要它：索引越久，搜索越慢，而且巡检会一直翻旧邮件要不要分级 ——
        全是白花的 token。不想留就定期跑一次 ``python main.py mail prune``。
        """
        days = int(days or 0)
        if days <= 0:
            return 0
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() - days * 86400))
        rows = self._query("SELECT id FROM messages WHERE received_at < ?", (cutoff,))
        ids = [str(r["id"]) for r in rows]
        for message_id in ids:
            self._exec("DELETE FROM attachments WHERE message_id = ?", (message_id,))
            self._exec("DELETE FROM messages WHERE id = ?", (message_id,))
        return len(ids)

    # ------------------------------------------------------------------ 草稿
    def create_draft(self, draft: dict) -> None:
        self._exec(
            "INSERT INTO drafts(id, created_at, to_json, cc_json, subject, body, reply_to,"
            " graph_draft_id, state, body_hash, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(draft["id"]), float(draft.get("created_at") or time.time()),
                json.dumps(draft.get("to") or [], ensure_ascii=False),
                json.dumps(draft.get("cc") or [], ensure_ascii=False),
                str(draft.get("subject") or ""), str(draft.get("body") or ""),
                str(draft.get("reply_to") or ""), str(draft.get("graph_draft_id") or ""),
                str(draft.get("state") or "pending"), str(draft.get("body_hash") or ""),
                float(draft.get("expires_at") or 0.0),
            ),
        )

    def get_draft(self, draft_id: str) -> dict | None:
        rows = self._query("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["to"] = json.loads(row.get("to_json") or "[]")
        row["cc"] = json.loads(row.get("cc_json") or "[]")
        return row

    def drafts(self, *, states: tuple[str, ...] = ("pending", "approved")) -> list[dict]:
        marks = ", ".join("?" for _ in states)
        rows = self._query(
            f"SELECT id FROM drafts WHERE state IN ({marks}) ORDER BY created_at DESC",
            states,
        )
        out = []
        for row in rows:
            got = self.get_draft(str(row["id"]))
            if got:
                out.append(got)
        return out

    def set_draft(self, draft_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE drafts SET {sets} WHERE id = ?",
                   [*fields.values(), draft_id])

    # ------------------------------------------------------------------ 事件提议
    def create_proposal(self, item: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO proposals(id, message_id, title, when_text, when_iso,"
            " place, source, reason, state, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(item["id"]), str(item.get("message_id") or ""),
                str(item.get("title") or ""), str(item.get("when_text") or ""),
                str(item.get("when_iso") or ""), str(item.get("place") or ""),
                str(item.get("source") or ""), str(item.get("reason") or ""),
                str(item.get("state") or "pending"), float(item.get("created_at") or time.time()),
            ),
        )

    def get_proposal(self, proposal_id: str) -> dict | None:
        rows = self._query("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return dict(rows[0]) if rows else None

    def proposals(self, *, states: tuple[str, ...] = ("pending",)) -> list[dict]:
        marks = ", ".join("?" for _ in states)
        rows = self._query(
            f"SELECT * FROM proposals WHERE state IN ({marks}) ORDER BY created_at",
            states,
        )
        return [dict(r) for r in rows]

    def set_proposal(self, proposal_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE proposals SET {sets} WHERE id = ?",
                   [*fields.values(), proposal_id])

    # ------------------------------------------------------------------ 行转换
    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["to"] = json.loads(out.pop("to_json", "") or "[]")
        out["cc"] = json.loads(out.pop("cc_json", "") or "[]")
        out["is_read"] = bool(out.get("is_read"))
        out["has_attachments"] = bool(out.get("has_attachments"))
        out["flagged"] = bool(out.get("flagged"))
        out["needs_reply"] = bool(out.get("needs_reply"))
        return out
