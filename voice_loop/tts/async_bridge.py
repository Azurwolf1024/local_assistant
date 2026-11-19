"""在一个同步线程里跑 async 协程（edge-tts 这类只有异步接口的云端库用）。

★为什么要专门写★：`synth()` 是同步生成器，而调用它的线程**有时**已经在跑事件循环
（控制台/测试里被 asyncio 包着）。这时直接 ``asyncio.run`` 会抛
「asyncio.run() cannot be called from a running event loop」，
所以：当前线程有循环 → 另起线程跑；没有 → 就地 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[Any, Any, T], *, timeout: float = 60.0) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_timeout(coro, timeout))
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(_with_timeout(coro, timeout))
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="async-bridge", daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise TimeoutError(f"异步调用超过 {timeout:g} 秒没有返回")
    return box["value"]


async def _with_timeout(coro: Coroutine[Any, Any, T], timeout: float) -> T:
    return await asyncio.wait_for(coro, timeout=timeout)


__all__ = ["run_sync"]
