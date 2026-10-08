"""项目根 / 自带资源 / Python 解释器 —— ★「路径从哪来」的判断只在这一个文件里★。

为什么需要它：控制台能冻成一个 exe（``scripts/make_console_exe.py``）。冻结之后
``__file__`` 指向 PyInstaller 的解包临时目录（``C:\\...\\Temp\\_MEIxxxx``，
**每次启动都不一样**），照它推「项目在哪」会把 ``config.toml``、``data/``、``models/``
全推到临时目录里去 —— 表现是「exe 起来说找不到配置文件」，而配置明明就在旁边。

三条规则（对应三种东西，别混）：

=================  ==================================  ==========================
``app_root()``     **用户的东西**：config.toml、data/、  exe 所在目录（冻结时）
                   models/、sessions/                   源码根（源码运行时）
``resource_root()``**随程序走的东西**：static/、panels/、  解包目录 ``_MEIPASS``
                   VERSION、内置模板
``find_python()``  **跑服务的解释器**：exe 里没有解释器，  .venv → venv → PATH
                   真跑语音服务得找到系统里的 Python
=================  ==================================  ==========================

覆盖方式（都不改代码）：``LOCAL_AI_ROOT`` 指项目根、``LOCAL_AI_PYTHON`` 指解释器。
两个环境变量都是为了让**测试**和**exe**互不干扰（自测全程在临时目录里跑）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ENV_ROOT = "LOCAL_AI_ROOT"
"""项目根覆盖（等价于命令行 ``--root``）。"""

ENV_PYTHON = "LOCAL_AI_PYTHON"
"""解释器覆盖：控制台 exe 拿它决定用哪个 Python 去跑服务。"""


def is_frozen() -> bool:
    """是不是冻成 exe 了（PyInstaller 会设 ``sys.frozen``）。"""
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path | None:
    """冻结时的 exe 本身（源码运行时是 None）。"""
    return Path(sys.executable).resolve() if is_frozen() else None


def exe_dir() -> Path | None:
    """exe 所在目录 —— 冻结时它就是「用户的项目根」（典型布局：exe 就在项目根里）。"""
    exe = exe_path()
    return exe.parent if exe else None


def source_root() -> Path:
    """源码根（``voice_loop/`` 的上一层）。冻结时指向解包目录，不是用户的项目。"""
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """随程序走的只读资源在哪：冻结时是解包目录，源码运行时就是源码根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass).resolve() if meipass else source_root()


def looks_like_project(path: str | Path) -> bool:
    """像个项目根吗（``config.toml`` + ``main.py`` 都在才算 —— 只看一个会误判）。"""
    base = Path(path)
    return (base / "config.toml").is_file() and (base / "main.py").is_file()


def find_project_root(start: str | Path | None = None, *, levels: int = 4) -> Path | None:
    """从 ``start`` 开始往上找项目根，找不到就 None。

    ★为什么 exe 需要这个★：双击运行时没有命令行参数，而 onedir 的布局是
    ``<项目>/console/local-assistant-console.exe`` —— exe 自己那一层没有 config.toml，
    上一层才有。不往上找的话，用户双击得到的是「找不到配置文件」，而配置就在旁边。
    """
    base = Path(start).expanduser().resolve() if start else (exe_dir() or Path.cwd())
    for _ in range(max(1, levels)):
        if looks_like_project(base):
            return base
        if base.parent == base:
            break
        base = base.parent
    return None


def app_root() -> Path:
    """项目根：``LOCAL_AI_ROOT`` > exe 附近能找到的项目 > exe 所在目录 > 源码根。

    ★顺序是有意为之★：环境变量最优先（测试、便携版、把 exe 放到别处都用它），
    其次是「exe 旁边/附近」——那是用户双击时的直觉位置。
    """
    override = os.environ.get(ENV_ROOT, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    found = exe_dir()
    if found is None:
        return source_root()
    return find_project_root(found) or found



def set_app_root(path: str | Path) -> Path:
    """改项目根（进程内）。给 exe 入口与测试用；★不写盘、只改这个进程★。"""
    resolved = Path(path).expanduser().resolve()
    os.environ[ENV_ROOT] = str(resolved)
    return resolved


def python_candidates(root: str | Path | None = None) -> list[Path]:
    """按优先级列出「可能能用来跑服务的 Python」。"""
    root = Path(root) if root else app_root()
    out: list[Path] = []
    override = os.environ.get(ENV_PYTHON, "").strip()
    if override:
        out.append(Path(override).expanduser())
    if os.name == "nt":
        for venv in (".venv", "venv", "env"):
            out.append(root / venv / "Scripts" / "python.exe")
    else:
        for venv in (".venv", "venv", "env"):
            out.append(root / venv / "bin" / "python")
    if not is_frozen() and sys.executable:
        out.append(Path(sys.executable))
    for name in (("python", "python3", "py") if os.name != "nt" else ("python", "py")):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    # 去重（保序）：.venv 与 PATH 可能指到同一个
    seen: set[str] = set()
    uniq: list[Path] = []
    for path in out:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def find_python(root: str | Path | None = None) -> Path | None:
    """挑一个真能跑的解释器；一个都没有就返回 None（调用方负责给出人话提示）。

    ★为什么不能直接用 ``sys.executable``★：冻结时它是控制台 exe 自己 ——
    拿它去跑 ``main.py listen`` 等于让 exe 自己再启动一遍（命令行全被吃掉）。
    这是把控制台做成 exe 之后最容易踩的一脚。
    """
    for path in python_candidates(root):
        try:
            if path.is_file() and path.exists():
                return path
        except OSError:  # pragma: no cover - 权限/网络盘之类的怪路径
            continue
    return None


def describe(root: str | Path | None = None) -> dict:
    """给 ``/api/meta`` 与 ``doctor`` 用的一份快照（出问题先看它）。"""
    exe = exe_path()
    python = find_python(root)
    return {
        "frozen": is_frozen(),
        "exe": str(exe) if exe else "",
        "app_root": str(Path(root) if root else app_root()),
        "resource_root": str(resource_root()),
        "source_root": str(source_root()),
        "python": str(python) if python else "",
        "env": {ENV_ROOT: os.environ.get(ENV_ROOT, ""), ENV_PYTHON: os.environ.get(ENV_PYTHON, "")},
    }
