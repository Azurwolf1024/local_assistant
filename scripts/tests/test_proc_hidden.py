"""无窗口子进程自测：从 pythonw 服务里调控制台程序不许再闪 cmd 黑框。

两条检查
    1. 行为：hidden_creationflags 注入正确；run_hidden / popen_hidden 的输出、
       返回码跟原生 subprocess 一样（只是少了那个窗口）。
    2. 防回归：用 AST 扫 voice_loop/ 与 main.py —— 直接调 ``subprocess.run``
       或 ``subprocess.Popen`` 却没传 creationflags 的一律报出来（docstring 里
       提到不算，所以不用 grep）。GUI 程序（Edge、xdg-open）本来就没有控制台
       窗口，列在豁免里。

    python scripts\\tests\\test_proc_hidden.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = "√", "×"
_failures: list[str] = []

# 本来就没有控制台窗口的 GUI 启动器 + 工具自己（它当然要调原生 subprocess）
EXEMPT = {
    "voice_loop/proc.py": "工具本身",
    "voice_loop/console/window.py": "启动 msedge.exe（GUI）",
    "voice_loop/tray.py": "xdg-open / open（非 Windows 分支的 GUI 启动器）",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"：{detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "subprocess":
            return func.attr
    return ""


def scan_sources() -> list[str]:
    """返回「裸调 subprocess.run/Popen 且没隐藏窗口」的 文件:行 列表。"""
    targets = [ROOT / "main.py", *(ROOT / "voice_loop").rglob("*.py")]
    bad: list[str] = []
    for path in sorted(targets):
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text.lstrip("\ufeff"))  # main.py 带 BOM
        except SyntaxError as exc:
            bad.append(f"{rel}: 解析失败（{exc}）")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee(node) not in {"run", "Popen"}:
                continue
            if any(kw.arg == "creationflags" for kw in node.keywords):
                continue  # 自己已经处理过了（例如起后台服务的 DETACHED_PROCESS）
            bad.append(f"{rel}:{node.lineno} 裸调 subprocess.{_callee(node)}")
    return bad


def main() -> int:
    from voice_loop.proc import hidden_creationflags, popen_hidden, run_hidden

    print("[1] 行为")
    flags = hidden_creationflags(0)
    if os.name == "nt":
        check("Windows 注入 CREATE_NO_WINDOW",
              bool(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0)), f"flags={flags}")
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        if detached:
            check("不覆盖已有的 DETACHED_PROCESS（两者互斥）",
                  hidden_creationflags(detached) == detached)
    else:
        check("非 Windows 保持原样", flags == 0, f"flags={flags}")

    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        alt = py.with_name("python.exe")
        if alt.exists():
            py = alt
    try:
        got = run_hidden([str(py), "-c", "print('hidden-ok')"],
                         capture_output=True, text=True, timeout=30)
        check("run_hidden 输出捕获正常",
              got.returncode == 0 and "hidden-ok" in (got.stdout or ""),
              f"rc={got.returncode} out={(got.stdout or '').strip()!r}")
    except Exception as exc:  # noqa: BLE001
        check("run_hidden 输出捕获正常", False, f"{type(exc).__name__}: {exc}")

    try:
        proc = popen_hidden([str(py), "-c", "pass"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("popen_hidden 正常返回", proc.wait(timeout=30) == 0)
    except Exception as exc:  # noqa: BLE001
        check("popen_hidden 正常返回", False, f"{type(exc).__name__}: {exc}")

    print("[2] 防回归：没有漏掉裸调用")
    bad = scan_sources()
    check("所有控制台子进程都走了 run_hidden / popen_hidden",
          not bad, "；".join(bad) if bad else "")

    print("\n" + ("全部通过" if not _failures else f"失败 {len(_failures)} 项：{_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
