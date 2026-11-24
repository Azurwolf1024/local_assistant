"""无窗口子进程：挡掉 Windows 上「一闪而过的 cmd 黑框」。

从 pythonw（无控制台）启动的服务 / 托盘 / 控制台后端去调 git、taskkill、
netstat、powershell、cmd 这类**控制台程序**时，Windows 会给子进程新建一个
控制台窗口 —— 就算 stdout/stderr 接进了管道也一样。一次记忆同步要跑十几条
git，于是桌面上就一串黑框乱跳。

统一走 :func:`run_hidden` / :func:`popen_hidden`：Windows 上注入
``CREATE_NO_WINDOW``，其他平台原样。本来就没窗口的 GUI 程序（Edge、
xdg-open）和已经设了 ``DETACHED_PROCESS`` 的后台服务不用改。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_creationflags(existing: int = 0) -> int:
    """在已有 flags 上补 ``CREATE_NO_WINDOW``（仅 Windows）。

    ``CREATE_NO_WINDOW`` 和 ``DETACHED_PROCESS`` 互斥：调用方已经给了后者
    （起后台服务那种）就原样返回，它本身也没有窗口。
    """
    flags = existing or 0
    if os.name != "nt":
        return flags
    if flags & getattr(subprocess, "DETACHED_PROCESS", 0):
        return flags
    return flags | getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_hidden(*args: Any, **kwargs: Any) -> Any:
    """同 :func:`subprocess.run`，只是 Windows 上不弹控制台窗口。"""
    kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags"))
    return subprocess.run(*args, **kwargs)


def popen_hidden(*args: Any, **kwargs: Any) -> Any:
    """同 :func:`subprocess.Popen`，只是 Windows 上不弹控制台窗口。"""
    kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags"))
    return subprocess.Popen(*args, **kwargs)


__all__ = ["hidden_creationflags", "run_hidden", "popen_hidden"]
