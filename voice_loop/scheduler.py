"""后台提醒调度器：到点播报闹钟与会议/课程提醒。

只做两件事：
    1. 每 ``check_interval`` 秒检查一次 data/alarms.json 里到点的闹钟
    2. 检查 data/schedule.json 里即将开始的会议/课程，按 ``remind_before`` 提前播报

播报动作通过回调交给 VoiceLoop，这样调度器不需要知道 TTS 的实现。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from .settings import Settings
from .skills import Skills


class ReminderScheduler:
    def __init__(
        self,
        skills: Skills,
        settings: Settings,
        on_announce: Callable[[str], None],
        logger: logging.Logger | None = None,
    ) -> None:
        self.skills = skills
        self.settings = settings
        self.on_announce = on_announce
        self.log = logger or logging.getLogger("voice_loop")
        self.interval = max(0.5, float(settings.skills.check_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired_count = 0

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reminder")
        self._thread.start()
        self.log.info(
            f"提醒调度器已启动（每 {self.interval:.0f}s 检查一次，"
            f"{self.skills.stats()}）"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"调度器出错（已忽略）：{exc}")

    def tick(self, now: datetime | None = None) -> int:
        """检查一次；返回本次新播报的条数（也便于单元测试手动调用）。"""
        now = now or datetime.now()
        fired = 0

        for item in self.skills.due_alarms(now):
            what = item.get("what") or "时间到了"
            self._announce(f"时间到了，{what}。")
            fired += 1

        for item, text in self.skills.due_schedule(now):
            self._announce(text)
            fired += 1

        return fired

    def _announce(self, text: str) -> None:
        self.fired_count += 1
        self.log.info(f"[提醒] {text}")
        try:
            self.on_announce(text)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"播报失败：{exc}")

    # ------------------------------------------------------------------ 工具
    def next_reminder(self) -> tuple[str, float] | None:
        """返回最近一条待提醒 (描述, 剩余秒数)，用于状态展示。"""
        now = datetime.now()
        best: tuple[str, float] | None = None
        for item in self.skills.alarms.load():
            if item.get("fired"):
                continue
            try:
                when = datetime.fromisoformat(item["when"])
            except Exception:  # noqa: BLE001
                continue
            delta = (when - now).total_seconds()
            if delta < 0:
                continue
            if best is None or delta < best[1]:
                best = (f"{item.get('what', '提醒')}（{when.strftime('%m-%d %H:%M')}）", delta)
        return best
