"""托盘图标自测（离线，不真的弹图标、不真的停服务）。

为什么值得钉住：这个图标是后台服务**唯一的**「怎么关掉它」的抓手（服务用 pythonw
跑，没有控制台）。最容易错的两处都不在画面上：

    1. 「停止服务」到底做了什么 —— 必须是**写停止文件**（让服务自己收尾），
       不是 kill、也不是什么都不做；
    2. 服务已经停了的时候，那几项该谁灰谁亮（重复点「停止」不该报错堆栈）。

所以这里把状态、动作、打开方式全用假实现喂进去，只验菜单数据与副作用。

    python scripts\\tests\\test_tray.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

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
    """临时根目录：绝不碰真实的 sessions/listen.stop。"""
    settings = load_settings()
    settings.app.project_root = str(tmp)
    settings.app.sessions_dir = "sessions"
    settings.wake.stop_file = "sessions/listen.stop"
    settings.wake.log_file = "sessions/listen.log"
    return settings


def ids(entries) -> list[str]:
    return [e.id if not e.is_sep else "-" for e in entries]


# --------------------------------------------------------------------------- #
def test_menu(tmp: Path) -> None:
    print("\n[1] 右键菜单长什么样（两种模式）")
    running = {"on": True, "pid": 4321}

    def status():
        return tr.Snapshot(running=running["on"], pid=running["pid"] if running["on"] else None)

    emb = tr.ServiceTray(make_settings(tmp / "a"), mode="embedded", status=status,
                         open_url=lambda _u: None, open_file=lambda _p: None)
    check("嵌入模式：状态行 + 控制台 + 日志 + 停止 + 隐藏", ids(emb.entries()),
          ["status", "-", "console", "log", "-", "stop", "-", "hide"])
    stop = [e for e in emb.entries() if e.id == "stop"][0]
    check("服务在跑 → 「停止服务」可点", stop.enabled, True)
    check("嵌入式没有「启动服务」（图标跟着服务走）",
          "start" not in ids(emb.entries()), True)
    check("状态行说清楚 PID", emb.snapshot().status_text, "服务运行中（PID 4321）")
    check("双击图标 = 打开控制台",
          [e.default for e in emb.entries() if e.id == "console"], [True])

    running["on"] = False
    check("服务停了 → 「停止服务」变灰",
          [e.enabled for e in emb.entries() if e.id == "stop"], [False])
    check("状态行改成「已停止」", emb.snapshot().status_text, "服务已停止")

    running["on"] = True
    alone = tr.ServiceTray(make_settings(tmp / "b"), mode="standalone", status=status,
                           open_url=lambda _u: None, open_file=lambda _p: None)
    check("独立模式多出「启动服务」和「退出托盘」",
          ids(alone.entries()), ["status", "-", "console", "log", "-", "start", "stop", "-", "quit"])
    check("服务在跑 → 「启动服务」灰着",
          [e.enabled for e in alone.entries() if e.id == "start"], [False])
    running["on"] = False
    check("服务停了 → 「启动服务」可点",
          [e.enabled for e in alone.entries() if e.id == "start"], [True])
    check("控制台地址跟着 [app] console_port",
          alone.url.startswith("http://127.0.0.1:"), True, detail=alone.url)


def test_stop_action(tmp: Path) -> None:
    print("\n[2] 「停止服务」到底做了什么（嵌入式：写停止文件）")
    settings = make_settings(tmp / "c")
    tray = tr.ServiceTray(settings, mode="embedded", status=lambda: tr.Snapshot(True, 99),
                          open_url=lambda _u: None, open_file=lambda _p: None)
    stop_file = settings.resolve(settings.wake.stop_file)
    check("动手前没有停止文件", stop_file.exists(), False)
    ok, msg = tray.act("stop")
    check("点了就返回成功", ok, True, detail=msg)
    check("★写了停止文件（服务自己收尾，不是 kill）★", stop_file.exists(), True)
    check("内容不是空的（服务只认「存在」这件事）", stop_file.read_text(encoding="utf-8").strip() != "", True)
    check("结果也记进了 last_message", tray.last_message, msg)

    print("\n    ★服务卡在长回合里时：宽限期一到就兜底强杀★")
    kills: list[int] = []
    holder = {"alive": True}

    def fake_kill(pid: int):
        kills.append(pid)
        holder["alive"] = False
        return True, "已结束进程"

    slow = tr.ServiceTray(settings, mode="embedded", status=lambda: tr.Snapshot(True, 99),
                          open_url=lambda _u: None, open_file=lambda _p: None,
                          grace_seconds=0.3, alive=lambda _p: holder["alive"], kill=fake_kill)
    stop_file.unlink(missing_ok=True)
    slow.act("stop")
    time.sleep(1.2)
    check("停止文件 + 看门狗都动了（杀的是状态里的服务 pid）",
          (stop_file.exists(), kills), (True, [99]))

    quiet = tr.ServiceTray(settings, mode="embedded", status=lambda: tr.Snapshot(True, 99),
                           open_url=lambda _u: None, open_file=lambda _p: None,
                           grace_seconds=0.3, alive=lambda _p: False, kill=fake_kill)
    kills.clear()
    quiet.act("stop")
    time.sleep(1.0)
    check("服务自己退了就不强杀", kills, [])

    print("\n    独立模式：交给 service_ctl（这里用假的）")
    called: list[str] = []

    def fake_stop():
        called.append("stop")
        return True, "服务已停止（假实现）"

    def fake_start():
        called.append("start")
        return True, "服务已启动（假实现）"

    alone = tr.ServiceTray(make_settings(tmp / "d"), mode="standalone",
                           status=lambda: tr.Snapshot(True, 7), stop=fake_stop,
                           start=fake_start, open_url=lambda _u: None,
                           open_file=lambda _p: None)
    ok, msg = alone.act("stop")
    check("独立模式走注入的 stop", (ok, called), (True, ["stop"]), detail=msg)
    ok, _ = alone.act("start")
    check("启动也走注入的 start", (ok, called[-1]), (True, "start"))

    check("未知菜单项不会执行任何东西", alone.act("status"), (False, "这个菜单项不能点"))
    check("隐藏/退出会收起图标（没起过也安全）", alone.act("hide")[0], True)


def test_open_actions(tmp: Path) -> None:
    print("\n[3] 打开控制台 / 看日志（注入的回调，不真开窗口）")
    opened: list[str] = []
    files: list[Path] = []
    tray = tr.ServiceTray(make_settings(tmp / "e"), mode="standalone",
                          status=lambda: tr.Snapshot(False),
                          open_url=opened.append, open_file=files.append)
    ok, _ = tray.act("console")
    check("打开控制台给了个 http 地址", (ok, opened[0].startswith("http://")), (True, True))
    ok, _ = tray.act("log")
    check("看日志打开的是配置里的日志文件", (ok, files[0].name), (True, "listen.log"))


def test_icon() -> None:
    print("\n[4] 图标是现画的（不依赖图片文件）")
    running = tr.icon_image(running=True)
    stopped = tr.icon_image(running=False)
    check("尺寸 / 通道", (running.size, running.mode), ((64, 64), "RGBA"))
    check("两种状态画得不一样（右下角状态点）", running.tobytes() != stopped.tobytes(), True)
    ok, why = tr.available()
    check("available() 返回 (bool, 原因)", isinstance(ok, bool) and isinstance(why, str),
          True, detail=f"{ok} {why}")


def test_fallbacks(tmp: Path) -> None:
    print("\n[5] 起不来的退路（服务不能因为没图标就挂）")
    settings = make_settings(tmp / "f")
    saved = tr.available
    try:
        tr.available = lambda: (False, "没装 pystray（pip install pystray）")  # type: ignore[assignment]
        tray = tr.ServiceTray(settings, mode="embedded")
        check("没装 pystray → start() 返回 False", tray.start(), False)
        check("并且把原因留下来", "pystray" in tray.last_message, True, detail=tray.last_message)
        check("start_embedded 也返回 None", tr.start_embedded(settings), None)
        check("run_standalone 返回 1（命令行里会说原因）",
              tr.run_standalone(settings, block=False), 1)
    finally:
        tr.available = saved  # type: ignore[assignment]

    print("\n    [app] tray = false → 连试都不试")
    settings2 = make_settings(tmp / "g")
    settings2.app.tray = False
    check("嵌入式被配置关掉了", tr.start_embedded(settings2), None)


def test_single_instance(tmp: Path) -> None:
    print("\n[6] 独立托盘的单实例保护")

    settings = make_settings(tmp / "h")
    lock = tr._acquire_lock(settings)  # noqa: SLF001 - 单实例锁就是要能在测试里验
    check("第一次能拿到锁", lock is not None, True)
    check("锁文件里是自己的 pid",
          int(lock.read_text(encoding="utf-8")) == os.getpid(), True)

    # 假装「另一个活着的进程」已经占了锁（用父进程的 pid：它一定还活着）
    lock.write_text(str(os.getppid()), encoding="utf-8")
    check("★别人已经占着锁时拿不到★", tr._acquire_lock(settings), None)  # noqa: SLF001
    check("这时 `main.py tray` 直接退出（不会起第二个图标）",
          tr.run_standalone(settings, block=False), 1)
    # 收尾：把锁清掉，别把真正的单实例判断留给后面的人
    tr._release_lock(lock)  # noqa: SLF001


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tray_"))
    try:
        test_menu(tmp)
        test_stop_action(tmp)
        test_open_actions(tmp)
        test_icon()
        test_fallbacks(tmp)
        test_single_instance(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        # 别把真正在跑的服务的停止文件删了：只有测试临时目录里的才删
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 托盘图标自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
