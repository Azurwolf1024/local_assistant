"""服务启停逻辑的自测（**纯离线，绝不真的起服务**）。

要钉住的是那些「一点就看起来坏了」的分支——用户报的「UI 启停异常」正是这些：

    §2 没有 pid 文件、服务也没在跑 → 就是「没在跑」，**不能**报成「pid 文件里是旧进程」
       （旧实现把日志里恢复出来的旧 pid 当状态，于是点启动被拒、点停止说没东西可停）
    §3 有残留文件 → 启动时**先自动清理再起**（而不是让用户自己点停止）
    §4 已经在跑 → 拒绝启动，并说清楚 PID 与来源
    §5 ★起了但没起来★ → 回失败 + 带上日志尾行（不能盲目报「已启动」）
    §6 停止：没在跑 → 明确说；停完要**验证**真的没了才算成功

    python scripts/test_service_ctl.py

★怎么保证不起真服务★：把 `service_ctl.spawn` / `subprocess.run` 换成假的
（只记下被调的参数，然后按剧本改“状态”），所以这个脚本永远只会写临时目录里的文件。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import service_ctl as sc  # noqa: E402
from voice_loop.settings import Settings, load_settings  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    # ★踩过的坑★：传 want=None 时本意是「期望是 None」，但这个签名把 None 当成了
    # 「没给期望值」→ 变成真值判断，断言会被反过来。要断言 None 就写显式布尔：
    #     check("pid 是 None", st.pid is None)      ← 这么写
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def dead_pid() -> int:
    """一个**确定已经退出**的 pid（用刚跑完的子进程，避免撞上 PID 复用）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return int(proc.pid)


class FakeRun:
    """假 subprocess.run：记下被调的命令，并按时执行剧本（写/删文件、记录日志行）。"""

    def __init__(self, script=None) -> None:
        self.calls: list[list[str]] = []
        self.script = script

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.script:
            self.script(command)
        return subprocess.CompletedProcess(command, 0, stdout="假 main.py stop：已处理\n", stderr="")


def make_settings(tmp: Path) -> Settings:
    (tmp / "sessions").mkdir(parents=True, exist_ok=True)
    s = load_settings(ROOT / "config.toml")
    s.app.project_root = str(tmp)
    return s


def patch(monkey: dict):
    """装上假实现，返回还原函数。"""
    originals = {name: getattr(sc, name) for name in monkey}
    for name, value in monkey.items():
        setattr(sc, name, value)
    return lambda: [setattr(sc, name, value) for name, value in originals.items()]


def section_1(tmp: Path) -> None:
    print("\n[1] 什么都没在跑（没有 pid 文件，日志里只有旧 pid）→ 就该说「没在跑」")
    s = make_settings(tmp / "a")
    gone = dead_pid()
    (s.root / "sessions" / "listen.log").write_text(
        f"12:00:00 I voice_loop | 服务已启动 PID={gone}\n", encoding="utf-8")
    st = sc.status(s)
    check("running = False", st.running, False)
    check("★ pid 是 None（不拿死 pid 骗人）★", st.pid is None, True, detail=str(st.pid))
    check("★ pid_stale = False（没有 pid 文件）★", st.pid_stale, False)
    check("没有残留", st.as_dict()["leftovers"], False)


def section_2(tmp: Path) -> None:
    print("\n[2] 有 pid 文件但进程早没了 → 算「残留」，且 pid 仍是 None")
    s = make_settings(tmp / "b")
    gone = dead_pid()
    (s.root / "sessions" / "listen.pid").write_text(str(gone), encoding="utf-8")
    st = sc.status(s)
    check("running = False", st.running, False)
    check("pid 是 None", st.pid is None, True, detail=str(st.pid))
    check("pid_stale = True（需要清理）", st.pid_stale, True)
    check("pid_file_exists = True", st.pid_file_exists, True)
    check("leftovers = True", st.as_dict()["leftovers"], True)


def section_3(tmp: Path) -> None:
    print("\n[3] ★有残留时点启动：先自动清理，再启动★（以前是让用户自己点停止）")
    s = make_settings(tmp / "c")
    (s.root / "sessions" / "listen.pid").write_text(str(dead_pid()), encoding="utf-8")
    (s.root / "sessions" / "listen.stop").write_text("stop", encoding="utf-8")
    spawned: list[list[str]] = []

    def fake_spawn(command, cwd):
        spawned.append(list(command))
        # 模仿 `main.py listen -B`：马上写出 pid 文件（用一个活着的 pid）
        import os

        (s.root / "sessions" / "listen.pid").write_text(str(os.getpid()), encoding="utf-8")
        (s.root / "sessions" / "listen.stop").unlink(missing_ok=True)
        return 99999

    def script(command):        # 假 `main.py stop`：把残留文件清掉
        (s.root / "sessions" / "listen.pid").unlink(missing_ok=True)
        (s.root / "sessions" / "listen.stop").unlink(missing_ok=True)

    run = FakeRun(script)
    restore = patch({"spawn": fake_spawn, "subprocess": _FakeSubprocess(run)})
    try:
        ok, message = sc.start_service(s, wait_seconds=3.0)
    finally:
        restore()
    check("启动成功（没被残留挡住）", ok, True)
    check("★确实先调了清理（main.py stop）★", len(run.calls), 1)
    check("清理用的是 stop 命令", "stop" in run.calls[0], True, detail=str(run.calls[0][-1:]))
    check("然后才 spawn", len(spawned), 1)
    check("消息里说明了清理过", "清理" in message, True, detail=message)
    check("消息里报的是真起来的 PID", str(__import__("os").getpid()) in message, True, detail=message)


def section_4(tmp: Path) -> None:
    print("\n[4] 已经在跑 → 拒绝启动，并说清 PID 来源")
    s = make_settings(tmp / "d")
    import os

    (s.root / "sessions" / "listen.pid").write_text(str(os.getpid()), encoding="utf-8")
    spawned: list[list[str]] = []
    restore = patch({"spawn": lambda command, cwd: spawned.append(list(command)) or 1})
    try:
        ok, message = sc.start_service(s, wait_seconds=1.0)
    finally:
        restore()
    check("拒绝启动", ok, False)
    check("没去 spawn", spawned, [])
    check("说了已经在运行", "已经在运行" in message, True, detail=message)
    check("说了 PID 来源", "pid 文件" in message, True, detail=message)


def section_5(tmp: Path) -> None:
    print("\n[5] ★起了但没起来 → 必须报失败 + 带上日志尾行★（不能盲目说已启动）")
    s = make_settings(tmp / "e")
    (s.root / "sessions" / "listen.log").write_text(
        "12:00:00 I voice_loop | 缺模型：models/asr/sensevoice-small\n", encoding="utf-8")
    run = FakeRun()
    restore = patch({
        "spawn": lambda command, cwd: 12345,      # 假装发出去了，但什么都没发生
        "subprocess": _FakeSubprocess(run),
    })
    try:
        ok, message = sc.start_service(s, wait_seconds=1.2)
    finally:
        restore()
    check("报失败", ok, False)
    check("消息里带上了日志原因", "缺模型" in message, True, detail=message)
    check("说明了等多久", "1.2 秒" in message, True, detail=message)


def section_6(tmp: Path) -> None:
    print("\n[6] 停止：没在跑要说清楚；停完要验证")
    s = make_settings(tmp / "f")
    run = FakeRun()
    restore = patch({"subprocess": _FakeSubprocess(run)})
    try:
        ok, message = sc.stop_service(s, timeout=1.0)
        check("没在跑 → 失败但说明白", ok, False)
        check("没有瞎调 main.py stop", run.calls, [])
        check("消息里说了没在跑", "没有在运行" in message, True, detail=message)

        # 真在跑：假 stop 把 pid 文件删掉 → 验证通过
        import os

        (s.root / "sessions" / "listen.pid").write_text(str(os.getpid()), encoding="utf-8")

        def script(command):
            (s.root / "sessions" / "listen.pid").unlink(missing_ok=True)

        run2 = FakeRun(script)
        patch_restore = patch({"subprocess": _FakeSubprocess(run2)})
        try:
            ok, message = sc.stop_service(s, timeout=2.0)
        finally:
            patch_restore()
        check("在跑 → 停止成功", ok, True)
        check("调了 main.py stop", len(run2.calls), 1)
        check("消息说已停止", "已停止" in message, True, detail=message)

        # 命令返回了但服务还在跑（卡住）→ 必须判失败
        (s.root / "sessions" / "listen.pid").write_text(str(os.getpid()), encoding="utf-8")
        run3 = FakeRun()          # 剧本：什么都不做 = 服务还在
        patch_restore = patch({"subprocess": _FakeSubprocess(run3)})
        try:
            ok, message = sc.stop_service(s, timeout=0.8)
        finally:
            patch_restore()
        check("★命令返回了但服务还在 → 判失败★", ok, False)
        check("消息里说还在跑", "还在跑" in message, True, detail=message)
    finally:
        restore()


def section_7(tmp: Path) -> None:
    print("\n[7] 起服务用的是 pythonw（无窗口）+ 绝对路径 main.py")
    s = make_settings(tmp / "g")
    cmd = sc.start_command(s)
    check("第一段是 pythonw.exe", Path(cmd[0]).name.lower() == "pythonw.exe", True, detail=cmd[0])
    check("第二段是 main.py 且存在", Path(cmd[1]).is_file(), True, detail=cmd[1])
    check("参数是 listen -B", cmd[2:], ["listen", "-B"])
    stop = sc.stop_command()
    check("停止用普通 python.exe", Path(stop[0]).name.lower().startswith("python"), True, detail=stop[0])
    check("停止参数是 stop", stop[2:], ["stop"])


class _FakeSubprocess:
    """替掉 service_ctl 里的 subprocess 模块（只用到 run 与异常类）。"""

    def __init__(self, runner) -> None:
        self.run = runner
        self.TimeoutExpired = subprocess.TimeoutExpired
        self.PIPE = subprocess.PIPE
        self.DEVNULL = subprocess.DEVNULL
        self.DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
        self.CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def main() -> int:
    print("=" * 70)
    print(" 服务启停逻辑自测（离线；不会真的起服务）")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        section_1(tmp)
        section_2(tmp)
        section_3(tmp)
        section_4(tmp)
        section_5(tmp)
        section_6(tmp)
        section_7(tmp)
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
