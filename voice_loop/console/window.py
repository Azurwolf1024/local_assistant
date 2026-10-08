"""控制台窗口：让它像个软件，而不是「弹了个浏览器」。

三种后端，按「像不像原生应用」排：

============  ==========================================  ==============================
``pywebview``  真窗口（Edge WebView2 内核，没有地址栏/标签栏）  需要 ``pip install pywebview``
``edge``       Edge 的 ``--app=`` 模式：无地址栏无标签栏的窗口   只要机器上有 Edge（Win10/11 都有）
``browser``    普通浏览器开一个标签页                         什么依赖都不要
============  ==========================================  ==============================

★为什么要三级而不是只做 pywebview★：pywebview 在 Windows 上要 pythonnet（.NET），
冻进 exe 还会再胖一截；而「打不开窗口」这种事不能变成控制台起不来。所以
:func:`choose` 只负责挑一个**确实能用**的，:func:`open_window` 起不来时由 ``serve()``
退回浏览器 —— 用户最多是「没有原生窗口」，而不是「界面打不开」。

★``edge --app`` 为什么要单独的 ``--user-data-dir``★：不加的话 Edge 会把窗口
交给**已经在运行的那个 Edge 进程**，我们启动的这个进程立刻退出 ——
于是「关了窗口 = 结束控制台」这件事就断了（服务还在后台跑，用户以为关了）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


def open_window(url: str, *, title: str, backend: str = "auto", profile_dir: Path | None = None,
                size: tuple[int, int] = (1200, 820)) -> tuple[bool, str]:
    """开窗口并**阻塞到窗口关闭**。返回 (成功了吗, 一句话)。

    ★必须在主线程调★：pywebview 要求主线程（它要跑自己的事件循环），
    所以 ``serve()`` 里是「uvicorn 在后台线程、窗口占主线程」这个结构。
    """
    picked = choose(backend)
    if picked == "pywebview":
        try:
            import webview  # noqa: PLC0415
        except ImportError as exc:
            return False, f"pywebview 用不了（{exc}）"
        try:
            window = webview.create_window(title, url, width=size[0], height=size[1],
                                           min_size=(880, 600), text_select=True)
            kwargs: dict = {}
            if profile_dir is not None:
                kwargs["storage_path"] = str(profile_dir)
            webview.start(**kwargs)          # 阻塞：窗口关掉才返回
            _ = window
            return True, "窗口已关闭"
        except Exception as exc:  # noqa: BLE001 - 起不来就让上层退回浏览器
            return False, f"窗口起不来（{type(exc).__name__}: {exc}）"
    if picked == "edge":
        exe = edge_path()
        if exe is None:
            return False, "找不到 msedge.exe"
        profile = profile_dir or Path.cwd() / "sessions" / "edge-window"
        profile.mkdir(parents=True, exist_ok=True)
        cmd = [str(exe), f"--app={url}", f"--window-size={size[0]},{size[1]}",
               f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
               "--disable-features=Translate,msEdgeSplitScreen", "--disable-session-crashed-bubble"]
        try:
            proc = subprocess.Popen(cmd)
        except OSError as exc:
            return False, f"Edge 起不来（{exc}）"
        try:
            proc.wait()                      # ★等窗口关掉★：这样「关窗口 = 退出控制台」
        except KeyboardInterrupt:
            pass
        return True, "窗口已关闭"
    return False, "这个后端不是窗口（browser）"


def describe() -> dict:
    """给 ``/api/meta`` 用：三种后端各自的状态 + 这台机器上会挑哪个。"""
    return {"chosen": choose("auto"), "backends": backends(),
            "platform": sys.platform}
