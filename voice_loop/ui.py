"""Tk 窗口宿主：一个进程里只允许一个 Tk 解释器，而且只能由创建它的那个线程碰。

为什么需要它：Tcl/Tk 不允许在多个线程里各建一个 ``Tk()``。之前提醒小窗和字幕各自
起线程、各自 ``Tk()``，结果第二个线程建字体时直接抛
``RuntimeError: main thread is not in main loop``，字幕一个字都画不出来。

所以这里做一个单例宿主：
    - 独占一个后台线程 + 一个隐藏的 ``Tk()`` 根窗口
    - 想要窗口的模块（``toast``、``subtitle``）把自己注册成 tick，
      每轮循环在 UI 线程里被调一次，自己管自己的 ``Toplevel``
    - 需要「立刻在 UI 线程里做件事」（比如销毁窗口）就用 :meth:`post`

这样即使同时开字幕和提醒弹窗，也只有一个解释器、一个线程，不会打架。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

Tick = Callable[[object], None]
Task = Callable[[object], None]


class UiHost:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger("voice_loop")
        self._tasks: queue.Queue = queue.Queue()
        self._ticks: list[Tick] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed = False
        self._started = False

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def failed(self) -> bool:
        """tkinter 不可用 / 建不出窗口之类的硬失败。"""
        return self._failed

    @property
    def started(self) -> bool:
        """本进程里是否已经尝试过启动 Tk（不管成功与否）。"""
        return self._started

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self.running:
            return True
        self._started = True
        self._stop.clear()
        self._ready.clear()
        self._failed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="tk-ui")
        self._thread.start()
        # 等根窗口建好，这样调用方立刻就能知道 tkinter 到底能不能用
        self._ready.wait(timeout=8.0)
        return not self._failed

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._ticks.clear()

    # ------------------------------------------------------------------ 线程间接口
    def post(self, fn: Task) -> None:
        """把一个回调丢到 UI 线程去执行（参数是根窗口）。"""
        if self._failed:
            return
        self._tasks.put(fn)

    def add_tick(self, fn: Tick) -> None:
        with self._lock:
            if fn not in self._ticks:
                self._ticks.append(fn)

    def remove_tick(self, fn: Tick) -> None:
        with self._lock:
            if fn in self._ticks:
                self._ticks.remove(fn)

    # ------------------------------------------------------------------ UI 线程
    def _run(self) -> None:  # pragma: no cover - 需要图形环境
        try:
            import tkinter as tk
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self.log.warning(f"没有 tkinter（{exc}），所有屏幕窗口已关闭")
            self._ready.set()
            return

        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self.log.warning(f"无法创建 Tk 根窗口（{exc}），所有屏幕窗口已关闭")
            self._ready.set()
            return

        root.withdraw()
        self._ready.set()

        while not self._stop.is_set():
            # 1) 先做那些「必须马上做」的事（销毁窗口之类）
            try:
                while True:
                    task = self._tasks.get_nowait()
                    try:
                        task(root)
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(f"UI 任务出错（已忽略）：{exc}")
            except queue.Empty:
                pass

            # 2) 再让各个窗口刷自己的界面
            with self._lock:
                ticks = list(self._ticks)
            for tick in ticks:
                try:
                    tick(root)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(f"UI 刷新出错（已忽略）：{exc}")

            try:
                root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.04)

        # 收尾：把还没跑的任务补上，再把所有子窗口都在**本线程**里销毁。
        # 这一步很关键：Tk 对象如果在别的线程被回收，退出时会冒出
        # “Tcl_AsyncDelete: async handler deleted by the wrong thread”。
        try:
            while True:
                task = self._tasks.get_nowait()
                try:
                    task(root)
                except Exception:  # noqa: BLE001
                    pass
        except queue.Empty:
            pass
        try:
            for child in root.winfo_children():
                try:
                    child.destroy()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 全局单例
# --------------------------------------------------------------------------- #
_host: UiHost | None = None
_host_lock = threading.Lock()


def host(logger: logging.Logger | None = None) -> UiHost:
    """取出（必要时创建）整个进程唯一的 UI 宿主。"""
    global _host
    with _host_lock:
        if _host is None:
            _host = UiHost(logger)
        elif logger is not None:
            _host.log = logger
        return _host


def shutdown() -> None:
    """关掉 UI 宿主。整个进程准备退出时调，免得解释器在错误的线程里被回收。"""
    global _host
    with _host_lock:
        h, _host = _host, None
    if h is not None:
        h.stop()
