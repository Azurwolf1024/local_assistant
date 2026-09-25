"""唤醒服务的「进程级」操作：查状态、起、停。

为什么单独一个模块：控制台 UI 是**另一个进程**，它要显示「服务在不在跑」并能点击启动/停止。
这份逻辑以前只在 `main.py` 里，UI 要复用就得 `import main`，而那样会连带加载
numpy / sounddevice（控制台不需要音频设备，也没资格碰它们）。所以把它下沉到这里，
`main.py` 反过来 import 本模块——**一份实现，两处使用**，不至分叉。

★约定（UI 绝不能违反，否则会把正在说话的服务弄哑）★：
    - ``sessions/listen.pid`` 只有服务自己的生命周期代码能删（见 pipeline.close）；
      外部要停服务就调 :func:`stop_service`，别自己删 pid 文件。
    - ``sessions/listen.stop`` 是停止信号：只写这一个字节串「stop」，服务轮询到就退出。
    - ``sessions/listen.log`` 由服务独占写（``python main.py listen`` 会把 stdout 重定向进去）。
      别的进程**只读**，不能追加，否则日志会交错乱掉。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import Settings

PYTHON_EXE = Path(sys.executable)
"""普通解释器（控制台用；起服务用 pythonw 才没窗口，见 :func:`start_command`）。"""


def pid_alive(pid: int) -> bool:
    """进程还在不在。Windows 用 OpenProcess/GetExitCodeProcess（不用 tasklist，太慢）。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_from_log(settings: Settings) -> int | None:
    """pid 文件被误删时的兜底：日志里最后一条「PID=xxxx」。"""
    log_file = settings.resolve(settings.wake.log_file)
    if not log_file.exists():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"PID=(\d+)", text)
    return int(matches[-1]) if matches else None


def read_pid(settings: Settings, *, recover: bool = True) -> int | None:
    """读 pid 文件里记的 PID；读不到（或坏了）按需从日志恢复。"""
    pid_file = settings.resolve(settings.wake.pid_file)
    if pid_file.exists():
        try:
            value = int(pid_file.read_text(encoding="utf-8").strip())
            return value or None
        except (OSError, ValueError):
            return None
    return pid_from_log(settings) if recover else None


@dataclass(frozen=True)
class ServiceStatus:
    """给界面看的服务状态快照（只读事实，不含判断）。

    ★三件事必须分清（UI 的分支全建在这上面）★：

        running       进程真的还在（pid 文件，或从日志恢复出来的那个 pid，验过）
        pid          **正在跑的那个进程号**；没在跑就是 None（不再拿死 pid 骗人）
        pid_stale     pid 文件存在但里面的进程已经没了（= 需要清理的残留）

    踩过的坑：以前把「从日志恢复出来的旧 pid」也算进 ``pid_stale``，于是
    「pid 文件根本没有、服务也没在跑」这种最常见的情况会被报成
    「pid 文件里是旧进程 —— 先点停止」，用户点启动被拒、点停止又说没东西可停，
    看起来就像「启停功能坏了」。★死 pid 不该被当成状态，它只是历史。★
    """

    running: bool
    pid: int | None
    pid_source: str          # "pid_file" / "log" / ""
    pid_file_exists: bool
    pid_stale: bool
    log_pending: bool        # 有停止信号文件还没被服务吃掉
    pid_file: Path
    stop_file: Path
    log_file: Path
    log_size: int
    log_mtime: float

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "pid_source": self.pid_source,
            "pid_file_exists": self.pid_file_exists,
            "pid_stale": self.pid_stale,
            "from_log": self.pid_source == "log",
            "pid_file": str(self.pid_file),
            "stop_file": str(self.stop_file),
            "log_file": str(self.log_file),
            "log_pending_stop": self.log_pending,
            "log_size": self.log_size,
            "log_age_seconds": (
                max(0.0, time.time() - self.log_mtime) if self.log_mtime else None
            ),
            "leftovers": self.pid_stale or self.log_pending,
        }


def _pid_in_file(pid_file: Path) -> int | None:
    try:
        value = int(pid_file.read_text(encoding="utf-8").strip())
        return value or None
    except (OSError, ValueError):
        return None


def status(settings: Settings) -> ServiceStatus:
    """看看唤醒服务现在是什么情况（**绝不修改任何文件**，UI 会频繁调用）。"""
    pid_file = settings.resolve(settings.wake.pid_file)
    stop_file = settings.resolve(settings.wake.stop_file)
    log_file = settings.resolve(settings.wake.log_file)

    pid_file_exists = pid_file.exists()
    file_pid = _pid_in_file(pid_file)
    # 记录在案的先看；pid 文件被误删时再从日志恢复（服务自己的 `stop` 也是这么找的）
    cand = file_pid if (file_pid and pid_alive(file_pid)) else None
    source = "pid_file" if cand else ""
    if cand is None:
        recovered = pid_from_log(settings)
        if recovered and pid_alive(recovered):
            cand, source = recovered, "log"

    try:
        stat = log_file.stat()
        log_size, log_mtime = stat.st_size, stat.st_mtime
    except OSError:
        log_size, log_mtime = 0, 0.0
    return ServiceStatus(
        running=cand is not None,
        pid=cand,
        pid_source=source,
        pid_file_exists=pid_file_exists,
        pid_stale=pid_file_exists and cand is None,
        log_pending=stop_file.exists(),
        pid_file=pid_file,
        stop_file=stop_file,
        log_file=log_file,
        log_size=log_size,
        log_mtime=log_mtime,
    )


def start_command(settings: Settings, *, console: bool = False) -> list[str]:
    """起服务的命令行。

    ``console=False``（默认）用 **pythonw.exe**：没有控制台窗口，不会被关窗口时的
    CTRL 事件带走。控制台 UI 点「启动」走的就是这条。
    """
    exe = PYTHON_EXE if console else PYTHON_EXE.with_name("pythonw.exe")
    if not exe.exists():
        exe = PYTHON_EXE
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    return [str(exe), str(main_py), "listen", "-B"]


def stop_command() -> list[str]:
    """停服务的命令行（走 `main.py stop`：带优雅退出 + 超时强杀，逻辑不在这里重写）。"""
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    return [str(PYTHON_EXE), str(main_py), "stop"]


def spawn(command: list[str], cwd: Path) -> int:
    """起一个**脱离当前终端**的进程，返回 PID。

    跟 `main.py listen -B` 一个道理：Windows 用 DETACHED_PROCESS + 新进程组
    （不然关掉控制台窗口会把服务一起带走），POSIX 用 start_new_session。
    """
    flags = 0
    kwargs: dict = {}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(cwd),
        creationflags=flags,
        close_fds=True,
        **kwargs,
    )
    return int(proc.pid)


def tail_lines(settings: Settings, count: int = 3) -> list[str]:
    """日志最后几行（失败时把原因带出来用）。**只读**——日志由服务独占写。"""
    log_file = settings.resolve(settings.wake.log_file)
    if not log_file.exists():
        return []
    try:
        size = log_file.stat().st_size
        with log_file.open("rb") as fh:
            fh.seek(max(0, size - 8192))
            data = fh.read()
    except OSError:
        return []
    rows = [r for r in data.decode("utf-8", errors="replace").splitlines() if r.strip()]
    return rows[-max(1, count):]


def wait_for(settings: Settings, predicate, *, timeout: float, interval: float = 0.3) -> ServiceStatus:
    """轮询直到 ``predicate(status)`` 为真或超时，返回最后那次状态。

    ★为什么要等★：启动/停止都是「发了命令」，**不等于做成了**。
    以前回一句「已发出启动命令」就算成功，服务其实没起来（比如缺模型）时
    界面会说「已启动」，看起来就是「启停坏了」（踩过）。
    """
    deadline = time.time() + max(0.1, float(timeout))
    while True:
        st = status(settings)
        if predicate(st) or time.time() >= deadline:
            return st
        time.sleep(interval)


def _clean_leftovers(settings: Settings) -> str:
    """清掉上一轮留下的 pid/stop 文件（★只在确认没在跑的时候调★）。

    为什么绕一圈调 `main.py stop`：它就是这个用途写的（判是否真死 → 清文件），
    在这里重写一份清理逻辑只会多一处会跑偏的地方。
    """
    try:
        proc = subprocess.run(
            stop_command(),
            cwd=str(settings.root),
            capture_output=True,
            text=True,
            timeout=20.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"清理残留失败：{type(exc).__name__}"
    tail = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return tail[-1] if tail else f"main.py stop 退出码 {proc.returncode}"


def start_service(
    settings: Settings, *, wait_seconds: float = 25.0
) -> tuple[bool, str]:
    """起后台唤醒服务。返回 (真的起来了吗, 给用户看的一句话)。

    ★不写 pid 文件★：那是服务自己的事（`main.py cmd_listen --background` 里做），
    这里替它写会和单实例守卫打架。

    三种情况都在这里收尾：① 已经在跑 → 直接说；② 有残留（pid 文件/停止信号）→
    **先清干净再启**（以前是让用户自己点「停止」，于是点启动→被拒→点停止→
    「没有在运行」，看起来就是坏了）；③ 起了但没起来 → 回一句失败 + 日志尾行。
    """
    st = status(settings)
    if st.running:
        return False, f"服务已经在运行（PID {st.pid}，来自{_source_text(st)}）"

    note = ""
    if st.pid_stale or st.log_pending:
        # 确认过没在跑，所以这里的残留文件可以安心清掉
        note = f"（已清理上次的残留文件：{_clean_leftovers(settings)}）"

    try:
        spawn_pid = spawn(start_command(settings), settings.root)
    except OSError as exc:
        return False, f"启动失败：{exc}{note}"

    st2 = wait_for(settings, lambda s: s.running, timeout=wait_seconds)
    if st2.running:
        return True, f"服务已启动（PID {st2.pid}，来自{_source_text(st2)}）{note}"
    rows = tail_lines(settings, 2)
    why = f"日志最后一行：{rows[-1]}" if rows else "日志还是空的（服务可能连启动都没走到）"
    return False, (
        f"启动命令发出去了（pythonw 进程 {spawn_pid}），但 {wait_seconds:g} 秒内没看到服务起来。\n"
        f"{why}{note}"
    )


def stop_service(settings: Settings, *, timeout: float = 20.0) -> tuple[bool, str]:
    """停服务：交给 `main.py stop` 干（优雅退出 → 超时强杀 → 清 pid/stop 文件）。

    ★事后验证★：命令返回 0 不代表服务真没了（可能刚好卡住），所以再等一会儿
    看到「没在跑且没残留」才算成。
    """
    st = status(settings)
    if not st.running and not st.pid_stale and not st.log_pending:
        return False, "服务没有在运行（也没有残留文件）"

    was_running = st.running
    try:
        proc = subprocess.run(
            stop_command(),
            cwd=str(settings.root),
            capture_output=True,
            text=True,
            timeout=timeout + 15.0,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "停止超时（main.py stop 没有在预期时间内返回）"
    except OSError as exc:
        return False, f"停止失败：{exc}"

    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    message = tail[-1] if tail else f"main.py stop 退出码 {proc.returncode}"

    st2 = wait_for(
        settings,
        lambda s: not s.running and not s.pid_stale and not s.log_pending,
        timeout=timeout,
    )
    if st2.running:
        return False, f"命令执行了，但服务还在跑（PID {st2.pid}）。{message}"
    if st2.pid_stale or st2.log_pending:
        return False, f"服务进程没了，但残留文件还在（{message}）"
    if not was_running:
        return True, f"服务本来就没在运行，已清理残留文件（{message}）"
    return True, f"服务已停止（{message}）"


def _source_text(st: ServiceStatus) -> str:
    return {"pid_file": "pid 文件", "log": "日志记录（pid 文件丢了）"}.get(st.pid_source, "未知来源")
