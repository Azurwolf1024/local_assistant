"""控制台的实时通道（SSE）：日志跟随、服务状态变化。

## 为什么是 SSE 而不是 WebSocket

要的只是「服务 → 浏览器」单向推送（日志行、状态变化），SSE 就是浏览器原生的
``EventSource``，**不用写心跳、不用管重连**（断了浏览器自己会重连）。
WebSocket 在这里只会多一层协议与重连逻辑。

## 线程模型（这里是唯一需要小心的地方）

控制台里有两种线程：uvicorn 的事件循环线程，和后台跟随日志的普通线程。
``publish`` 允许从任何线程调：它把事件塞进 ``call_soon_threadsafe``，
投递到事件循环里，再分发给每个订阅者自己的队列。**订阅者慢不会拖住别人**
（各自一个队列，满了就丢最旧的——日志丢几行比把界面卡死好）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

QUEUE_SIZE = 200          # 每个订阅者最多积压多少条（超了丢最旧的）
KEEP_RECENT = 80          # 新订阅者补发最近多少条（刷新页面时日志不会一片空白）


class EventBus:
    """极简发布订阅：线程安全的 ``publish`` + 异步生成器 ``subscribe``。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger("voice_loop.console")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._recent: deque[dict] = deque(maxlen=KEEP_RECENT)
        self._dead = threading.Event()

    # ------------------------------------------------------------------ 生命周期
    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """在事件循环启动时绑定（uvicorn 的 startup 钩子）。"""
        self._loop = loop

    def close(self) -> None:
        self._dead.set()
        with self._lock:
            self._subs.clear()

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    # ------------------------------------------------------------------ 发布
    def publish(self, kind: str, data: Any = None, *, echo: bool = True) -> None:
        """发一条事件。**可以在任何线程调**（日志线程会调）。"""
        event = {"kind": kind, "at": time.time(), "data": data}
        if echo:
            self._recent.append(event)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:              # 循环正在关闭
            pass

    def _fanout(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            if q.full():
                try:
                    q.get_nowait()          # 丢最旧的一条，保持界面活着
                except asyncio.QueueEmpty:  # pragma: no cover - 竞态保护
                    pass
            q.put_nowait(event)

    # ------------------------------------------------------------------ 订阅
    async def subscribe(self, kinds: tuple[str, ...] | None = None) -> AsyncIterator[str]:
        """异步生成器：产出 SSE 格式的字符串（``data: {...}\\n\\n``）。

        ``kinds`` 为 None 表示全收。开头先补发最近的事件，这样刚打开页面就能看到
        最近几十行日志；同时立刻吐一个注释行，让浏览器知道连上了（不然要等到
        第一条事件才显示已连接）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        with self._lock:
            self._subs.append(q)
        try:
            yield ": connected\n\n"
            for event in list(self._recent):
                if kinds is None or event["kind"] in kinds:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            while True:
                event = await q.get()
                if kinds is not None and event["kind"] not in kinds:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            with self._lock:
                if q in self._subs:
                    self._subs.remove(q)


def sse_response_headers() -> dict[str, str]:
    """SSE 需要的响应头（关掉一切缓存与缓冲，不然要等一大坨才吐）。"""
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",       # 万一以后放到反向代理后面
    }
