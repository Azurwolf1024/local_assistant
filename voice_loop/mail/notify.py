"""通知策略：**重要邮件立刻说，一般邮件攒成摘要，噪音不打扰**。

三档（阈值来自 ``[mail] high_score / medium_score``）
    ``high``   → 立刻弹窗（可带一句语音）；同一封只提醒一次
    ``medium`` → 攒着，等 ``[mail] digest_hours`` 的点一次报（早/晚各一次）
    ``low``    → 静默入库，只可检索（不打扰，但问起来答得出）

「同一封只提醒一次」靠索引里的 ``notified_at``，不是内存标记 —— 服务重启不会重复响。
弹窗 / 朗读都是**注入**的（``toast`` / ``speak``），所以这个模块能离线自测。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable


class Notifier:
    def __init__(
        self,
        cfg: Any,
        store: Any,
        *,
        toast: Any = None,
        speak: Callable[[str], None] | None = None,
        log: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.toast = toast
        self.speak = speak
        self.log = log or logging.getLogger("voice_loop.mail")
        self.clock = clock or time.time
        self._last_digest: dict[str, float] = {}

    # ---------------------------------------------------------------- 配置面
    @property
    def high_enabled(self) -> bool:
        return bool(getattr(self.cfg, "notify_high", True))

    @property
    def digest_hours(self) -> list[int]:
        raw = getattr(self.cfg, "digest_hours", [9, 20]) or []
        out = []
        for item in raw:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                out.append(hour)
        return out or [9, 20]

    # ---------------------------------------------------------------- 立刻提醒
    def notify_high(self, rows: list[dict]) -> int:
        """把「重要且还没提醒过」的邮件弹出来。返回提醒了几封。"""
        if not self.high_enabled or not rows:
            return 0
        sent = 0
        for row in rows:
            message_id = str(row.get("id") or "")
            if not message_id:
                continue
            title = "重要邮件"
            who = row.get("sender_name") or row.get("sender") or "未知发件人"
            one_line = row.get("one_line") or row.get("subject") or "（无主题）"
            extra = "，需要你回复" if row.get("needs_reply") else ""
            text = f"{who}：{one_line}{extra}"
            if self.toast is not None:
                try:
                    self.toast.show(title, text)
                except Exception as exc:  # noqa: BLE001 - 弹窗失败不影响入库
                    self.log.warning(f"[mail] 弹窗失败（忽略）：{exc}")
            if self.speak is not None:
                try:
                    self.speak(f"{who}发来一封邮件：{one_line}")
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(f"[mail] 朗读失败（忽略）：{exc}")
            self.store.mark_notified(message_id)
            sent += 1
            self.log.info(f"[mail] 已提醒：{who} · {one_line[:40]}")
        return sent

    # ---------------------------------------------------------------- 摘要
    def digest(self, *, since_hours: float = 12, force: bool = False) -> str:
        """把这段时间的「中/高」邮件攒成一段话（早报/晚报）。

        ``force=True`` 时顺便念出来；``force=False`` 只返回文本（给查询类调用）。
        """
        rows = self.store.search("", limit=30, since_iso=_iso_ago(since_hours))
        picked = [r for r in rows if str(r.get("level") or "") in ("high", "medium")]
        if not picked:
            return ""
        parts = [f"有 {len(picked)} 封值得看的邮件："]
        for row in picked[:8]:
            who = row.get("sender_name") or row.get("sender") or "未知"
            what = row.get("one_line") or row.get("subject") or "（无主题）"
            parts.append(f"{who}：{what}；")
        text = "".join(parts).rstrip("；") + "。"
        if force and self.speak is not None:
            try:
                self.speak(text)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"[mail] 摘要朗读失败（忽略）：{exc}")
        return text

    def due_digest(self, *, now: datetime | None = None) -> bool:
        """现在是不是摘要时间点（每档每天最多一次）。"""
        moment = now or datetime.now()
        if moment.hour not in self.digest_hours:
            return False
        key = f"{moment.date()}-{moment.hour}"
        if key in self._last_digest:
            return False
        self._last_digest[key] = self.clock()
        # 只留最近 8 个标记，别一直长
        for stale in list(self._last_digest)[:-8]:
            self._last_digest.pop(stale, None)
        return True


def _iso_ago(hours: float) -> str:
    from datetime import timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(hours)))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
