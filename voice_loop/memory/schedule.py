"""记忆层看日程的适配器：读的是**同一份** ``data/events.json``。

★为什么单独一个模块★：记忆层只认「一个能读日程的东西」（`memory.Schedule` 协议），
它不该知道日程存在哪个文件；而 pipeline 也不该为了这点事多背一个内联类。
放在这里，两边都干净：**记忆隔离，日程共享**。

    白泽和凯尔希看到的是同一份安排（同一份 events.json）；
    各自的 episodes/facts 却各在各的目录里，互不可见。

这里**只读**：写日程仍然走原来的技能层（`skills` / MCP 工具），
不另开一条写路径 —— 同一个事实两个写入点，迟早会互相覆盖（`store.py` 开头那段就是讲这个的）。
"""

from __future__ import annotations

from datetime import datetime, timedelta


class SharedSchedule:
    """日程的只读窗口（供记忆摘要显示「日程（共享）」那一行）。"""

    def __init__(self, settings) -> None:
        self._settings = settings

    def _items(self) -> list[dict]:
        from ..events import EventStore  # noqa: PLC0415

        path = self._settings.resolve(self._settings.skills.event_file)
        try:
            return EventStore(path).load()
        except Exception:  # noqa: BLE001 - 日程读不到不该影响记忆
            return []

    def add_event(self, title: str, when: str, **kwargs) -> int | None:  # noqa: ARG002
        """写这一侧故意不实现：所有写入都走技能层，只留一个入口。"""
        return None

    def upcoming(self, days: int = 7) -> list[dict]:
        """未来 N 天的事件（按开始时间排序）。

        `start` 是 ``YYYY-MM-DD HH:MM`` 这种定长文本，字符串比较就是时间顺序
        （这也正是它选这个格式的原因），不用再引一层时间解析。
        """
        now = datetime.now()
        edge = (now + timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M")
        stamp = now.strftime("%Y-%m-%d %H:%M")
        rows = []
        for item in self._items():
            start = str(item.get("start") or "")
            if stamp <= start <= edge:
                rows.append({"start": start, "title": item.get("title") or ""})
        return sorted(rows, key=lambda r: r["start"])
