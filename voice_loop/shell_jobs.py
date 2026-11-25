"""后台 shell 作业（对齐 DSH 的 ``ctx.jobs`` + ``ctx.shell.start``）。

前台 ``run_command`` 用 :func:`voice_loop.proc.run_hidden` 一次跑完；后台作业
用这里的 :class:`ShellJobManager`：命令立即返回 job id，模型随后用
``job_output`` / ``job_list`` / ``job_kill`` 收集结果。

和 DSH 一致的几条语义：

- 后台进程**没有 executor timeout**；要停就 ``job_kill``。
- ``readOutput`` 是消费型的：连续读不会重复同一段。
- 内存里只留尾部；完整 stdout/stderr 落到私有 spill 文件，截断时报告路径。
- ``done`` 永不因命令失败而 reject；非零退出、信号、spawn 失败都是"结果"。
"""
from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .proc import hidden_creationflags, popen_hidden

_TAIL_CAP = 128 * 1024          # 内存里保留多少字符的尾部
_MAX_JOBS = 64                  # 防止模型把作业开到天荒地老
_MAX_IDLE_SECONDS = 3600        # 已结束作业保留一小时


@dataclass
class ShellJob:
    id: str
    command: str
    workdir: str
    started_at: float
    proc: subprocess.Popen
    spill_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    stdout_tail: deque[str] = field(default_factory=lambda: deque(maxlen=4096))
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=4096))
    stdout_seen: int = 0
    stderr_seen: int = 0
    lossy: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    signal: str | None = None
    killed: bool = False
    spawn_error: str = ""
    tail_cap: int = _TAIL_CAP

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    def _spill(self, stream: str) -> Path:
        return self.spill_dir / f"{self.id}-{stream}.log"

    def _pump(self, pipe, stream: str) -> None:
        target = self._spill(stream)
        try:
            with pipe, target.open("a", encoding="utf-8", errors="replace") as fh:
                while True:
                    chunk = pipe.readline()
                    if not chunk:
                        break
                    fh.write(chunk)
                    fh.flush()
                    with self.lock:
                        self.stdout_tail.append(chunk) if stream == "stdout" \
                            else self.stderr_tail.append(chunk)
        except Exception as exc:  # noqa: BLE001 - 读管道失败也要让主流程收口
            with self.lock:
                self.spawn_error = f"{type(exc).__name__}: {exc}"

    def _settle(self) -> None:
        try:
            code = self.proc.wait()
            self.exit_code = code if code >= 0 else None
            if code < 0:
                self.signal = f"SIG{-code}"
        except Exception as exc:  # noqa: BLE001
            self.spawn_error = f"{type(exc).__name__}: {exc}"
            self.signal = "spawn-failed"
        self.done.set()

    def read(self, *, wait: bool = False, timeout_ms: int = 0) -> dict:
        if wait:
            self.done.wait(max(0.0, timeout_ms / 1000.0) if timeout_ms else None)
        with self.lock:
            so = "".join(self.stdout_tail)[self.stdout_seen:]
            se = "".join(self.stderr_tail)[self.stderr_seen:]
            self.stdout_seen += len(so)
            self.stderr_seen += len(se)
            if len(so) > self.tail_cap:
                self.lossy = True
                so = so[-self.tail_cap:]
            if len(se) > self.tail_cap:
                self.lossy = True
                se = se[-self.tail_cap:]
        return {
            "job_id": self.id,
            "running": self.running,
            "stdout": so,
            "stderr": se,
            "lossy": self.lossy,
            "stdout_spill": str(self._spill("stdout")) if self.lossy else "",
            "stderr_spill": str(self._spill("stderr")) if self.lossy else "",
            "exit_code": self.exit_code,
            "signal": self.signal,
            "killed": self.killed,
            "error": self.spawn_error,
        }

    def kill(self) -> bool:
        if not self.running:
            return False
        try:
            if os.name == "nt":
                self.proc.kill()
            else:
                os.killpg(self.proc.pid, 9)
        except Exception:  # noqa: BLE001 - 已经结束就是 no-op
            pass
        self.killed = True
        return True


class ShellJobManager:
    """进程内作业表；不跨重启，不暴露给网络。"""

    def __init__(self, spill_dir: str | Path, *, tail_cap: int = _TAIL_CAP) -> None:
        self.spill_dir = Path(spill_dir)
        self.tail_cap = max(1024, int(tail_cap))
        self.jobs: dict[str, ShellJob] = {}
        self.lock = threading.Lock()
        atexit.register(self.shutdown)

    def start(self, command: str, workdir: str | Path, env: dict[str, str]) -> ShellJob:
        self._prune()
        if len(self.jobs) >= _MAX_JOBS:
            raise RuntimeError(f"后台作业已达上限 {_MAX_JOBS}，先 job_kill 几个")
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        jid = uuid.uuid4().hex[:12]
        flags = hidden_creationflags(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                     if os.name == "nt" else 0)
        kwargs: dict[str, Any] = {
            "cwd": str(workdir), "env": env, "shell": True,
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "errors": "replace", "bufsize": 1,
        }
        if os.name == "nt":
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        proc = popen_hidden(command, **kwargs)
        job = ShellJob(id=jid, command=command, workdir=str(workdir),
                       started_at=time.time(), proc=proc, spill_dir=self.spill_dir,
                       tail_cap=self.tail_cap)
        with self.lock:
            self.jobs[jid] = job
        for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(target=job._pump, args=(pipe, stream), daemon=True).start()
        threading.Thread(target=job._settle, daemon=True).start()
        return job

    def get(self, jid: str) -> ShellJob | None:
        return self.jobs.get(str(jid or "").strip())

    def list(self) -> list[dict]:
        self._prune()
        return [{
            "job_id": j.id, "command": j.command, "running": j.running,
            "exit_code": j.exit_code, "signal": j.signal,
            "elapsed": round(time.time() - j.started_at, 1),
        } for j in sorted(self.jobs.values(), key=lambda x: x.started_at)]

    def kill(self, jid: str) -> bool:
        job = self.get(jid)
        return bool(job and job.kill())

    def _prune(self) -> None:
        now = time.time()
        for jid, job in list(self.jobs.items()):
            if not job.running and now - job.started_at > _MAX_IDLE_SECONDS:
                self.jobs.pop(jid, None)

    def shutdown(self) -> None:
        for job in list(self.jobs.values()):
            if job.running:
                job.kill()
        for job in self.jobs.values():
            job.done.wait(2.0)


_MANAGERS: dict[str, ShellJobManager] = {}
_MANAGER_LOCK = threading.Lock()


def manager_for(settings: Any) -> ShellJobManager:
    """同一份 settings 共用一个作业表（不同 root 互不串）。"""
    root = str(getattr(settings, "root", ".") or ".")
    with _MANAGER_LOCK:
        got = _MANAGERS.get(root)
        if got is None:
            got = ShellJobManager(Path(root) / "sessions" / "agent_shell")
            _MANAGERS[root] = got
        return got


__all__ = ["ShellJob", "ShellJobManager", "manager_for"]
