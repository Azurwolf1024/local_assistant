"""增量同步：**第一次拉窗口，之后只问「有什么变化」**。

两条路，别混
    1. 首次（本地还没有游标）：用 ``$filter=receivedDateTime ge 今天-N天`` 拉最近一批，
       拉够 ``max_pages`` 页或者没下一页为止。★不要用 delta 做首次★——它会把整个
       文件夹的历史变更都吐出来（几千封要翻几十页）。
    2. 之后：用上次存下的 ``@odata.deltaLink`` 继续问。Graph 只会返回新增/修改/删除，
       一封没变就一个请求。
    3. 遇到 ``410 Gone`` / ``400 resyncRequired``（游标过期、或查询形状变了）：
       **丢掉游标，重走第 1 步** —— 这是微软文档明确要求的处理方式，不是异常。

★★关于「形状」的两条硬规则（2026-11-21 真机事故）★★
    (1) delta 令牌把**查询形状**（``$select``）烘在令牌里。用 ``$select=id`` 铸出来的游标，
        之后每一页都只带 ``id`` —— 于是新邮件被写成"只有 id 的空行"，搜索里永远看不见，
        Agent 却能理直气壮地说"没有新邮件"，日志还一路显示"同步完成：新增 2"。
        所以铸游标必须用完整 ``LIST_SELECT``（见 ``graph.delta_cursor_from_now``）。
    (2) 即使形状是对的，Graph 对**改过**的邮件也只回**变化字段**（实测把一封标为未读后，
        增量里只有 ``id`` / ``isRead`` / ``parentFolderId``）。这种"稀疏更新"不是 bug，
        但它同样没有 ``receivedDateTime`` —— 所以一律**逐条补**（``message_row``），
        ★绝不能用空值去覆盖索引里已有的行★（那会把主题/发件人抹掉）。
        补不到的（网络/权限）**不推进游标**，下一轮重试 —— 否则那几条就永远丢了。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .graph import DeltaGone, Graph, GraphError
from .store import MailStore

CURSOR_KEY = "delta_cursor"

#: 一页里最多逐条补几封（每封一个 GET；页面本身 ≤ page_size，这里只是兜个上限）
BACKFILL_PER_ROUND = 60


class ShapeBroken(RuntimeError):
    """增量里只有 id（游标形状不对，而且连补字段都做不到）：必须丢游标重建。"""


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
        """跑一轮同步。返回 ``{ok, added, updated, deleted, skipped, full, pages, error}``。"""
        cursor = "" if force_full else self.cursor
        try:
            if cursor:
                result = self._run_delta(cursor)
            else:
                result = self._run_first()
        except ShapeBroken as exc:
            self.log.warning(f"[mail] 增量形状不对（{exc}）：丢游标、按时间窗口重建")
            self.store.meta_set(CURSOR_KEY, "")
            result = self._run_first()
            result["full"] = True
            result["repaired"] = True
        except DeltaGone:
            self.log.warning("[mail] delta 游标过期（410），改走全量重来")
            self.store.meta_set(CURSOR_KEY, "")
            try:
                result = self._run_first()
                result["full"] = True
                result["repaired"] = True
            except (GraphError, OSError) as exc:
                return {"ok": False, "added": 0, "updated": 0, "deleted": 0, "skipped": 0,
                        "full": True, "pages": 0, "error": str(exc)}
        except (GraphError, OSError) as exc:
            self.log.warning(f"[mail] 同步失败：{exc}")
            return {"ok": False, "added": 0, "updated": 0, "deleted": 0, "skipped": 0,
                    "full": not cursor, "pages": 0, "error": str(exc)}
        self.log.info(
            f"[mail] 同步完成：新增 {result['added']}、更新 {result['updated']}"
            f"、删除 {result.get('deleted', 0)}"
            f"（{'全量' if result['full'] else '增量'} {result['pages']} 页）"
            + (f"，补字段 {result['backfilled']}、跳过 {result['skipped']}"
               if result.get("backfilled") or result.get("skipped") else "")
        )
        if result.get("skipped"):
            self.log.warning(
                f"[mail] 有 {result['skipped']} 封增量条目字段不全、补也没补到："
                "已跳过（不写空行），游标停在原地，下一轮重试")
        return result

    # ------------------------------------------------------------------ 内部
    def _split(self, items: list[dict]) -> tuple[list[dict], list[str], list[str]]:
        """一页增量 → (可写库的, 要删的 id, 字段不全的 id)。"""
        good: list[dict] = []
        removed: list[str] = []
        partial: list[str] = []
        for item in items or []:
            mid = str(item.get("id") or "")
            if not mid:
                continue
            if item.get("removed"):
                removed.append(mid)
                continue
            if str(item.get("received_at") or ""):
                good.append(item)
            else:
                # 正常增量里一定有 receivedDateTime；没有就是"只有 id"的坏载荷
                partial.append(mid)
        return good, removed, partial

    def _apply(self, items: list[dict]) -> dict[str, int]:
        """把一页写进索引：删的删、好的存、缺字段的逐条补（补不到就跳过）。"""
        good, removed, partial = self._split(items)
        out = {"added": 0, "updated": 0, "deleted": 0, "backfilled": 0, "skipped": 0}
        if removed:
            out["deleted"] = self.store.delete_messages(removed)
        if good:
            added, updated = self.store.upsert_messages(good)
            out["added"] += added
            out["updated"] += updated
        if not partial:
            return out
        fetch = getattr(self.graph, "message_row", None)
        if not callable(fetch):
            # 连"按 id 补一封"都做不到：整页缺字段时只能丢掉游标重建，别硬写空行
            if not good and not removed:
                raise ShapeBroken(f"{len(partial)} 条增量只有 id，且这个 Graph 不能补字段")
            out["skipped"] = len(partial)
            return out
        for mid in partial[:BACKFILL_PER_ROUND]:
            row: dict = {}
            try:
                row = fetch(mid) or {}
            except (GraphError, OSError) as exc:
                self.log.warning(f"[mail] 补字段失败（{mid[:20]}…）：{exc}")
                row = {}
            if str(row.get("received_at") or ""):
                added, updated = self.store.upsert_messages([row])
                out["added"] += added
                out["updated"] += updated
                out["backfilled"] += 1
        out["skipped"] = len(partial) - out["backfilled"]
        return out

    @staticmethod
    def _merge(total: dict[str, int], part: dict[str, int]) -> None:
        for key, value in part.items():
            total[key] = total.get(key, 0) + int(value or 0)

    def _run_first(self) -> dict[str, Any]:
        # 顺手清掉历史遗留的"空行"（只有 id、没有时间/发件人：搜不到也用不上）
        dropped = self.store.drop_broken()
        if dropped:
            self.log.info(f"[mail] 清掉 {dropped} 行历史遗留的空行（以前游标形状不对留下的）")
        since = _iso_days_ago(self.first_days)
        items, next_link = self.graph.list_recent(
            folder=self.folder, since_iso=since, page_size=self.page_size)
        pages = 1
        total = {"added": 0, "updated": 0, "deleted": 0, "backfilled": 0, "skipped": 0}
        self._merge(total, self._apply(items))
        while next_link and pages < self.max_pages:
            more, next_link = self.graph.next_page(next_link)
            self._merge(total, self._apply(more))
            pages += 1
        # 接力：从现在开始记变化（拉窗口期间可能又有新邮件，下一轮增量会补上）
        cursor = self.graph.delta_cursor_from_now(folder=self.folder)
        if cursor:
            self.store.meta_set(CURSOR_KEY, cursor)
        return {"ok": True, "full": True, "pages": pages, "error": "",
                "dropped": dropped, **total}

    def _run_delta(self, cursor: str) -> dict[str, Any]:
        items, next_link, delta_link = self.graph.delta(
            folder=self.folder, cursor=cursor, page_size=self.page_size)
        pages = 1
        total = {"added": 0, "updated": 0, "deleted": 0, "backfilled": 0, "skipped": 0}
        self._merge(total, self._apply(items))
        while next_link and pages < self.max_pages:
            raw, next_link, delta_link = self.graph.delta(
                folder=self.folder, cursor=next_link, page_size=self.page_size)
            self._merge(total, self._apply(raw))
            pages += 1
        # ★有没吃掉的条目就不推进游标★：推进了它们就永远丢了（Graph 不会重发）。
        # 下一轮从同一个旧游标重新问，代价是白拿一遍页面，好过丢信。
        if delta_link and not total["skipped"]:
            self.store.meta_set(CURSOR_KEY, delta_link)
        elif total["skipped"]:
            self.log.warning(
                f"[mail] {total['skipped']} 条增量没补全：游标本轮不推进，下一轮重试")
        return {"ok": True, "full": False, "pages": pages, "error": "",
                "cursor_held": bool(total["skipped"]), **total}

