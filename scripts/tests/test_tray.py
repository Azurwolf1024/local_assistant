"""托盘与「应用」自测（离线：不弹图标、不起窗口、不停真服务）。

2026-11-21 起定了新的归属（见 voice_loop/tray.py 的模块说明）：

    **控制台是应用，托盘是应用的托盘**；语音服务是应用带着的后台子系统。

于是三条手感必须在代码里钉住，否则改着改着就退回「托盘跟服务一起生灭」：

    1. 关窗口 ≠ 退出应用（窗口要能藏起来再叫回来）；
    2. 「停止服务」只停服务，应用和托盘都留着；
    3. 「退出应用」= 停服务 → 关窗口 → 通知主线程退出，且**幂等**。

    python scripts\\tests\\test_tray.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop import service_ctl as sc  # noqa: E402
from voice_loop import tray as tr  # noqa: E402
from voice_loop.console import window as wm  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def make_settings(tmp: Path):
    """临时根目录：绝不碰真实的 sessions/listen.pid 与 console.pid。"""
    settings = load_settings()
    settings.app.project_root = str(tmp)
    settings.app.sessions_dir = "sessions"
    settings.app.console_port = 65_001          # 别撞上真在跑的应用（8765）
    settings.wake.stop_file = "sessions/listen.stop"
    settings.wake.pid_file = "sessions/listen.pid"
    settings.wake.log_file = "sessions/listen.log"
    return settings


def ids(entries) -> list[str]:
    return [e.id if not e.is_sep else "-" for e in entries]


def enabled_of(entries, want: str) -> list[bool]:
    return [e.enabled for e in entries if e.id == want]


# --------------------------------------------------------------------------- #
def test_menu(tmp: Path) -> None:
    print("\n[1] 菜单是「这个应用」的菜单：显示窗口 / 启停服务 / 看日志 / 退出")
    running = {"on": True}

    def status():
        return tr.Snapshot(running=running["on"], pid=4321 if running["on"] else None,
                           port=65001)

    tray = tr.AppTray(make_settings(tmp / "a"), status=status,
                      open_file=lambda _p: None)
    check("菜单项与顺序", ids(tray.entries()),
          ["status", "-", "show", "start", "stop", "log", "-", "quit"])
    check("★双击图标 = 显示控制台窗口★",
          [e.default for e in tray.entries() if e.id == "show"], [True])
    check("服务在跑 → 「停止服务」可点", enabled_of(tray.entries(), "stop"), [True])
    check("服务在跑 → 「启动服务」灰着", enabled_of(tray.entries(), "start"), [False])
    check("状态行写明服务 + 控制台端口", tray.snapshot().status_text,
          "服务运行中（PID 4321） · 控制台 65001")
    check("没有「隐藏图标」这种半吊子项（关窗口就是藏到托盘）",
          "hide" not in ids(tray.entries()), True)

    running["on"] = False
    check("服务停了 → 「启动服务」可点", enabled_of(tray.entries(), "start"), [True])
    check("服务停了 → 「停止服务」灰着", enabled_of(tray.entries(), "stop"), [False])
    check("状态行如实说", tray.snapshot().status_text, "服务已停止 · 控制台 65001")


def test_show_window(tmp: Path) -> None:
    print("\n[2] 「显示控制台窗口」：走注入的回调，失败要如实说")
    seen: list[str] = []
    tray = tr.AppTray(make_settings(tmp / "b"), status=lambda: tr.Snapshot(True, 7, 65001),
                      show_window=lambda: (seen.append("show"), (True, "窗口已显示"))[1],
                      open_file=lambda _p: None)
    ok, msg = tray.act("show")
    check("调了显示窗口", (ok, seen), (True, ["show"]), detail=msg)

    tray2 = tr.AppTray(make_settings(tmp / "b2"), status=lambda: tr.Snapshot(True, 7, 65001),
                       show_window=lambda: (False, "这个应用没开窗口"),
                       open_file=lambda _p: None)
    ok, msg = tray2.act("show")
    check("没窗口时如实说，不假装成功", (ok, msg), (False, "这个应用没开窗口"))


def test_stop_keeps_app(tmp: Path) -> None:
    print("\n[3] ★「停止服务」只停服务，应用和托盘都留着★")
    called: list[str] = []
    quits: list[str] = []
    tray = tr.AppTray(make_settings(tmp / "c"),
                      status=lambda: tr.Snapshot(True, 7, 65001),
                      stop_service=lambda: (called.append("stop"), (True, "服务已停止（假）"))[1],
                      start_service=lambda: (called.append("start"), (True, "服务已启动（假）"))[1],
                      quit_app=lambda: quits.append("quit"),
                      open_file=lambda _p: None)
    ok, msg = tray.act("stop")
    check("停服务走注入的实现", (ok, called), (True, ["stop"]), detail=msg)
    check("★没有触发退出应用★", quits, [])
    check("托盘还活着（_stop_poll 没被 set）", tray._stop_poll.is_set(), False)  # noqa: SLF001
    check("结果记进 last_message", tray.last_message, msg)

    called.clear()
    ok, _ = tray.act("start")
    check("启动也走注入的实现", (ok, called), (True, ["start"]))


def test_quit_app(tmp: Path) -> None:
    print("\n[4] ★「退出应用」交给应用去收尾（停服务 → 关窗口 → 退出）★")
    quits: list[str] = []
    tray = tr.AppTray(make_settings(tmp / "d"),
                      status=lambda: tr.Snapshot(True, 7, 65001),
                      quit_app=lambda: quits.append("quit"),
                      open_file=lambda _p: None)
    ok, msg = tray.act("quit")
    check("调了应用的退出动作", (ok, quits), (True, ["quit"]), detail=msg)
    check("未知菜单项不会执行任何东西", tray.act("status"), (False, "这个菜单项不能点"))


def test_app_control(tmp: Path) -> None:
    print("\n[5] 应用的退出协调器：顺序（停服务 → 关窗口 → 通知）+ 幂等")
    from voice_loop.console import app as console_app

    order: list[str] = []
    saved_status, saved_stop = sc.status, sc.stop_service
    try:
        sc.status = lambda _s: type("St", (), {"running": True, "pid": 7})()  # type: ignore[assignment]
        sc.stop_service = lambda *_a, **_k: (order.append("stop"), (True, "服务已停止"))[1]  # type: ignore[assignment]

        done = __import__("threading").Event()
        ctl = console_app._Application(make_settings(tmp / "e"), log=__import__("logging").getLogger("t"),
                                       server=object(), done=done, grace=1.0)

        class FakeWindow:
            def destroy(self_inner):
                order.append("window")

        ctl.set_window(FakeWindow())
        ctl.quit("测试")
        check("顺序对（先停服务再关窗口）", order, ["stop", "window"])
        check("★退出事件被 set★", done.is_set(), True)

        # 幂等：再点一次不该再停一遍服务
        order.clear()
        ctl.quit("第二次")
        check("★重复退出是空操作★", order, [])
    finally:
        sc.status, sc.stop_service = saved_status, saved_stop

    print("\n    没窗口的 headless 应用：显示窗口要如实说")
    ctl2 = console_app._Application(make_settings(tmp / "e2"),
                                    log=__import__("logging").getLogger("t"),
                                    server=object(), done=__import__("threading").Event())
    ok, msg = ctl2.show_window()
    check("没窗口 → 提示用浏览器", (ok, "浏览器" in msg), (False, True), detail=msg)


def test_ensure_service(tmp: Path) -> None:
    print("\n[6] ★应用启动时把服务带起来（已在跑就不重复起）★")
    import logging

    from voice_loop.console import app as console_app

    calls: list[str] = []
    saved_status, saved_start = sc.status, sc.start_service
    try:
        sc.status = lambda _s: type("St", (), {"running": True, "pid": 99})()  # type: ignore[assignment]
        sc.start_service = lambda *_a, **_k: (calls.append("start"), (True, "起来了"))[1]  # type: ignore[assignment]
        msg = console_app._ensure_service(make_settings(tmp / "f"), logging.getLogger("t"))
        check("服务已经在跑 → 不再启动", calls, [])
        check("并且说清楚 PID", "99" in msg, True, detail=msg)

        sc.status = lambda _s: type("St", (), {"running": False, "pid": None})()  # type: ignore[assignment]
        msg = console_app._ensure_service(make_settings(tmp / "f2"), logging.getLogger("t"))
        check("没在跑 → 启动一次", calls, ["start"], detail=msg)
    finally:
        sc.status, sc.start_service = saved_status, saved_start


def test_windows(tmp: Path) -> None:
    print("\n[7] 窗口把手：三种后端各是什么脾气")
    check("off/none 不当窗口（退回浏览器后端）", wm.choose("off"), "browser")
    win = wm.prepare_window("http://127.0.0.1:1/", prefer="browser")
    check("browser 不占主线程", win.blocks_main_thread, False)
    check("browser 藏不了窗口（关标签页拦不住）", win.can_hide, False)

    opened: list[str] = []
    saved_open = wm.webbrowser.open if hasattr(wm, "webbrowser") else None
    import webbrowser

    saved = webbrowser.open
    try:
        webbrowser.open = lambda url, *a, **k: (opened.append(url), True)[1]
        ok, msg = win.show()
        check("browser 的「显示」= 再开一次标签页", (ok, opened), (True, ["http://127.0.0.1:1/"]),
              detail=msg)
    finally:
        webbrowser.open = saved
    _ = saved_open

    wv = wm.prepare_window("http://127.0.0.1:1/", prefer="pywebview")
    if wv.backend == "pywebview":
        check("pywebview 要主线程", wv.blocks_main_thread, True)
        check("★pywebview 能藏窗口（关窗口 = 收到托盘）★", wv.can_hide, True)
    else:
        check("这台机器没有 pywebview，退回了 browser", wv.backend, "browser")

    print("\n    Edge 应用窗口：关掉窗口 = 那个 Edge 进程退出，应用不受影响")
    edge = wm.prepare_window("http://127.0.0.1:1/", prefer="edge",
                             profile_dir=tmp / "edge-profile")
    if edge.backend != "edge":
        check("这台机器没有 Edge，跳过", True, True)
    else:
        killed: list[str] = []

        class FakeProc:
            def __init__(self):
                self.alive = True
                self.n = 0

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False
                killed.append("terminate")

        procs: list[FakeProc] = []

        def fake_popen(cmd, **kw):
            p = FakeProc()
            procs.append(p)
            return p

        saved_popen, saved_edge = wm.subprocess.Popen, wm.edge_path
        try:
            wm.edge_path = lambda: Path("C:/fake/msedge.exe")  # type: ignore[assignment]
            wm.subprocess.Popen = fake_popen  # type: ignore[assignment]
            ok, msg = edge.show()
            check("显示窗口 = 起一个 Edge --app 进程", (ok, len(procs)), (True, 1), detail=msg)
            ok, msg = edge.show()
            check("已经开着就不重复起", (ok, len(procs)), (True, 1), detail=msg)
            edge.destroy()
            check("destroy = 只关那个窗口进程", killed, ["terminate"])
            ok, msg = edge.show()
            check("★关掉之后再显示：重新起一个★", (ok, len(procs)), (True, 2), detail=msg)
        finally:
            wm.subprocess.Popen = saved_popen
            wm.edge_path = saved_edge


def test_console_pid_file(tmp: Path) -> None:
    print("\n[8] 应用 pid 记账：写 / 读 / 残留 / 清理")
    settings = make_settings(tmp / "p")
    path = sc.console_pid_file(settings)
    check("一开始没有 pid 文件", path.exists(), False)
    check("没在跑", sc.console_status(settings).running, False)

    sc.write_console_pid(settings, 65_001)
    st = sc.console_status(settings)
    check("★记的是自己（活着的进程）→ 认为在跑★", (st.running, st.pid), (True, os.getpid()))

    sc.clear_console_pid(settings, pid=os.getpid() + 1)
    check("pid 对不上就不删（别误删别人的）", path.exists(), True)
    sc.clear_console_pid(settings, pid=os.getpid())
    check("pid 对得上才删", path.exists(), False)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 999999, "port": 65001}', encoding="utf-8")
    st = sc.console_status(settings)
    check("死 pid + 端口没人听 → 没在跑，且标成残留", (st.running, st.stale), (False, True))
    check("关一个没在跑的应用 → 如实说，并顺手清残留",
          (sc.stop_console(settings)[0], path.exists()), (False, False))

    print("\n    ★端口被占但没 pid 文件时：绝不能乱杀别人的进程★")
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(16)          # 攒够队列：探活会连好几次，backlog=1 会被拒
        settings.app.console_port = int(holder.getsockname()[1])
        sc.clear_console_pid(settings)
        st = sc.console_status(settings)
        check("端口在听就认为在跑（老版本没留 pid）", (st.running, st.pid), (True, None))
        ok, msg = sc.stop_console(settings)
        check("但认不出是控制台 → 不动手，给手动查法", (ok, "不是控制台" in msg), (False, True),
              detail=msg)
        check("★测试进程自己还活着（没被误杀）★", True, True)


def test_icon() -> None:
    print("\n[9] 图标是现画的（不依赖图片文件）")
    running = tr.icon_image(running=True)
    stopped = tr.icon_image(running=False)
    check("尺寸 / 通道", (running.size, running.mode), ((64, 64), "RGBA"))
    check("两种状态画得不一样（右下角状态点）", running.tobytes() != stopped.tobytes(), True)
    ok, why = tr.available()
    check("available() 返回 (bool, 原因)", isinstance(ok, bool) and isinstance(why, str),
          True, detail=f"{ok} {why}")


def test_fallbacks(tmp: Path) -> None:
    print("\n[10] 起不来的退路（应用不能因为没图标就挂）")
    settings = make_settings(tmp / "g")
    saved = tr.available
    try:
        tr.available = lambda: (False, "没装 pystray（pip install pystray）")  # type: ignore[assignment]
        tray = tr.AppTray(settings)
        check("没装 pystray → start() 返回 False", tray.start(), False)
        check("并且把原因留下来", "pystray" in tray.last_message, True, detail=tray.last_message)
        check("start_app_tray 返回 None（应用照常跑）", tr.start_app_tray(settings), None)
    finally:
        tr.available = saved  # type: ignore[assignment]

    print("\n    [app] tray = false → 连试都不试")
    settings2 = make_settings(tmp / "h")
    settings2.app.tray = False
    check("配置关掉了托盘", tr.start_app_tray(settings2), None)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="app_tray_"))
    try:
        test_menu(tmp)
        test_show_window(tmp)
        test_stop_keeps_app(tmp)
        test_quit_app(tmp)
        test_app_control(tmp)
        test_ensure_service(tmp)
        test_windows(tmp)
        test_console_pid_file(tmp)
        test_icon()
        test_fallbacks(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 托盘 / 应用自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
