"""托盘小图标：右下角右键就能管**服务**和**控制台**两个进程。

## 为什么需要它

``python main.py listen -B`` 起的是一个**没有窗口的后台进程**（Windows 上用
``pythonw.exe``，连控制台都没有）。于是「怎么把它关掉」过去只有两个答案：
敲一条 ``python main.py stop``，或者去任务管理器里翻 pythonw 进程。
托盘补上第三个：右下角右键 → **停止服务**。

## ★托盘永远是独立进程★（2026-11-20 改的）

以前有两种模式：``embedded``（图标跑在服务进程里）和 ``standalone``（``main.py tray``）。
``embedded`` 有个致命毛病：**图标在 = 服务在**，所以「停止服务」必然连托盘一起没 ——
想再启动一次就没入口了，只能去命令行。现在只保留独立进程这一种：

| 以前 | 现在 |
| --- | --- |
| 图标在服务进程内，停服务 = 图标消失 | 图标独立常驻，**停服务后图标还在**，可以直接再启动 |
| 服务在跑才看得到图标 | 服务没在跑也能看到图标（状态显示「服务已停止」） |
| 退出要开两个地方（服务 + 托盘） | 菜单「退出」= 停服务 + 关控制台 + 收图标 |

代价是「图标在 ≠ 服务在」，所以图标右下角那颗状态点（绿/灰）和 tooltip 必须诚实反映
真实状态 —— 由 :meth:`ServiceTray._poll_loop` 每 5 秒对一次。

## 菜单

```
服务运行中（PID 1234）· 控制台在跑
─────────────────────
打开控制台            ← 双击图标走这条（没起就先拉起来）
关闭控制台            ← 只在控制台真在跑时可点
看服务日志
─────────────────────
启动服务              ← 起完顺手把控制台也拉起来
停止服务              ← ★只停服务，托盘留着★
─────────────────────
退出（停服务 + 关控制台）
```

## 停止是怎么生效的（★别用 kill★）

``voice_loop/pipeline.py`` 的常驻循环每轮检查 ``sessions/listen.stop``；
所以「停止服务」交给 ``service_ctl.stop_service``（走 ``main.py stop``：写停止文件 →
宽限期 → 兜底强杀 → 清残留）。托盘不重复实现那套，只调用。

控制台则可以随时硬停：它只读数据文件 + 往信箱投命令，写文件走原子写 + 文件锁
（见 ``voice_loop/store.py``），不存在「停一半把数据写坏」。它的 pid 记在
``sessions/console.pid``（自己写的，见 ``console/app.py``），托盘的「关闭控制台」按它去关。

## 为什么是 pystray 而不是裸 ctypes

Pillow 本来就是硬依赖（截屏要用），pystray 是纯 Python 的薄封装。裸写
``Shell_NotifyIcon`` 要自己管隐藏窗口、消息泵、图标句柄回收，两百行起步，
在「只是想要个右键菜单」这件事上不划算。没装 pystray 也不影响服务本身：
:func:`spawn_tray` 只记一行日志就返回，语音链路照常跑。

## 线程模型

图标跑在**守护线程**里（Windows 上 pystray 自己建隐藏窗口 + 消息泵，不占主线程），
主线程在 :func:`run_standalone` 里等一个 ``Event``（菜单「退出」会 set 它）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER_NAME = "voice_loop.tray"

#: 状态轮询间隔（秒）：只用来更新 tooltip / 图标状态点 / 菜单可用性，不触发任何动作
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

    一个蓝底 + 三道声波柱；右下角一颗状态点：绿 = 服务在跑，灰 = 已停止。
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
    """一个菜单项（或一条分隔线）。``enabled`` 在 :meth:`ServiceTray.entries` 里算好。"""

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
    note: str = ""
    console: bool = False
    console_pid: int | None = None

    @property
    def status_text(self) -> str:
        if self.running:
            svc = f"服务运行中（PID {self.pid}）" if self.pid else "服务运行中"
        else:
            svc = "服务已停止"
        return f"{svc} · {'控制台在跑' if self.console else '控制台未开'}"


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
class ServiceTray:
    """服务 + 控制台的托盘。``start()`` 之后在守护线程里活着；所有动作都可注入（方便自测）。"""

    def __init__(
        self,
        settings: Any,
        *,
        log: logging.Logger | None = None,
        status: Callable[[], Any] | None = None,
        stop: Callable[[], tuple[bool, str]] | None = None,
        start: Callable[[], tuple[bool, str]] | None = None,
        open_url: Callable[[str], Any] | None = None,
        start_console: Callable[..., tuple[bool, str]] | None = None,
        close_console: Callable[..., tuple[bool, str]] | None = None,
        open_file: Callable[[Path], Any] | None = None,
        on_exit: Callable[[], None] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.log = log or logging.getLogger(LOGGER_NAME)
        self._status_fn = status or self._default_status
        self._stop_fn = stop or self._default_stop
        self._start_fn = start or self._default_start
        self._open_url = open_url or _open_url
        self._start_console = start_console or (
            lambda settings, log=None: start_console(settings, log))
        self._close_console = close_console or (
            lambda settings, log=None: _default_close_console(settings))
        self._open_file = open_file or _open_file
        self._on_exit = on_exit
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
        con = service_ctl.console_status(self.settings)
        return Snapshot(running=bool(st.running), pid=st.pid,
                        console=bool(con.running), console_pid=con.pid)

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
                        console=bool(getattr(got, "console", False)),
                        console_pid=getattr(got, "console_pid", None))

    @property
    def url(self) -> str:
        return console_url(self.settings)

    def entries(self) -> list[Entry]:
        """右键菜单里有什么（顺序就是显示顺序）。"""
        snap = self.snapshot()
        return [
            Entry(id="status", label=snap.status_text, enabled=False),
            _sep(),
            Entry(id="console", label="打开控制台", default=True),
            Entry(id="console_close", label="关闭控制台", enabled=snap.console),
            Entry(id="log", label="看服务日志"),
            _sep(),
            Entry(id="start", label="启动服务", enabled=not snap.running),
            Entry(id="stop", label="停止服务", enabled=snap.running),
            _sep(),
            Entry(id="quit", label="退出（停服务 + 关控制台）"),
        ]

    # ------------------------------------------------------------- 动作
    def _default_stop(self) -> tuple[bool, str]:
        from . import service_ctl  # noqa: PLC0415

        # tray_stop_grace 就是「等服务自己收尾等多久」：服务收尾含记忆归档 + 云端同步
        grace = float(getattr(getattr(self.settings, "app", None), "tray_stop_grace", 20.0) or 20.0)
        return service_ctl.stop_service(self.settings, timeout=grace)

    def _default_start(self) -> tuple[bool, str]:
        from . import service_ctl  # noqa: PLC0415

        return service_ctl.start_service(self.settings)

    def act(self, entry_id: str) -> tuple[bool, str]:
        """执行一个菜单动作（★自测直接调它，不用真点图标★）。"""
        allowed = {"console", "console_close", "log", "start", "stop", "quit"}
        if entry_id not in allowed:
            return False, "这个菜单项不能点"
        if not self._busy.acquire(blocking=False):
            return False, "上一个动作还没做完"
        try:
            if entry_id == "console":
                ok, msg = self._open_console()
                self._announce(msg, ok)
                return ok, msg
            if entry_id == "console_close":
                ok, msg = self._close_console(self.settings, self.log)
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            if entry_id == "log":
                self._open_file(self.settings.resolve(self.settings.wake.log_file))
                return True, "已打开服务日志"
            if entry_id == "start":
                ok, msg = self._start_fn()
                if ok:
                    # ★启动服务默认带控制台★：不然「起来了但没界面」还得再点一次
                    cok, cmsg = self._start_console(self.settings, self.log)
                    msg = f"{msg}；{cmsg}" if cok else f"{msg}；但{cmsg}"
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            if entry_id == "stop":
                # ★只停服务，托盘留着★：停完还能从这里再启动
                ok, msg = self._stop_fn()
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            return self._quit()
        except Exception as exc:  # noqa: BLE001 - 菜单动作失败不能带崩自己
            self.log.warning(f"[托盘] 动作 {entry_id} 失败：{exc}")
            return False, f"动作失败：{exc}"
        finally:
            self._busy.release()

    def _quit(self) -> tuple[bool, str]:
        """退出：**停服务 → 关控制台 → 收图标**（用户确认过：退出就是全收干净）。"""
        notes: list[str] = []
        snap = self.snapshot()
        if snap.running:
            try:
                ok, msg = self._stop_fn()
                notes.append(msg if ok else f"停服务没成：{msg}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"停服务出错：{exc}")
        if snap.console:
            try:
                ok, msg = self._close_console(self.settings, self.log)
                notes.append(msg if ok else f"关控制台没成：{msg}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"关控制台出错：{exc}")
        text = "；".join(notes) or "已退出"
        self._announce(text, True)
        self.stop()
        if self._on_exit is not None:
            self._on_exit()
        return True, text

    def _open_console(self) -> tuple[bool, str]:
        """「打开控制台」：没起就先拉起来，再开浏览器。"""
        ok, msg = self._start_console(self.settings, self.log)
        if ok:
            self._open_url(self.url)
            return True, msg
        return False, msg

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
        """起图标（守护线程）。失败只记日志，绝不影响服务本身。"""
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
            self._icon = pystray.Icon(
                "local_ai_service",
                icon=icon_image(running=self.snapshot().running),
                title=f"本地语音助手 · {self.snapshot().status_text}",
                menu=menu,
            )
            self._thread = threading.Thread(
                target=self._icon.run, name="service-tray", daemon=True)
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 - 托盘是附加能力，不是启动前提
            self.log.warning(f"[托盘] 图标没起来：{exc}")
            self.last_message = f"托盘图标没起来：{exc}"
            self._icon = None
            return False
        self._stop_poll.clear()
        self._poller = threading.Thread(target=self._poll_loop, name="service-tray-poll",
                                        daemon=True)
        self._poller.start()
        self.log.info("[托盘] 已就绪：右键可启停服务 / 开关控制台")
        return True

    def _menu_item(self, pystray: Any, entry: Entry) -> Any:
        if entry.is_sep:
            return pystray.Menu.SEPARATOR
        if entry.id == "status":
            # 状态行是灰色不可点的，文字在打开菜单时现算（服务/控制台可能刚变）
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

    def refresh(self) -> None:
        """更新 tooltip 与图标状态点（菜单文字在打开时会自己重算）。"""
        icon = self._icon
        if icon is None:
            return
        snap = self.snapshot()
        try:
            icon.title = f"本地语音助手 · {snap.status_text}"
            icon.icon = icon_image(running=snap.running)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[托盘] 刷新图标失败：{exc}")

    def _poll_loop(self) -> None:
        was = (self.snapshot().running, self.snapshot().console)
        while not self._stop_poll.wait(self._poll):
            snap = self.snapshot()
            now = (snap.running, snap.console)
            if now != was:
                was = now
                self.refresh()

    def stop(self) -> None:
        """收图标（幂等）。服务与控制台都不受影响。"""
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


# --------------------------------------------------------------------------- #
# 入口：拉起独立托盘进程 / 在进程里跑托盘
# --------------------------------------------------------------------------- #
def tray_enabled(settings: Any) -> bool:
    return bool(getattr(getattr(settings, "app", None), "tray", True))


def run_standalone(settings: Any, log: logging.Logger | None = None,
                   *, block: bool = True) -> int:
    """``main.py tray``：独立托盘进程（服务停了也还在，可以从这里再起回来）。"""
    log = log or logging.getLogger(LOGGER_NAME)
    ok, why = available()
    if not ok:
        print(f"× {why}", flush=True)
        return 1
    lock = _acquire_lock(settings)
    if lock is None:
        print("× 已经有一个托盘图标在跑了（sessions/tray.pid 是活着的）。", flush=True)
        return 1
    done = threading.Event()
    tray = ServiceTray(settings, log=log, on_exit=done.set)
    if not tray.start():
        print(f"× 托盘图标没起来：{tray.last_message or '未知原因'}", flush=True)
        _release_lock(lock)
        return 1
    print(f"√ 托盘图标已就绪（右键 → 启停服务 / 开关控制台 {tray.url}）", flush=True)
    if not block:
        return 0
    try:
        # 菜单「退出」会 set 这个 Event；Ctrl+C 也一样能走
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
        _release_lock(lock)
    return 0


def spawn_tray(settings: Any, log: logging.Logger | None = None, *,
               wait_seconds: float = 6.0,
               popen: Callable[..., Any] | None = None,
               pid_alive: Callable[[int], bool] | None = None) -> tuple[bool, str]:
    """确保**独立托盘进程**在跑；没跑就拉一个。返回 ``(ok, 人话)``。

    ★这是 ``listen`` 起服务时顺手起图标的方式★：托盘不再是服务进程里的一个线程
    （那样停服务就把图标一起带走了），而是一个 ``main.py tray`` 子进程，
    服务死了它还在，可以直接从菜单里把服务再起回来。
    """
    log = log or logging.getLogger(LOGGER_NAME)
    if not tray_enabled(settings):
        return False, "配置里关掉了托盘（[app] tray = false）"
    alive = pid_alive or _pid_alive
    path = _lock_path(settings)
    try:
        if path.exists():
            old = int(path.read_text(encoding="utf-8").strip() or 0)
        else:
            old = 0
    except (OSError, ValueError):
        old = 0
    if old and alive(old):
        return True, f"托盘已经在跑（PID {old}）"

    root = getattr(settings, "project_root", None) or Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "main.py", "tray"]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    spawn = popen or subprocess.Popen
    try:
        spawn(cmd, cwd=str(root), close_fds=True,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=flags)
    except OSError as exc:
        return False, f"托盘起不来：{exc}"

    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        try:
            if path.exists() and alive(int(path.read_text(encoding="utf-8").strip() or 0)):
                log.info("[托盘] 独立托盘进程已就绪")
                return True, "托盘图标已就绪"
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    if path.exists():
        # 进程起来了但还没写锁：别报错，图标多半就在
        return True, "托盘图标已就绪（进程已拉起）"
    return False, "托盘没起来（多半是没装 pystray / Pillow，pip install pystray pillow）"


def _lock_path(settings: Any) -> Path:
    return Path(settings.root) / "sessions" / "tray.pid"


def _acquire_lock(settings: Any) -> Path | None:
    """单实例保护：同一时间只留一个托盘图标。"""
    from . import service_ctl  # noqa: PLC0415

    path = _lock_path(settings)
    try:
        if path.exists():
            try:
                old = int(path.read_text(encoding="utf-8").strip() or 0)
            except ValueError:
                old = 0
            if old and old != os.getpid() and service_ctl.pid_alive(old):
                return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - 写不了锁就当没有，别为了锁把自己拦在门外
        print(f"[托盘] 单实例锁写不了（{exc}），继续启动", flush=True)
    return path


def _release_lock(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def console_url(settings: Any) -> str:
    from . import service_ctl  # noqa: PLC0415

    return f"http://127.0.0.1:{service_ctl.console_port(settings)}/"


def start_console(settings: Any, log: logging.Logger | None = None,
                  wait_seconds: float = 8.0,
                  popen: Callable[..., Any] | None = None,
                  port_open: Callable[..., bool] | None = None) -> tuple[bool, str]:
    """确保控制台在跑；没跑就拉一个独立进程。返回 ``(ok, 人话)``。

    ★这是「托盘点『打开控制台』打不开」的正解★：控制台是**另一个进程**
    （不占麦克风、不加载模型）。以前托盘点一下只是 `webbrowser.open(...)` ——
    浏览器老老实实打开一个「无法访问此网站」，看起来就像按钮坏了。
    现在先探端口，没起就自己拉起来再开浏览器。
    """
    log = log or logging.getLogger(LOGGER_NAME)
    from . import service_ctl  # noqa: PLC0415

    port = service_ctl.console_port(settings)
    probe = port_open or service_ctl.port_open
    st = service_ctl.console_status(settings)
    if st.running or probe(port):
        return True, f"控制台已经在跑（端口 {port}）"
    if st.stale:
        service_ctl.clear_console_pid(settings)

    root = getattr(settings, "project_root", None) or Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "main.py", "ui", "--port", str(port), "--no-browser"]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    spawn = popen or subprocess.Popen
    try:
        spawn(cmd, cwd=str(root), close_fds=True,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=flags)
    except OSError as exc:
        return False, f"控制台起不来：{exc}"
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if probe(port):
            log.info(f"[托盘] 控制台已就绪：{console_url(settings)}")
            return True, f"控制台已启动（端口 {port}）"
        time.sleep(0.2)
    return False, f"控制台没起来（端口 {port} 一直没响应，多半是缺 fastapi/uvicorn）"


def _default_close_console(settings: Any) -> tuple[bool, str]:
    from . import service_ctl  # noqa: PLC0415

    return service_ctl.stop_console(settings)


def _open_url(url: str) -> None:
    import webbrowser  # noqa: PLC0415

    webbrowser.open(url)


def _pid_alive(pid: int) -> bool:
    from . import service_ctl  # noqa: PLC0415

    return service_ctl.pid_alive(pid)


def _open_file(path: Path) -> None:
    """用系统默认程序打开（Windows 是记事本，方便直接看日志）。"""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])  # noqa: S603


__all__ = [
    "Entry", "POLL_SECONDS", "ServiceTray", "Snapshot", "available", "console_url",
    "icon_image", "run_standalone", "spawn_tray", "start_console", "tray_enabled",
]
