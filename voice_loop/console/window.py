"""控制台窗口：让它像个软件，而不是「弹了个浏览器」。

三种后端，按「像不像原生应用」排：

============  ==========================================  ==============================
``pywebview``  真窗口（Edge WebView2 内核，没有地址栏/标签栏）  需要 ``pip install pywebview``
``edge``       Edge 的 ``--app=`` 模式：无地址栏无标签栏的窗口   只要机器上有 Edge（Win10/11 都有）
``browser``    普通浏览器开一个标签页                         什么依赖都不要
============  ==========================================  ==============================

★为什么要三级而不是只做 pywebview★：pywebview 在 Windows 上要 pythonnet（.NET），
冻进 exe 还会再胖一截；而「打不开窗口」这种事不能变成控制台起不来。所以
:func:`choose` 只负责挑一个**确实能用**的，:func:`prepare_window` 造出来的把手
起不来时由 ``serve()`` 退回浏览器 —— 用户最多是「没有原生窗口」，而不是「界面打不开」。

★2026-11-21 起它是「应用的窗口」★：除了开，还要能 ``hide`` / ``show`` / ``destroy`` ——
因为关窗口 = 最小化到托盘，不是退出应用（见 :class:`AppWindow` 与 voice_loop/tray.py）。

★``edge --app`` 为什么要单独的 ``--user-data-dir``★：不加的话 Edge 会把窗口
交给**已经在运行的那个 Edge 进程**，我们启动的这个进程立刻退出 ——
于是「关了窗口 = 结束控制台」这件事就断了（服务还在后台跑，用户以为关了）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
"""WebView2 Runtime 的固定产品 ID（Edge 更新器注册表里用它登记）。"""

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path() -> Path | None:
    """找 msedge.exe：标准安装路径 → 注册表 App Paths。"""
    for raw in EDGE_PATHS:
        path = Path(raw)
        if path.is_file():
            return path
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
            got, _ = winreg.QueryValueEx(key, "")
        path = Path(str(got))
        return path if path.is_file() else None
    except OSError:
        return None


def webview2_ready() -> tuple[bool, str]:
    """机器上有没有 WebView2 运行时（Win11 自带；Win10 可能没有）。"""
    if Path(r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application").is_dir():
        return True, "有 EdgeWebView 目录"
    if os.name != "nt":
        return False, "非 Windows：由 pywebview 自己挑内核"
    try:
        import winreg  # noqa: PLC0415

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for flag in (0, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive,
                                        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}",
                                        0, winreg.KEY_READ | flag) as key:
                        version, _ = winreg.QueryValueEx(key, "pv")
                    if str(version).strip("0."):
                        return True, f"注册表里有 WebView2 {version}"
                except OSError:
                    continue
    except ImportError:  # pragma: no cover - 非 Windows
        pass
    return False, "没找到 WebView2 运行时（装一下 Evergreen Runtime，或换 --window edge）"


def pywebview_ready() -> tuple[bool, str]:
    """pywebview 装了吗、内核在吗。"""
    try:
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec("webview") is None:
            return False, "没装 pywebview（pip install pywebview）"
    except (ImportError, ValueError):  # pragma: no cover
        return False, "pywebview 探测失败"
    return webview2_ready()


def backends() -> list[dict]:
    """三个后端各能不能用（``/api/meta`` 与控制台界面会显示它）。"""
    ok_wv, why_wv = pywebview_ready()
    edge = edge_path()
    out = [
        {"id": "pywebview", "name": "原生窗口（WebView2）", "ok": ok_wv, "detail": why_wv},
        {"id": "edge", "name": "Edge 应用窗口", "ok": edge is not None,
         "detail": str(edge) if edge else "没找到 msedge.exe"},
        {"id": "browser", "name": "系统浏览器", "ok": True, "detail": "始终可用"},
    ]
    return out


def choose(prefer: str = "auto") -> str:
    """挑一个能用的后端。``prefer`` 可以是 auto / pywebview / edge / browser / off。"""
    prefer = (prefer or "auto").strip().lower()
    ready = {item["id"]: item["ok"] for item in backends()}
    if prefer in ("off", "none", "browser"):
        return "browser"
    if prefer in ready:
        return prefer if ready[prefer] else "browser"
    for candidate in ("pywebview", "edge", "browser"):
        if ready.get(candidate):
            return candidate
    return "browser"


# --------------------------------------------------------------------------- #
# 应用主窗口：一个把手，三种后端
# --------------------------------------------------------------------------- #
class AppWindow:
    """控制台**应用的主窗口**。三种后端背后一个把手：``run`` / ``show`` / ``hide`` / ``destroy``。

    ★为什么要有这一层★：2026-11-21 起托盘的语义变成「**应用的**托盘」（见 tray.py），
    于是窗口必须能被**藏起来再拿出来**（关窗口 = 最小化到托盘，不是退出应用）。
    三种后端的「隐藏」根本不是一回事：

    ============  ============================  ==========================================
    后端           关窗口                        「再打开」
    ============  ============================  ==========================================
    ``pywebview``   拦下 closing → ``hide()``      ``show()``（同一个窗口对象）
    ``edge``        那个 Edge 进程退出（★不影响   重新 spawn 一个 ``--app`` 窗口
                    我们的应用进程★）
    ``browser``     关标签页（拦不住）             再 ``webbrowser.open`` 一次
    ============  ============================  ==========================================

    ``blocks_main_thread`` 只在 pywebview 上是 True：它要**主线程**跑自己的事件循环，
    所以 ``serve()`` 里必须「先起托盘、再让主线程进窗口循环」。
    """

    backend = "browser"
    blocks_main_thread = False

    def __init__(self, url: str, *, title: str = "", prefer: str = "auto",
                 profile_dir: Path | None = None, size: tuple[int, int] = (1200, 820),
                 on_user_close: Callable[[], bool] | None = None, logger=None) -> None:
        self.url = url
        self.title = title
        self.size = size
        self.profile_dir = profile_dir
        self.on_user_close = on_user_close
        self.log = logger or logging.getLogger("voice_loop.console.window")
        self.picked = choose(prefer)
        self._window: Any = None
        self._proc: Any = None
        self._allow_close = False       # destroy() 时置 True，把「拦下关闭」让路

    # ---- 子类实现 ----
    def run(self) -> None:
        """打开窗口。pywebview 会**阻塞到窗口被 destroy**，其余后端立刻返回。"""
        raise NotImplementedError

    def show(self) -> tuple[bool, str]:
        raise NotImplementedError

    def hide(self) -> tuple[bool, str]:
        return False, "这个后端藏不了窗口"

    def destroy(self) -> None:
        pass

    # ---- 公共 ----
    @property
    def can_hide(self) -> bool:
        return False

    def describe(self) -> str:
        return {"pywebview": "原生窗口", "edge": "Edge 应用窗口", "browser": "浏览器标签页"}.get(
            self.picked, self.picked)


class _BrowserWindow(AppWindow):
    backend = "browser"

    def run(self) -> None:
        self.show()

    def show(self) -> tuple[bool, str]:
        try:
            import webbrowser  # noqa: PLC0415

            webbrowser.open(self.url)
        except Exception as exc:  # noqa: BLE001 - 打不开浏览器不该让应用起不来
            return False, f"打不开浏览器（{exc}），请手动访问 {self.url}"
        return True, "已在浏览器里打开"


class _EdgeWindow(AppWindow):
    backend = "edge"

    def run(self) -> None:
        self.show()

    def _spawn(self) -> tuple[bool, str]:
        exe = edge_path()
        if exe is None:
            return False, "找不到 msedge.exe"
        profile = self.profile_dir or Path.cwd() / "sessions" / "edge-window"
        profile.mkdir(parents=True, exist_ok=True)
        cmd = [str(exe), f"--app={self.url}", f"--window-size={self.size[0]},{self.size[1]}",
               f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
               "--disable-features=Translate,msEdgeSplitScreen",
               "--disable-session-crashed-bubble"]
        try:
            self._proc = subprocess.Popen(cmd)
        except OSError as exc:
            return False, f"Edge 起不来（{exc}）"
        return True, "已打开 Edge 应用窗口"

    def show(self) -> tuple[bool, str]:
        # 已经有一个 Edge 窗口在跑 → 前面去（关掉过就重开一个）
        if self._proc is not None and self._proc.poll() is None:
            return True, "窗口已经开着（在任务栏里）"
        return self._spawn()

    def destroy(self) -> None:
        """只关我们自己起的那个 Edge 窗口进程；应用进程不受影响。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError as exc:
            self.log.debug(f"关窗口失败（忽略）：{exc}")


class _PyWebviewWindow(AppWindow):
    backend = "pywebview"
    blocks_main_thread = True

    @property
    def can_hide(self) -> bool:
        return True

    def run(self) -> None:
        """★必须在主线程调★：建窗口 + 进事件循环，``destroy()`` 之后才返回。"""
        try:
            import webview  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(f"pywebview 用不了（{exc}）") from exc
        self._webview = webview
        window = webview.create_window(self.title, self.url, width=self.size[0],
                                       height=self.size[1], min_size=(880, 600),
                                       text_select=True)
        self._window = window

        def _on_closing() -> bool:
            # ★返回 False = 拦下这次关闭★，然后藏起来（关窗口 = 最小化到托盘，不退出应用）
            if self._allow_close:
                return True          # 是「退出应用」叫的 destroy()，放行
            try:
                window.hide()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"藏窗口失败（当成真关）：{exc}")
                return True
            return False

        try:
            window.events.closing += _on_closing
        except Exception as exc:  # noqa: BLE001 - 老版本 pywebview 没有这个事件
            self.log.warning(f"这条 pywebview 不支持 closing 事件（关窗口会退出应用）：{exc}")
        kwargs: dict = {}
        if self.profile_dir is not None:
            kwargs["storage_path"] = str(self.profile_dir)
        webview.start(**kwargs)          # 阻塞：窗口被 destroy 才返回

    def show(self) -> tuple[bool, str]:
        window = self._window
        if window is None:
            return False, "窗口还没建好"
        try:
            window.show()
            window.restore() if hasattr(window, "restore") else None
        except Exception as exc:  # noqa: BLE001
            return False, f"显示窗口失败（{exc}）"
        return True, "窗口已显示"

    def hide(self) -> tuple[bool, str]:
        window = self._window
        if window is None:
            return False, "窗口还没建好"
        try:
            window.hide()
        except Exception as exc:  # noqa: BLE001
            return False, f"藏窗口失败（{exc}）"
        return True, "窗口已收到托盘"

    def destroy(self) -> None:
        window = self._window
        if window is None:
            return
        # ★必须先放行 closing★：否则「退出应用」调的 destroy() 会被自己的拦截器拦下，
        #   窗口永远关不掉、`webview.start()` 不返回、进程就挂在托盘里退不出去。
        self._allow_close = True
        try:
            window.destroy()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"关窗口失败（继续退出）：{exc}")


def prepare_window(url: str, *, title: str = "本地语音助手 · 控制台",
                   prefer: str = "auto", profile_dir: Path | None = None,
                   size: tuple[int, int] = (1200, 820),
                   on_user_close: Callable[[], bool] | None = None,
                   logger=None) -> AppWindow:
    """按 ``prefer`` 造一个窗口把手（**不起窗口**，起窗口是 :meth:`AppWindow.run`）。"""
    cls = {"pywebview": _PyWebviewWindow, "edge": _EdgeWindow,
           "browser": _BrowserWindow}.get(choose(prefer), _BrowserWindow)
    return cls(url, title=title, prefer=prefer, profile_dir=profile_dir, size=size,
               on_user_close=on_user_close, logger=logger)


def describe() -> dict:
    """给 ``/api/meta`` 用：三种后端各自的状态 + 这台机器上会挑哪个。"""
    return {"chosen": choose("auto"), "backends": backends(),
            "platform": sys.platform}
