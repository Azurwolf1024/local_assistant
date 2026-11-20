"""托盘小图标：右下角右键就能停 / 起语音服务。

## 为什么需要它

``python main.py listen -B`` 起的是一个**没有窗口的后台进程**（Windows 上用
``pythonw.exe``，连控制台都没有）。于是「怎么把它关掉」过去只有两个答案：
敲一条 ``python main.py stop``，或者去任务管理器里翻 pythonw 进程。
托盘图标补上第三个：右下角右键 → **停止服务**。

## 两种模式（同一个类，菜单不一样）

| 模式 | 谁在跑 | 菜单里多什么 |
|---|---|---|
| ``embedded`` | 图标在**服务进程内**（``listen`` 启动时顺手起一个） | 隐藏图标（服务继续跑） |
| ``standalone`` | 图标是**独立进程**（``main.py tray``） | 启动服务 / 退出托盘 |

嵌入式的好处是「图标在 = 服务在」，停服务时进程自己退出、图标跟着消失，没有残留；
独立进程的好处是服务停了图标还在，可以从菜单里再起回来。

## 停止是怎么生效的（★别用 kill★）

``voice_loop/pipeline.py`` 的常驻循环每轮检查 ``sessions/listen.stop``；
所以「停止服务」只是**写一个停止文件**，让服务自己收尾退出（关模型、关设备、
写日志）。只有它卡住时才由 ``main.py stop`` 兜底强杀 —— 那条路在
``voice_loop/service_ctl.py`` 里，托盘不重复实现。

## 为什么是 pystray 而不是裸 ctypes

Pillow 本来就是硬依赖（截屏要用），pystray 是纯 Python 的薄封装。裸写
``Shell_NotifyIcon`` 要自己管隐藏窗口、消息泵、图标句柄回收，两百行起步，
在「只是想要个右键菜单」这件事上不划算。没装 pystray 也不影响服务本身：
:func:`start_embedded` 只记一行日志就返回，语音链路照常跑。

## 线程模型

图标跑在**守护线程**里（Windows 上 pystray 自己建隐藏窗口 + 消息泵，不占主线程），
主线程继续跑语音循环 —— 和 ``voice_loop/console/`` 里那套窗口/uvicorn 的取舍一致。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER_NAME = "voice_loop.tray"

#: 状态轮询间隔（秒）：只用来更新 tooltip 与菜单项的可用状态，不触发任何动作
POLL_SECONDS = 5.0

#: 「停止服务」的宽限期（秒）：服务卡在长回合里时，等这么久还没退就强杀。
#: 和 `main.py stop` 里那段兜底是同一个数字量级（见 voice_loop/service_ctl.py）。
GRACE_SECONDS = 12.0

RUNNING, STOPPED = "running", "stopped"


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

    @property
    def status_text(self) -> str:
        if self.running:
            return f"服务运行中（PID {self.pid}）" if self.pid else "服务运行中"
        return "服务已停止"


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
class ServiceTray:
    """服务托盘。``start()`` 之后在守护线程里活着；所有动作都可注入（方便自测）。"""

    def __init__(
        self,
        settings: Any,
        *,
        mode: str = "embedded",
        log: logging.Logger | None = None,
        status: Callable[[], Any] | None = None,
        stop: Callable[[], tuple[bool, str]] | None = None,
        start: Callable[[], tuple[bool, str]] | None = None,
        open_url: Callable[[str], Any] | None = None,
        open_file: Callable[[Path], Any] | None = None,
        on_exit: Callable[[], None] | None = None,
        poll_seconds: float = POLL_SECONDS,
        grace_seconds: float = GRACE_SECONDS,
        alive: Callable[[int], bool] | None = None,
        kill: Callable[[int], tuple[bool, str]] | None = None,
    ) -> None:
        self.settings = settings
        self.mode = "standalone" if str(mode).lower().startswith("stand") else "embedded"
        self.log = log or logging.getLogger(LOGGER_NAME)
        self._status_fn = status or self._default_status
        self._stop_fn = stop or self._default_stop
        self._start_fn = start or self._default_start
        self._open_url = open_url or _open_url
        self._open_file = open_file or _open_file
        self._on_exit = on_exit
        self._poll = max(1.0, float(poll_seconds))
        self.grace = max(0.0, float(grace_seconds))
        self._alive = alive or _pid_alive
        self._kill = kill or _kill_pid
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
        return Snapshot(running=bool(st.running), pid=st.pid)

    def snapshot(self) -> Snapshot:
        try:
            got = self._status_fn()
        except Exception as exc:  # noqa: BLE001 - 状态读不出来不该让菜单炸掉
            self.log.warning(f"[托盘] 读服务状态失败：{exc}")
            return Snapshot(running=False, note=f"读不到状态：{exc}")
        if isinstance(got, Snapshot):
            return got
        return Snapshot(running=bool(getattr(got, "running", False)),
                        pid=getattr(got, "pid", None))

    @property
    def url(self) -> str:
        port = int(getattr(getattr(self.settings, "app", None), "console_port", 8765) or 8765)
        return f"http://127.0.0.1:{port}/"

    def entries(self) -> list[Entry]:
        """右键菜单里有什么（顺序就是显示顺序）。"""
        snap = self.snapshot()
        running = snap.running
        out = [
            Entry(id="status", label=snap.status_text, enabled=False),
            _sep(),
            Entry(id="console", label="打开控制台", default=True),
            Entry(id="log", label="看服务日志"),
            _sep(),
        ]
        if self.mode == "standalone":
            out.append(Entry(id="start", label="启动服务", enabled=not running))
        out.append(Entry(id="stop", label="停止服务", enabled=running))
        out.append(_sep())
        out.append(Entry(id="hide" if self.mode == "embedded" else "quit",
                         label="隐藏图标" if self.mode == "embedded" else "退出托盘"))
        return out

    # ------------------------------------------------------------- 动作
    def _signal_stop(self) -> tuple[bool, str]:
        """嵌入式：写停止文件让服务自己收尾；卡住了再兜底强杀。

        ★为什么还要兜底★：``pipeline`` 只在**每轮循环开头**看停止文件。服务正在
        回答一个长回合（ASR 复核 + 多轮工具）时，光写文件要等好几秒甚至十几秒才生效，
        而用户点「停止服务」的预期是「马上停」。所以同时起一个看门狗线程：
        宽限期内没退就 ``taskkill``（和 ``main.py stop`` 最后那段是同一招）。
        """
        path = self.settings.resolve(self.settings.wake.stop_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stop\n", encoding="utf-8")
        except OSError as exc:
            return False, f"写不了停止文件（{path}）：{exc}"
        # 嵌入式图标就活在服务进程里，os.getpid() 就是服务自己；
        # 万一有人在别的进程里建了「嵌入式」托盘，也按状态里的 pid 去杀，别误杀自己。
        self._escalate(self.snapshot().pid or os.getpid())
        return True, "已请求停止服务，正在收尾…"

    def _escalate(self, pid: int) -> None:
        """宽限期后还没退就强杀（守护线程，不挡菜单）。"""
        if not pid:
            return

        def worker() -> None:
            deadline = time.monotonic() + self.grace
            while time.monotonic() < deadline:
                time.sleep(0.5)
                if not self._alive(pid):
                    return
            ok, why = self._kill(pid)
            (self.log.info if ok else self.log.warning)(
                f"[托盘] 服务 {self.grace:g} 秒内没退出，已强制结束：{why}")

        threading.Thread(target=worker, name="tray-stop-watchdog", daemon=True).start()

    def _default_stop(self) -> tuple[bool, str]:
        if self.mode == "embedded":
            return self._signal_stop()
        from . import service_ctl  # noqa: PLC0415

        return service_ctl.stop_service(self.settings)

    def _default_start(self) -> tuple[bool, str]:
        from . import service_ctl  # noqa: PLC0415

        return service_ctl.start_service(self.settings)

    def act(self, entry_id: str) -> tuple[bool, str]:
        """执行一个菜单动作（★自测直接调它，不用真点图标★）。"""
        if entry_id in ("status", "") or entry_id not in {
            "console", "log", "stop", "start", "hide", "quit"
        }:
            return False, "这个菜单项不能点"
        if not self._busy.acquire(blocking=False):
            return False, "上一个动作还没做完"
        try:
            if entry_id == "console":
                self._open_url(self.url)
                return True, "已在浏览器打开控制台"
            if entry_id == "log":
                self._open_file(self.settings.resolve(self.settings.wake.log_file))
                return True, "已打开服务日志"
            if entry_id == "stop":
                ok, msg = self._stop_fn()
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            if entry_id == "start":
                ok, msg = self._start_fn()
                self._announce(msg, ok)
                self.refresh()
                return ok, msg
            # hide / quit：都是收起图标，区别只在独立模式要把进程也结束掉
            self.stop()
            if entry_id == "quit" and self._on_exit is not None:
                self._on_exit()
            return True, "图标已收起"
        except Exception as exc:  # noqa: BLE001 - 菜单动作失败不能带崩服务
            self.log.warning(f"[托盘] 动作 {entry_id} 失败：{exc}")
            return False, f"动作失败：{exc}"
        finally:
            self._busy.release()

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
        self.log.info(f"[托盘] 已就绪（{self.mode}）：右下角右键可停止服务")
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
        was = self.snapshot().running
        while not self._stop_poll.wait(self._poll):
            snap = self.snapshot()
            if snap.running != was:
                was = snap.running
                self.refresh()

    def stop(self) -> None:
        """收图标（幂等）。服务本身不受影响。"""
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
# 两个入口
# --------------------------------------------------------------------------- #
def start_embedded(settings: Any, log: logging.Logger | None = None) -> ServiceTray | None:
    """在服务进程里顺手起一个托盘。起不来就算了 —— 返回 ``None``。"""
    if not bool(getattr(getattr(settings, "app", None), "tray", True)):
        return None
    log = log or logging.getLogger(LOGGER_NAME)
    grace = float(getattr(getattr(settings, "app", None), "tray_stop_grace", 20.0) or 20.0)
    tray = ServiceTray(settings, mode="embedded", log=log, grace_seconds=grace)
    return tray if tray.start() else None


def run_standalone(settings: Any, log: logging.Logger | None = None,
                   *, block: bool = True) -> int:
    """``main.py tray``：独立托盘进程（服务停了也能从这里再起回来）。"""
    log = log or logging.getLogger(LOGGER_NAME)
    ok, why = available()
    if not ok:
        print(f"× {why}", flush=True)
        return 1
    lock = _acquire_lock(settings)
    if lock is None:
        print("× 已经有一个托盘图标在跑了（sessions/tray.pid 是活着的）。", flush=True)
        return 1
    grace = float(getattr(getattr(settings, "app", None), "tray_stop_grace", 20.0) or 20.0)
    tray = ServiceTray(settings, mode="standalone", log=log, grace_seconds=grace)
    if not tray.start():
        print(f"× 托盘图标没起来：{tray.last_message or '未知原因'}", flush=True)
        return 1
    print(f"√ 托盘图标已就绪（右键 → 停止服务 / 启动服务 / 打开控制台 {tray.url}）", flush=True)
    if not block:
        return 0
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
        _release_lock(lock)
    return 0


def _acquire_lock(settings: Any) -> Path | None:
    """单实例保护：同一时间只留一个托盘图标。"""
    from . import service_ctl  # noqa: PLC0415

    path = Path(settings.root) / "sessions" / "tray.pid"
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


def _open_url(url: str) -> None:
    import webbrowser  # noqa: PLC0415

    webbrowser.open(url)


def _pid_alive(pid: int) -> bool:
    from . import service_ctl  # noqa: PLC0415

    return service_ctl.pid_alive(pid)


def _kill_pid(pid: int) -> tuple[bool, str]:
    """强杀（最后手段，和 `main.py stop` 里那段一致）。"""
    import subprocess  # noqa: PLC0415

    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"已结束进程 {pid}"


def _open_file(path: Path) -> None:
    """用系统默认程序打开（Windows 是记事本，方便直接看日志）。"""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        return
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])  # noqa: S603
