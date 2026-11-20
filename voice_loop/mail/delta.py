"""增量同步：**第一次拉窗口，之后只问「有什么变化」**。

两条路，别混
    1. 首次（本地还没有游标）：用 ``$filter=receivedDateTime ge 今天-N天`` 拉最近一批，
       拉够 ``max_pages`` 页或者没下一页为止。★不要用 delta 做首次★——它会把整个
       文件夹的历史变更都吐出来（几千封要翻几十页）。
    2. 之后：用上次存下的 ``@odata.deltaLink`` 继续问。Graph 只会返回新增/修改/删除，
       一封没变就一个请求。
    3. 遇到 ``410 Gone``（游标过期/租户维护）：**丢掉游标，重走第 1 步**——
       这是微软文档明确要求的处理方式，不是异常。

失败策略：同步失败**不抛给语音链路**，只记一行日志并把错误回给调用方（巡检线程下一轮再试）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .graph import DeltaGone, Graph, GraphError
from .store import MailStore

CURSOR_KEY = "delta_cursor"


def _iso_days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Syncer:
    """把 Graph 的邮件**只增不减地**搬进本地索引。"""

    def __init__(
        self,
        graph: Graph,
        store: MailStore,
        *,
        folder: str = "inbox",
        first_days: int = 30,
        page_size: int = 50,
        max_pages: int = 10,
        log: logging.Logger | None = None,
    ) -> None:
        self.graph = graph
        self.store = store
        self.folder = (folder or "inbox").strip()
        self.first_days = max(1, int(first_days))
        self.page_size = max(1, min(200, int(page_size)))
        self.max_pages = max(1, int(max_pages))
        self.log = log or logging.getLogger("voice_loop.mail")

    # ------------------------------------------------------------------ 对外
    @property
    def cursor(self) -> str:
        return self.store.meta_get(CURSOR_KEY)

    def sync(self, *, force_full: bool = False) -> dict[str, Any]:
        """跑一轮同步。返回 ``{ok, added, updated, full, pages, error}``。"""
        cursor = "" if force_full else self.cursor
        try:
            if cursor:
                result = self._run_delta(cursor)
            else:
                result = self._run_first()
        except DeltaGone:
            self.log.warning("[mail] delta 游标过期（410），改走全量重来")
            self.store.meta_set(CURSOR_KEY, "")
            try:
                result = self._run_first()
                result["full"] = True
            except (GraphError, OSError) as exc:
                return {"ok": False, "added": 0, "updated": 0, "full": True, "pages": 0,
                        "error": str(exc)}
        except (GraphError, OSError) as exc:
            self.log.warning(f"[mail] 同步失败：{exc}")
            return {"ok": False, "added": 0, "updated": 0, "full": not cursor, "pages": 0,
                    "error": str(exc)}
        self.log.info(
            f"[mail] 同步完成：新增 {result['added']}、更新 {result['updated']}"
            f"（{'全量' if result['full'] else '增量'} {result['pages']} 页）"
        )
        return result

    # ------------------------------------------------------------------ 内部
    def _run_first(self) -> dict[str, Any]:
        since = _iso_days_ago(self.first_days)
        items, next_link = self.graph.list_recent(
            folder=self.folder, since_iso=since, page_size=self.page_size)
        pages = 1
        added, updated = self.store.upsert_messages(items)
        while next_link and pages < self.max_pages:
            more, next_link = self.graph.next_page(next_link)
            a, u = self.store.upsert_messages(more)
            added += a
            updated += u
            pages += 1
        # 接力：从现在开始记变化（拉窗口期间可能又有新邮件，下一轮增量会补上）
        cursor = self.graph.delta_cursor_from_now(folder=self.folder)
        if cursor:
            self.store.meta_set(CURSOR_KEY, cursor)
        return {"ok": True, "added": added, "updated": updated, "full": True,
                "pages": pages, "error": ""}

    def _run_delta(self, cursor: str) -> dict[str, Any]:
        items, next_link, delta_link = self.graph.delta(
            folder=self.folder, cursor=cursor, page_size=self.page_size)
        pages = 1
        added, updated = self.store.upsert_messages(items)
        while next_link and pages < self.max_pages:
            raw, next_link, delta_link = self.graph.delta(
                folder=self.folder, cursor=next_link, page_size=self.page_size)
            a, u = self.store.upsert_messages(raw)
            added += a
            updated += u
            pages += 1
        if delta_link:
            self.store.meta_set(CURSOR_KEY, delta_link)
        return {"ok": True, "added": added, "updated": updated, "full": False,
                "pages": pages, "error": ""}
