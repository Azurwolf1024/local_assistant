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
    """给界面看的服务状态快照（只读事实，不含判断）。"""

    running: bool
    pid: int | None
    pid_stale: bool          # 有 pid 文件但进程已经没了（需要清理）
    from_log: bool           # pid 是从日志里恢复出来的（pid 文件丢了）
    pid_file: Path
    stop_file: Path
    log_file: Path
    log_pending: bool        # 有停止信号文件还没被服务吃掉
    log_size: int
    log_mtime: float

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "pid_stale": self.pid_stale,
            "from_log": self.from_log,
            "pid_file": str(self.pid_file),
            "log_file": str(self.log_file),
            "log_pending_stop": self.log_pending,
            "log_size": self.log_size,
            "log_age_seconds": (
                max(0.0, time.time() - self.log_mtime) if self.log_mtime else None
            ),
        }


def status(settings: Settings) -> ServiceStatus:
    """看看唤醒服务现在是什么情况（**绝不修改任何文件**，UI 会频繁调用）。"""
    pid_file = settings.resolve(settings.wake.pid_file)
    stop_file = settings.resolve(settings.wake.stop_file)
    log_file = settings.resolve(settings.wake.log_file)

    has_file = pid_file.exists()
    pid = read_pid(settings)
    alive = bool(pid) and pid_alive(int(pid))
    try:
        stat = log_file.stat()
        log_size, log_mtime = stat.st_size, stat.st_mtime
    except OSError:
        log_size, log_mtime = 0, 0.0
    return ServiceStatus(
        running=alive,
        pid=int(pid) if pid else None,
        pid_stale=bool(pid) and not alive,
        from_log=alive and not has_file,
        pid_file=pid_file,
        stop_file=stop_file,
        log_file=log_file,
        log_pending=stop_file.exists(),
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


def start_service(settings: Settings) -> tuple[bool, str]:
    """起后台唤醒服务。返回 (是否已发出启动, 给用户看的一句话)。

    ★不写 pid 文件★：那是服务自己的事（`main.py cmd_listen --background` 里做），
    这里替它写会和单实例守卫打架。
    """
    st = status(settings)
    if st.running:
        return False, f"服务已经在运行（PID {st.pid}）"
    if st.pid_stale:
        return False, f"pid 文件里是旧进程（{st.pid}，已经没了）——先点「停止」清理一下"
    if st.log_pending:
        return False, "还有没被处理的停止信号（sessions/listen.stop），等服务退出后再启动"
    try:
        pid = spawn(start_command(settings), settings.root)
    except OSError as exc:
        return False, f"启动失败：{exc}"
    return True, f"已发出启动命令（pythonw PID {pid}）——服务加载模型要几秒，稍等"


def stop_service(settings: Settings, *, timeout: float = 20.0) -> tuple[bool, str]:
    """停服务：交给 `main.py stop` 干（优雅退出 → 超时强杀 → 清 pid/stop 文件）。"""
    st = status(settings)
    if not st.running and not st.pid_stale and not st.log_pending:
        return False, "服务没有在运行"
    if not st.running and st.pid_stale:
        # 服务早就没了，只是 pid/stop 文件留着：`main.py stop` 会顺手清理
        pass
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
    return proc.returncode == 0, (tail[-1] if tail else f"main.py stop 退出码 {proc.returncode}")
