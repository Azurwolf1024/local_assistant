"""**应用的**托盘图标：控制台是一个应用，这颗图标是它的托盘（VS Code / Discord 那种）。

## 归属（2026-11-21 重新定的）

以前这颗图标被当成「服务的托盘」：`listen` 起服务时顺手拉起它，菜单里管着
「服务」和「控制台」两个独立进程。用户指出这不是常见形态 —— 常见形态是：

> **控制台是应用，托盘是应用的托盘**；语音服务是应用在后台带着的子系统。

现在是这个结构：

```
main.py ui（应用进程）
 ├─ uvicorn 线程         ← 主窗口的网页服务（127.0.0.1:8765）
 ├─ 窗口（pywebview / Edge / 浏览器）  ← 应用的主窗口
 ├─ ★托盘图标（就是本模块）★          ← 应用的托盘：关窗口不退出，图标还在
 └─ 语音服务（另一个进程，应用启动时带起来、退出时停掉）
```

于是三条手感都变了：

| 动作 | 以前 | 现在 |
| --- | --- | --- |
| 关窗口 | 退出控制台 | **藏到托盘**（pywebview 拦下 closing；Edge/浏览器那个窗口没了但应用还在） |
| 双击图标 | 打开控制台网页 | **把主窗口叫回来**（有窗口就显示，没有就重开一个） |
| 退出 | 收图标 | **停服务 → 关窗口 → 退应用** |

服务依然可以被单独启动（`main.py listen`，没有界面、没有托盘）—— 那条路是给
「只想要语音、不要界面」的场景留的。

## 菜单

```
服务运行中（PID 1234）· 控制台 8765
─────────────────────
显示控制台窗口          ← 双击图标走这条
启动服务 / 停止服务     ← 应用带着的那个后台子系统
看服务日志
─────────────────────
退出应用（停服务 + 关窗口）
```

## 为什么用 pystray 而不是裸 ctypes

Pillow 本来就是硬依赖（截屏要用），pystray 是纯 Python 的薄封装。裸写
``Shell_NotifyIcon`` 要自己管隐藏窗口、消息泵、图标句柄回收，两百行起步，
在「只是想要个右键菜单」这件事上不划算。没装 pystray 也不影响应用本身：
:func:`start_app_tray` 只记一行日志就返回，网页照常能用。

## 线程模型

图标跑在**守护线程**里（Windows 上 pystray 自己建隐藏窗口 + 消息泵，不占主线程）。
主线程在 pywebview 上是窗口事件循环（见 ``console/app.py``），否则等一个 ``Event``。
所有菜单动作都在托盘线程里跑，**不得阻塞**（停止服务会等几秒，所以那是 service_ctl
的活，托盘只调它并把结果写进气泡）。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER_NAME = "voice_loop.tray"

#: 状态轮询间隔（秒）：只更新 tooltip / 图标状态点，不触发任何动作
POLL_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# 能力探测 / 图标
# --------------------------------------------------------------------------- #
def available() -> tuple[bool, str]:
    """能不能开托盘。返回 ``(可以, 原因)``；不能时原因是一句给人看的话。"""
    try:
        import pystray  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001 - ImportError 之外也可能炸（缺 DLL 之类）
        return False, f"没装 pystray（pip install pystray）：{exc}"
    try:
        from PIL import Image  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"没装 Pillow（pip install pillow）：{exc}"
    if os.name != "nt" and os.environ.get("DISPLAY") is None:
        # Linux 无 X 会静默失败，不如先说清楚
        return False, "当前没有图形会话（没有 DISPLAY），托盘图标无处可放"
    return True, ""


def icon_image(size: int = 64, *, running: bool = True) -> Any:
    """现画图标，不依赖图片文件（源码运行和冻结成 exe 都能用）。

    一个蓝底 + 三道声波柱；右下角一颗状态点：绿 = 语音服务在跑，灰 = 已停止。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((1, 1, size - 2, size - 2), fill=(38, 132, 255, 255))
    for offset, height in ((0.24, 0.34), (0.43, 0.56), (0.62, 0.34)):
        left = int(size * offset)
        right = left + max(3, size // 9)
        top = int(size * (0.48 - height / 2))
        bottom = int(size * (0.48 + height / 2))
        draw.rounded_rectangle(
            (left, top, right, bottom), radius=max(1, size // 22), fill=(255, 255, 255, 240)
        )
    dot = max(6, size // 4)
    x0, y0 = size - dot - 1, size - dot - 1
    draw.ellipse((x0, y0, size - 1, size - 1), fill=(0, 0, 0, 60))
    draw.ellipse(
        (x0 + 1, y0 + 1, size - 3, size - 3),
        fill=(46, 204, 113, 255) if running else (150, 150, 150, 255),
    )
    return img


# --------------------------------------------------------------------------- #
# 菜单项（纯数据：自测不用真开窗口）
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    """一个菜单项（或一条分隔线）。``enabled`` 在 :meth:`AppTray.entries` 里算好。"""

    id: str
    label: str = ""
    enabled: bool = True
    kind: str = "item"          # item | sep
    default: bool = False       # 双击图标触发的那一项

    @property
    def is_sep(self) -> bool:
        return self.kind == "sep"


def _sep() -> Entry:
    return Entry(id="", kind="sep")


@dataclass
class Snapshot:
    """一次状态快照（测试里也用它喂假数据）。"""

    running: bool
    pid: int | None = None
    port: int = 0
    note: str = ""
    window: str = ""        # 主窗口现在是「显示着 / 收起来了 / 没有窗口」

    @property
    def status_text(self) -> str:
        svc = f"服务运行中（PID {self.pid}）" if self.running and self.pid else (
            "服务运行中" if self.running else "服务已停止")
        bits = [svc]
        if self.port:
            bits.append(f"控制台 {self.port}")
        if self.window:
            bits.append(self.window)
        return " · ".join(bits)


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
class AppTray:
    """应用的托盘。所有动作都可注入（自测直接调 :meth:`act`，不用真点图标）。"""

    def __init__(
        self,
        settings: Any,
        *,
        log: logging.Logger | None = None,
        status: Callable[[], Any] | None = None,
        show_window: Callable[[], tuple[bool, str]] | None = None,
        start_service: Callable[[], tuple[bool, str]] | None = None,
        stop_service: Callable[[], tuple[bool, str]] | None = None,
        quit_app: Callable[[], None] | None = None,
        open_file: Callable[[Path], Any] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.log = log or logging.getLogger(LOGGER_NAME)
        self._status_fn = status or self._default_status
        self._show_window = show_window or (lambda: (False, "没有窗口可以显示"))
        self._start_fn = start_service or self._default_start
        self._stop_fn = stop_service or self._default_stop
        self._quit_fn = quit_app
        self._open_file = open_file or _open_file
        self._poll = max(1.0, float(poll_seconds))
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._stop_poll = threading.Event()
        self._busy = threading.Lock()
        self.last_message = ""

    # ------------------------------------------------------------- 状态
    def _default_status(self) -> Snapshot:
        from . import service_ctl  # noqa: PLC0415 - 只有真要读状态才 import

        st = service_ctl.status(self.settings)
        return Snapshot(running=bool(st.running), pid=st.pid,
                        port=service_ctl.console_port(self.settings))

    def snapshot(self) -> Snapshot:
        try:
            got = self._status_fn()
        except Exception as exc:  # noqa: BLE001 - 状态读不出来不该让菜单炸掉
            self.log.warning(f"[托盘] 读状态失败：{exc}")
            return Snapshot(running=False, note=f"读不到状态：{exc}")
        if isinstance(got, Snapshot):
            return got
        return Snapshot(running=bool(getattr(got, "running", False)),
                        pid=getattr(got, "pid", None),
                        port=int(getattr(got, "port", 0) or 0),
                        window=str(getattr(got, "window", "") or ""))

    def entries(self) -> list[Entry]:
        """右键菜单里有什么（顺序就是显示顺序）。"""
        snap = self.snapshot()
        return [
            Entry(id="status", label=snap.status_text, enabled=False),
            _sep(),
            Entry(id="show", label="显示控制台窗口", default=True),
            Entry(id="start", label="启动服务", enabled=not snap.running),
            Entry(id="stop", label="停止服务", enabled=snap.running),
            Entry(id="log", label="看服务日志"),
            _sep(),
            Entry(id="quit", label="退出应用（停服务 + 关窗口）"),
        ]

    # ------------------------------------------------------------- 动作
    def _default_start(self) -> tuple[bool, str]:
        from . import service_ctl  # noqa: PLC0415

        return service_ctl.start_service(self.settings)

    def _default_stop(self) -> tuple[bool, str]:
        from . import service_ctl  # noqa: PLC0415

        grace = float(getattr(getattr(self.settings, "app", None),
                              "tray_stop_grace", 20.0) or 20.0)
        return service_ctl.stop_service(self.settings, timeout=grace)

    def act(self, entry_id: str) -> tuple[bool, str]:
        """执行一个菜单动作（★自测直接调它，不用真点图标★）。"""
        if entry_id not in {"show", "start", "stop", "log", "quit"}:
            return False, "这个菜单项不能点"
        if not self._busy.acquire(blocking=False):
            return False, "上一个动作还没做完"
        try:
            if entry_id == "show":
                ok, msg = self._show_window()
                self._announce(msg, ok)
                return ok, msg
            if entry_id == "log":
                self._open_file(self.settings.resolve(self.settings.wake.log_file))
                return True, "已打开服务日志"
            if entry_id == "start":
                ok, msg = self._start_fn()
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            if entry_id == "stop":
                # ★只停服务，应用和托盘都留着★
                ok, msg = self._stop_fn()
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            return self._quit()
        except Exception as exc:  # noqa: BLE001 - 菜单动作失败不能带崩应用
            self.log.warning(f"[托盘] 动作 {entry_id} 失败：{exc}")
            return False, f"动作失败：{exc}"
        finally:
            self._busy.release()

    def _quit(self) -> tuple[bool, str]:
        """退出应用：**停服务 → 关窗口 → 收图标**（顺序很重要，见 console/app.py）。"""
        if self._quit_fn is None:
            return False, "没有接退出动作（托盘是单独起的？）"
        self.log.info("[托盘] 用户选择退出应用")
        try:
            self._quit_fn()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[托盘] 退出时出错（继续）：{exc}")
        return True, "正在退出应用…"

    def _announce(self, message: str, ok: bool) -> None:
        """把结果写到托盘气泡 + 日志（气泡失败就算了）。"""
        self.last_message = message
        (self.log.info if ok else self.log.warning)(f"[托盘] {message}")
        icon = self._icon
        if icon is None:
            return
        notify = getattr(icon, "notify", None)
        if callable(notify):
            try:
                notify(message, "本地语音助手")
            except Exception:  # noqa: BLE001 - 气泡是锦上添花
                pass

    # ------------------------------------------------------------- 生命周期
    def _import(self) -> Any:
        """拿 pystray（单独一层，方便自测把「没装」这条路走一遍）。"""
        try:
            import pystray  # noqa: PLC0415

            return pystray
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[托盘] 用不了托盘（{exc}）")
            return None

    def start(self) -> bool:
        """起图标（守护线程）。失败只记日志，绝不影响应用本身。"""
        ok, why = available() if self._icon is None else (True, "")
        if not ok:
            self.log.warning(f"[托盘] {why}")
            self.last_message = why
            return False
        pystray = self._import()
        if pystray is None:
            return False
        try:
            menu = pystray.Menu(*[self._menu_item(pystray, e) for e in self.entries()])
            snap = self.snapshot()
            self._icon = pystray.Icon(
                "local_ai_service",
                icon=icon_image(running=snap.running),
                title=f"本地语音助手 · {snap.status_text}",
                menu=menu,
            )
            self._thread = threading.Thread(
                target=self._icon.run, name="app-tray", daemon=True)
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 - 托盘是附加能力，不是启动前提
            self.log.warning(f"[托盘] 图标没起来：{exc}")
            self.last_message = f"托盘图标没起来：{exc}"
            self._icon = None
            return False
        self._stop_poll.clear()
        self._poller = threading.Thread(target=self._poll_loop, name="app-tray-poll",
                                        daemon=True)
        self._poller.start()
        self.log.info("[托盘] 已就绪：关窗口不收应用，双击图标可叫回窗口")
        return True

    def _menu_item(self, pystray: Any, entry: Entry) -> Any:
        if entry.is_sep:
            return pystray.Menu.SEPARATOR
        if entry.id == "status":
            # 状态行是灰色不可点的，文字在打开菜单时现算（服务可能刚停）
            return pystray.MenuItem(
                lambda _item: self.snapshot().status_text, None, enabled=False)
        return pystray.MenuItem(entry.label, self._action_for(entry.id),
                                default=entry.default, enabled=entry.enabled)

    def _action_for(self, entry_id: str) -> Callable[[Any, Any], None]:
        """★pystray 只认 0/1/2 个参数的 action★（`co_argcount` 卡得很死）。

        所以不能图省事写 ``lambda _icon, _item, _id=entry.id: ...``（那是 3 个参数，
        直接 ValueError，坑过一次），只能老老实实包一层。
        """

        def action(_icon: Any, _item: Any) -> None:
            self.act(entry_id)

        return action

    def refresh(self, window_note: str | None = None) -> None:
        """更新 tooltip 与图标状态点（菜单文字在打开时会自己重算）。"""
        icon = self._icon
        if icon is None:
            return
        snap = self.snapshot()
        if window_note is not None:
            snap.window = window_note
        try:
            icon.title = f"本地语音助手 · {snap.status_text}"
            icon.icon = icon_image(running=snap.running)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[托盘] 刷新图标失败：{exc}")

    def _poll_loop(self) -> None:
        was = self.snapshot().running
        while not self._stop_poll.wait(self._poll):
            snap = self.snapshot()
            if snap.running != was:
                was = snap.running
                self.refresh()

    def stop(self) -> None:
        """收图标（幂等）。"""
        self._stop_poll.set()
        icon, self._icon = self._icon, None
        thread, self._thread = self._thread, None
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"[托盘] 收图标时出错（忽略）：{exc}")
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)


def start_app_tray(settings: Any, log: logging.Logger | None = None, **kwargs: Any) -> AppTray | None:
    """给应用起一颗托盘（起不来就算了，返回 ``None``）。

    ``[app] tray = false`` 或环境没有图形会话时直接返回 ``None``，网页照常能用。
    """
    if not bool(getattr(getattr(settings, "app", None), "tray", True)):
        return None
    log = log or logging.getLogger(LOGGER_NAME)
    tray = AppTray(settings, log=log, **kwargs)
    return tray if tray.start() else None


def _open_file(path: Path) -> None:
    """用系统默认程序打开（Windows 是记事本，方便直接看日志）。"""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        return
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])  # noqa: S603


__all__ = [
    "AppTray", "Entry", "POLL_SECONDS", "Snapshot", "available", "icon_image",
    "start_app_tray",
]
