"""服务里的巡检线程：同步 → 分级 → 提醒 → 到点发摘要。

照 ``ReminderScheduler`` 的样子写：一个守护线程 + ``Event`` 等待，退出时干净收尾。

为什么放在服务进程里
    - 「收到重要邮件就提醒」必须是**后台**行为，不能等用户开口问；
    - 服务本来就在跑（麦克风 + 提醒调度器），多一个每 3 分钟的 HTTP 轮询成本很低；
    - 失败不抛给语音链路：这一轮不行就下一轮，日志留一句原因。
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime
from typing import Any, Callable


class MailWatcher:
    def __init__(
        self,
        service: Any,
        notifier: Any,
        *,
        interval: float = 180.0,
        log: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.notifier = notifier
        self.interval = max(20.0, float(interval))
        self.log = log or logging.getLogger("voice_loop.mail")
        self.clock = clock or (lambda: datetime.now())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {}
        self.cycles = 0
        self._warned = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mail-watcher", daemon=True)
        self._thread.start()
        self.log.info(f"[mail] 巡检已启动（每 {self.interval:.0f}s）")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- 一轮
    def tick(self) -> dict[str, Any]:
        """跑一轮（自测直接调它，不用开线程）。"""
        self.cycles += 1
        result: dict[str, Any] = {"cycle": self.cycles}
        # 没登录 / 没配 client_id 就别每 3 分钟刷一行警告（第一次说一句就够）
        ready = getattr(self.service, "ready", None)
        if callable(ready):
            ok, why = ready()
            if not ok:
                if not self._warned:
                    self.log.info(f"[mail] 巡检跳过：{why}")
                    self._warned = True
                result.update({"ok": False, "skipped": why})
                self.last = result
                return result
            self._warned = False
        try:
            result["sync"] = self.service.sync()
            fresh = self.service.store.untriaged(limit=5)
            for msg in fresh:
                info = self.service.triage.classify(msg)
                self.service.store.set_triage(str(msg["id"]), info)
            pending = self.service.store.unnotified(levels=("high",), limit=5)
            result["notified"] = self.notifier.notify_high(pending)
            moment = self.clock()
            if self.notifier.due_digest(now=moment if isinstance(moment, datetime) else None):
                result["digest"] = self.notifier.digest(since_hours=12, force=True)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 巡检失败不能影响语音
            self.log.warning(f"[mail] 巡检出错（下一轮再试）：{exc}")
            self.log.debug(traceback.format_exc())
            result.update({"ok": False, "error": str(exc)})
        self.last = result
        return result

    def _loop(self) -> None:
        # 起服务后先等一下，别和质量仪的预热抢资源
        if self._stop.wait(5.0):
            return
        while not self._stop.is_set():
            self.tick()
            if self._stop.wait(self.interval):
                break
