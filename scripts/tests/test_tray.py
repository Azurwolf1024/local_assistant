"""托盘图标自测（离线，不真的弹图标、不真的停服务）。

为什么值得钉住：服务用 pythonw 跑、没有控制台窗口，托盘是「怎么把它关掉」的抓手。
2026-11-20 改了设计（见 voice_loop/tray.py 的模块说明），这里钉三条新语义：

    1. **托盘独立于服务**：点「停止服务」只停服务，图标与进程都得留着（不然就没入口了）；
    2. **「退出」是全收**：停服务 + 关控制台 + 收图标（用户确认过就这个意思）；
    3. **「启动服务」顺手拉起控制台**（不然「起来了但没界面」还得再点一次）。

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
    settings.app.console_port = 65_001          # 别撞上真在跑的控制台（8765）
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
    print("\n[1] 右键菜单：状态说清「服务 + 控制台」，两项各自可点/灰")
    running = {"on": True, "console": False}

    def status():
        return tr.Snapshot(running=running["on"], pid=4321 if running["on"] else None,
                           console=running["console"], console_pid=88)

    tray = tr.ServiceTray(make_settings(tmp / "a"), status=status,
                         open_url=lambda _u: None, open_file=lambda _p: None)
    check("菜单项与顺序", ids(tray.entries()),
          ["status", "-", "console", "console_close", "log", "-", "start", "stop", "-", "quit"])
    check("服务在跑 → 「停止服务」可点", enabled_of(tray.entries(), "stop"), [True])
    check("服务在跑 → 「启动服务」灰着", enabled_of(tray.entries(), "start"), [False])
    check("控制台没开 → 「关闭控制台」灰着", enabled_of(tray.entries(), "console_close"), [False])
    check("状态行两件事都说", tray.snapshot().status_text,
          "服务运行中（PID 4321） · 控制台未开")
    check("双击图标 = 打开控制台", [e.default for e in tray.entries() if e.id == "console"], [True])

    running["console"] = True
    check("控制台在跑 → 「关闭控制台」可点", enabled_of(tray.entries(), "console_close"), [True])
    check("状态行跟着变", tray.snapshot().status_text,
          "服务运行中（PID 4321） · 控制台在跑")

    running["on"] = False
    running["console"] = False
    check("服务停了 → 「启动服务」可点", enabled_of(tray.entries(), "start"), [True])
    check("服务停了 → 「停止服务」灰着", enabled_of(tray.entries(), "stop"), [False])
    check("服务停了状态行也如实说", tray.snapshot().status_text, "服务已停止 · 控制台未开")
    check("控制台地址跟着 [app] console_port",
          tray.url, "http://127.0.0.1:65001/", detail=tray.url)


def test_stop_keeps_tray(tmp: Path) -> None:
    print("\n[2] ★「停止服务」只停服务，托盘不退出★（以前图标跟着服务一起没了）")
    called: list[str] = []
    exits: list[str] = []
    tray = tr.ServiceTray(make_settings(tmp / "b"),
                          status=lambda: tr.Snapshot(running=True, pid=7, console=True),
                          stop=lambda: (called.append("stop"), (True, "服务已停止（假）"))[1],
                          on_exit=lambda: exits.append("exit"),
                          open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray.act("stop")
    check("停服务走注入的实现", (ok, called), (True, ["stop"]), detail=msg)
    check("★没有触发退出★", exits, [])
    check("托盘还活着（_stop_poll 没被 set）", tray._stop_poll.is_set(), False)  # noqa: SLF001
    check("结果记进 last_message", tray.last_message, msg)


def test_quit_collects_all(tmp: Path) -> None:
    print("\n[3] ★「退出」= 停服务 + 关控制台 + 收图标★")
    called: list[str] = []
    exits: list[str] = []
    tray = tr.ServiceTray(make_settings(tmp / "c"),
                          status=lambda: tr.Snapshot(running=True, pid=7, console=True),
                          stop=lambda: (called.append("stop"), (True, "服务已停止（假）"))[1],
                          close_console=lambda settings, log=None: (
                              called.append("console"), (True, "控制台已关闭（假）"))[1],
                          on_exit=lambda: exits.append("exit"),
                          open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray.act("quit")
    check("先停服务、再关控制台", called, ["stop", "console"])
    check("★然后才退出（on_exit 被调用一次）★", exits, ["exit"])
    check("收尾说明里两件事都提到了", ("服务已停止（假）" in msg, "控制台已关闭（假）" in msg),
          (True, True), detail=msg)
    check("图标确实收了", tray._stop_poll.is_set(), True)  # noqa: SLF001
    check("没有「隐藏图标」这种半吊子项（用户要的就是全收干净）",
          "hide" not in ids(tray.entries()), True)


def test_quit_when_idle(tmp: Path) -> None:
    print("\n    ★服务/控制台本来就没开时，退出不该报错也不该乱调★")
    called: list[str] = []
    exits: list[str] = []
    tray = tr.ServiceTray(make_settings(tmp / "c2"),
                          status=lambda: tr.Snapshot(running=False, console=False),
                          stop=lambda: (called.append("stop"), (True, "x"))[1],
                          close_console=lambda settings, log=None: (
                              called.append("console"), (True, "x"))[1],
                          on_exit=lambda: exits.append("exit"),
                          open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray.act("quit")
    check("什么都没开着 → 一个动作都不做", called, [])
    check("但退出照旧生效", (ok, exits), (True, ["exit"]), detail=msg)


def test_start_brings_console(tmp: Path) -> None:
    print("\n[4] ★「启动服务」顺手把控制台也拉起来★（不然起来了还得再点一次）")
    called: list[str] = []
    tray = tr.ServiceTray(make_settings(tmp / "d"),
                          status=lambda: tr.Snapshot(running=False),
                          start=lambda: (called.append("start"), (True, "服务已启动（假）"))[1],
                          start_console=lambda settings, log=None: (
                              called.append("console"), (True, "控制台已启动（假）"))[1],
                          open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray.act("start")
    check("启动服务 + 启动控制台都做了", called, ["start", "console"], detail=msg)
    check("两句话都在返回里", ("服务已启动（假）" in msg, "控制台已启动（假）" in msg),
          (True, True))

    called.clear()
    tray2 = tr.ServiceTray(make_settings(tmp / "d2"),
                           status=lambda: tr.Snapshot(running=False),
                           start=lambda: (called.append("start"), (True, "起来了"))[1],
                           start_console=lambda settings, log=None: (False, "缺 fastapi"),
                           open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray2.act("start")
    check("服务起来了但控制台没起来 → 仍算成功，但话说清楚",
          (ok, "缺 fastapi" in msg), (True, True), detail=msg)


def test_open_close_console(tmp: Path) -> None:
    print("\n[5] 打开 / 关闭控制台（注入回调，不真开窗口）")
    opened: list[str] = []
    files: list[Path] = []
    started: list[str] = []

    def fake_start(settings, log=None):
        started.append("start")
        return True, "控制台已启动（端口 65001）"

    tray = tr.ServiceTray(make_settings(tmp / "e"), status=lambda: tr.Snapshot(running=False),
                          open_url=opened.append, open_file=files.append,
                          start_console=fake_start)
    ok, msg = tray.act("console")
    check("★打开控制台会先把控制台拉起来★", (ok, started), (True, ["start"]), detail=msg)
    check("打开控制台给了个 http 地址", opened[0].startswith("http://"), True, detail=opened[0])
    ok, _ = tray.act("log")
    check("看日志打开的是配置里的日志文件", (ok, files[0].name), (True, "listen.log"))

    # 起不来时：不开浏览器、如实报错（以前这里只是"打开了打不开的网页"）
    tray2 = tr.ServiceTray(make_settings(tmp / "e2"), status=lambda: tr.Snapshot(running=False),
                           open_url=opened.append,
                           start_console=lambda settings, log=None: (False, "缺 fastapi"))
    ok, msg = tray2.act("console")
    check("起不来就报错、不假装成功", (ok, "缺 fastapi" in msg), (False, True), detail=msg)
    check("起不来就不开浏览器", len(opened), 1)

    closed: list[str] = []
    tray3 = tr.ServiceTray(make_settings(tmp / "e3"),
                           status=lambda: tr.Snapshot(running=True, console=True),
                           close_console=lambda settings, log=None: (
                               closed.append("close"), (True, "控制台已关闭（假）"))[1],
                           open_url=lambda _u: None, open_file=lambda _p: None)
    ok, msg = tray3.act("console_close")
    check("关闭控制台走注入的实现", (ok, closed), (True, ["close"]), detail=msg)


def test_console_boot(tmp: Path) -> None:
    print("\n[6] start_console：端口探活 / 不重复起 / 起不来要说清")
    settings = make_settings(tmp / "c")
    # ★注意★：这里用注入的 port_open，真的 console_status 会去探 65001 端口
    spawned: list[list[str]] = []
    ok, msg = tr.start_console(settings, port_open=lambda *a, **k: True,
                               popen=spawned.append)
    check("已经跑着就不再拉一个", ok, True, detail=msg)
    check("没重复 spawn", len(spawned), 0)

    state = {"open": False}

    def fake_popen(cmd, **kw):
        spawned.append(list(cmd))
        state["open"] = True            # 一起就"通了"

    ok, msg = tr.start_console(settings, port_open=lambda *a, **k: state["open"],
                               popen=fake_popen, wait_seconds=2)
    check("没跑就拉一个", (ok, len(spawned)), (True, 1), detail=msg)
    check("拉的是 main.py ui 且指定端口",
          ("main.py" in spawned[0], "ui" in spawned[0], "65001" in spawned[0]),
          (True, True, True), detail=" ".join(spawned[0]))

    ok, msg = tr.start_console(settings, port_open=lambda *a, **k: False,
                               popen=lambda *a, **k: None, wait_seconds=0.3)
    check("一直没通就如实说（不再假装打开成功）", (ok, "没起来" in msg), (False, True), detail=msg)


def test_console_pid_file(tmp: Path) -> None:
    print("\n[7] 控制台 pid 记账：写 / 读 / 残留 / 清理（关闭控制台靠它）")
    settings = make_settings(tmp / "p")
    path = sc.console_pid_file(settings)
    check("一开始没有 pid 文件", path.exists(), False)
    st = sc.console_status(settings)
    check("没在跑", st.running, False)

    sc.write_console_pid(settings, 65_001)
    check("pid 文件写出来了", path.exists(), True)
    st = sc.console_status(settings)
    check("★记的是自己（活着的进程）→ 认为在跑★", (st.running, st.pid), (True, os.getpid()))

    sc.clear_console_pid(settings, pid=os.getpid() + 1)
    check("pid 对不上就不删（别误删别人的）", path.exists(), True)
    sc.clear_console_pid(settings, pid=os.getpid())
    check("pid 对得上才删", path.exists(), False)

    # 残留：文件里记了个死 pid
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 999999, "port": 65001}', encoding="utf-8")
    st = sc.console_status(settings)
    check("死 pid + 端口没人听 → 没在跑，且标成残留", (st.running, st.stale), (False, True))
    check("关一个没在跑的控制台 → 如实说，并顺手清残留",
          (sc.stop_console(settings)[0], path.exists()), (False, False))


def test_icon() -> None:
    print("\n[8] 图标是现画的（不依赖图片文件）")
    running = tr.icon_image(running=True)
    stopped = tr.icon_image(running=False)
    check("尺寸 / 通道", (running.size, running.mode), ((64, 64), "RGBA"))
    check("两种状态画得不一样（右下角状态点）", running.tobytes() != stopped.tobytes(), True)
    ok, why = tr.available()
    check("available() 返回 (bool, 原因)", isinstance(ok, bool) and isinstance(why, str),
          True, detail=f"{ok} {why}")


def test_fallbacks(tmp: Path) -> None:
    print("\n[9] 起不来的退路（服务不能因为没图标就挂）")
    settings = make_settings(tmp / "f")
    saved = tr.available
    try:
        tr.available = lambda: (False, "没装 pystray（pip install pystray）")  # type: ignore[assignment]
        tray = tr.ServiceTray(settings)
        check("没装 pystray → start() 返回 False", tray.start(), False)
        check("并且把原因留下来", "pystray" in tray.last_message, True, detail=tray.last_message)
        check("run_standalone 返回 1（命令行里会说原因）",
              tr.run_standalone(settings, block=False), 1)
        ok, msg = tr.spawn_tray(settings, popen=lambda *a, **k: None)
        check("spawn_tray 起不来也如实说（不抛异常）", ok, False, detail=msg)
    finally:
        tr.available = saved  # type: ignore[assignment]

    print("\n    [app] tray = false → 连试都不试")
    settings2 = make_settings(tmp / "g")
    settings2.app.tray = False
    ok, msg = tr.spawn_tray(settings2, popen=lambda *a, **k: None)
    check("被配置关掉了", ok, False, detail=msg)
    check("原因写的是配置", "[app] tray" in msg, True)


def test_spawn_tray(tmp: Path) -> None:
    print("\n[10] spawn_tray：单实例 / 拉的是 main.py tray")
    settings = make_settings(tmp / "h")
    lock = tr._lock_path(settings)  # noqa: SLF001
    spawned: list[list[str]] = []

    def fake_popen(cmd, **kw):
        spawned.append(list(cmd))
        lock.parent.mkdir(parents=True, exist_ok=True)
        # 假装子进程写好了自己的锁：用父进程 pid（一定活着，且不等于本进程）
        lock.write_text(str(os.getppid()), encoding="utf-8")

    ok, msg = tr.spawn_tray(settings, popen=fake_popen,
                            pid_alive=lambda p: p == os.getppid(), wait_seconds=2)
    check("没在跑 → 拉一个", (ok, len(spawned)), (True, 1), detail=msg)
    check("拉的是 main.py tray", ("main.py" in spawned[0], "tray" in spawned[0]),
          (True, True), detail=" ".join(spawned[0]))

    # 锁里现在是「另一个活着的 pid」→ 不该再拉第二个
    spawned.clear()
    ok, msg = tr.spawn_tray(settings, popen=fake_popen, wait_seconds=0.3)
    check("★已经有活的托盘 → 不再拉第二个★", (ok, len(spawned)), (True, 0), detail=msg)
    check("这时 `main.py tray` 自己也会放弃",
          tr.run_standalone(settings, block=False), 1)
    tr._release_lock(lock)  # noqa: SLF001


def test_single_instance(tmp: Path) -> None:
    print("\n[11] run_standalone 的单实例锁")
    settings = make_settings(tmp / "i")
    lock = tr._acquire_lock(settings)  # noqa: SLF001 - 单实例锁就是要能在测试里验
    check("第一次能拿到锁", lock is not None, True)
    check("锁文件里是自己的 pid",
          int(lock.read_text(encoding="utf-8")) == os.getpid(), True)
    lock.write_text(str(os.getppid()), encoding="utf-8")
    check("★别人已经占着锁时拿不到★", tr._acquire_lock(settings), None)  # noqa: SLF001
    tr._release_lock(lock)  # noqa: SLF001
    check("释放后能再拿到", tr._acquire_lock(settings) is not None, True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tray_"))
    try:
        test_menu(tmp)
        test_stop_keeps_tray(tmp)
        test_quit_collects_all(tmp)
        test_quit_when_idle(tmp)
        test_start_brings_console(tmp)
        test_open_close_console(tmp)
        test_console_boot(tmp)
        test_console_pid_file(tmp)
        test_icon()
        test_fallbacks(tmp)
        test_spawn_tray(tmp)
        test_single_instance(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 托盘自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
