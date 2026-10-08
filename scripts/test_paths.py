"""根目录 / 冻结 / 解释器：``voice_loop/paths.py`` 的自测（纯离线、不碰真实文件）。

为什么值得单独一个自测：控制台能冻成 exe（``scripts/make_console_exe.py``）之后，
「项目根从哪来」错了的后果是**看起来完全不相干的现象** ——
exe 双击起来说找不到 config.toml、数据存到临时目录、点「启动服务」起来的是 exe 自己。
这几条在任何真机上都不容易复现（要真去双击），所以在这里用假装的冻结环境钉住。

    python scripts/test_paths.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def child(code: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    """在一个干净的子进程里跑一段代码（改 ``sys.frozen`` 这种事不能在父进程里做）。"""
    full = dict(os.environ)
    full["PYTHONPATH"] = str(ROOT)
    full["PYTHONIOENCODING"] = "utf-8"
    full.pop("LOCAL_AI_ROOT", None)
    full.pop("LOCAL_AI_PYTHON", None)
    if env:
        full.update(env)
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=full)


# --------------------------------------------------------------------------- #
def section_source() -> None:
    print("\n[1] 源码运行：项目根就是源码根")
    from voice_loop import paths

    check("没被误判成冻结", paths.is_frozen(), False)
    check("app_root = 源码根", paths.app_root(), ROOT)
    check("resource_root = 源码根", paths.resource_root(), ROOT)
    check("exe 路径为空（源码运行时没有 exe）", paths.exe_path() is None, True)
    snap = paths.describe()
    check("快照里 frozen=False", snap["frozen"], False)
    check("快照里有解释器", bool(snap["python"]), True, detail=snap["python"])
    check("快照里的 app_root", snap["app_root"], str(ROOT))


def section_env_override() -> None:
    print("\n[2] LOCAL_AI_ROOT 覆盖（导入前设好才生效 —— 这是刻意的）")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "config.toml").write_text("[app]\n", encoding="utf-8")
        (tmp / "main.py").write_text("", encoding="utf-8")
        out = child(
            "from voice_loop.paths import app_root, resource_root;"
            "print(app_root()); print(resource_root())",
            env={"LOCAL_AI_ROOT": str(tmp)},
        )
        lines = [ln.strip() for ln in out.stdout.strip().splitlines() if ln.strip()]
        check("子进程跑通了", out.returncode, 0, detail=out.stderr[-300:])
        check("app_root 跟着环境变量走", lines[:1], [str(tmp)])
        check("★资源目录不受影响★（自带文件仍从源码/解包目录取）", lines[1:2], [str(ROOT)])

        out2 = child(
            "from voice_loop.settings import PROJECT_ROOT; print(PROJECT_ROOT)",
            env={"LOCAL_AI_ROOT": str(tmp)},
        )
        check("settings.PROJECT_ROOT 也认这个变量", out2.stdout.strip(), str(tmp),
              detail=out2.stderr[-200:])


def section_frozen() -> None:
    print("\n[3] 假装冻结：项目根 = 项目目录，资源 = 解包目录")
    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "my-project"
        project.mkdir(parents=True)
        (project / "config.toml").write_text("[app]\n", encoding="utf-8")
        (project / "main.py").write_text("", encoding="utf-8")
        meipass = Path(tmpdir) / "_MEI12345"
        meipass.mkdir()
        (meipass / "VERSION").write_text("7.7.7\n", encoding="utf-8")
        fake_exe = project / "local-assistant-console.exe"
        fake_exe.write_bytes(b"")
        code = (
            "import sys;"
            f"sys.frozen=True; sys._MEIPASS=r'{meipass}'; sys.executable=r'{fake_exe}';"
            "from voice_loop import paths;"
            "print(paths.app_root()); print(paths.resource_root()); print(paths.exe_path());"
            "print(paths.is_frozen())"
        )
        out = child(code)
        lines = [ln.strip() for ln in out.stdout.strip().splitlines() if ln.strip()]
        check("子进程跑通了", out.returncode, 0, detail=out.stderr[-400:])
        check("★app_root 是 exe 旁边的项目★", lines[:1], [str(project)])
        check("★resource_root 是解包目录★", lines[1:2], [str(meipass)])
        check("exe 路径认得出来", lines[2:3], [str(fake_exe)])
        check("is_frozen 为真", lines[3:4], ["True"])

        # onedir 的常见布局：exe 在项目下面的子目录里 → 往上找
        nested = project / "console"
        nested.mkdir()
        nested_exe = nested / "local-assistant-console.exe"
        nested_exe.write_bytes(b"")
        code2 = (
            "import sys;"
            f"sys.frozen=True; sys._MEIPASS=r'{meipass}'; sys.executable=r'{nested_exe}';"
            "from voice_loop import paths;"
            "print(paths.app_root()); print(paths.find_project_root())"
        )
        out2 = child(code2)
        lines2 = [ln.strip() for ln in out2.stdout.strip().splitlines() if ln.strip()]
        check("★exe 在子目录时会往上找到项目★", lines2[:1], [str(project)],
              detail=out2.stderr[-300:])

        code3 = (
            "import sys;"
            f"sys.frozen=True; sys._MEIPASS=r'{meipass}'; sys.executable=r'{Path(tmpdir) / 'elsewhere.exe'}';"
            "from voice_loop import paths;"
            "print(paths.app_root())"
        )
        out3 = child(code3)
        check("附近没项目时退回 exe 目录", out3.stdout.strip(),
              str(Path(tmpdir)), detail=out3.stderr[-200:])


def section_python() -> None:
    print("\n[4] 解释器：.venv → LOCAL_AI_PYTHON → PATH（exe 场景不能拿自己当 Python）")
    from voice_loop.paths import find_python

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        venv_py = tmp / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_bytes(b"")
        check("优先用 .venv 里的", find_python(tmp), venv_py)

        out = child(
            "from voice_loop.paths import find_python; print(find_python())",
            env={"LOCAL_AI_PYTHON": str(venv_py)},
        )
        check("LOCAL_AI_PYTHON 也能指", out.stdout.strip(), str(venv_py),
              detail=out.stderr[-200:])

        # 冻结 + 没有 venv + PATH 是空的 → 诚实地返回 None（调用方给提示）
        code = (
            "import os, sys;"
            "sys.frozen=True; sys._MEIPASS=r'C:\\nope'; sys.executable=r'C:\\nope\\app.exe';"
            "os.environ['PATH']=''; os.environ.pop('LOCAL_AI_PYTHON', None);"
            "from voice_loop.paths import find_python; print(repr(find_python()))"
        )
        out2 = child(code, env={"PATH": ""})
        check("找不到就返回 None（不瞎猜）", out2.stdout.strip(), "None",
              detail=out2.stderr[-300:])

        # ★最容易踩的一脚★：冻结时 sys.executable 是 exe 自己，不能拿去跑 main.py
        code2 = (
            "import sys;"
            f"sys.frozen=True; sys._MEIPASS=r'C:\\nope'; sys.executable=r'{venv_py}';"
            "from voice_loop import service_ctl;"
            "print(service_ctl.python_exe()); print(service_ctl.main_script())"
        )
        out3 = child(code2, env={"LOCAL_AI_ROOT": str(tmp)})
        lines = [ln.strip() for ln in out3.stdout.strip().splitlines() if ln.strip()]
        check("service_ctl 用的是环境里那个 Python", lines[:1], [str(venv_py)],
              detail=out3.stderr[-300:])
        check("main.py 在项目根下", lines[1:2], [str(tmp / "main.py")])


def section_consistency() -> None:
    print("\n[5] 四处看到的根必须是同一个（以前各推各的）")
    import voice_loop.settings as settings
    import voice_loop.setup_flow as setup_flow
    from voice_loop import paths, service_ctl

    check("settings.PROJECT_ROOT", settings.PROJECT_ROOT, paths.app_root())
    check("setup_flow.ROOT", setup_flow.ROOT, paths.app_root())
    check("service_ctl.main_script 的父目录", service_ctl.main_script().parent, paths.app_root())
    check("源码根也在同一个地方（源码运行时）", paths.source_root(), paths.app_root())


def section_version() -> None:
    print("\n[6] 版本号：项目根那份优先（那才是「装在你机器上的版本」）")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "VERSION").write_text("# 注释\n9.9.9\n", encoding="utf-8")
        out = child("import voice_loop; print(voice_loop.__version__)",
                    env={"LOCAL_AI_ROOT": str(tmp)})
        check("读到了项目根里的版本号", out.stdout.strip(), "9.9.9", detail=out.stderr[-200:])

        empty = Path(tmpdir) / "empty"
        empty.mkdir()
        out2 = child("import voice_loop; print(voice_loop.__version__)",
                     env={"LOCAL_AI_ROOT": str(empty)})
        check("项目根没有 VERSION → 退回解包目录/源码根那份", out2.stdout.strip(),
              (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
              detail=out2.stderr[-200:])


def section_no_regression() -> None:
    print("\n[7] ★防回退★：这几个模块里不许再出现「拿 __file__ 推项目根」")
    # 冻结之后 __file__ 是临时解包目录（每次启动都换名字）。这几处以前各自推根，
    # 现在统一走 paths.py —— 谁再写回 `parents[1]`，这条测试就会失败。
    # ★用 AST 而不是 grep★：docstring 里为了讲明白这件事会**提到**这个写法，
    # 按文本搜会把说明文字也算成违规（第一版就误报了一次）。
    import ast  # noqa: PLC0415

    guard = [
        "voice_loop/settings.py",
        "voice_loop/setup_flow.py",
        "voice_loop/service_ctl.py",
        "voice_loop/__init__.py",
        "voice_loop/console/app.py",
        "voice_loop/console/panels/update.py",
        "console_exe.py",
    ]
    bad: list[str] = []
    for rel in guard:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
                    and node.slice.value):
                bad.append(f"{rel}: parents[{node.slice.value}]（第 {node.lineno} 行）")
    check("没有残留的 parents[N] 推根", bad, [])

    from voice_loop.console import app as console_app

    check("静态文件目录来自解包/源码目录（冻结后它自己会指对）",
          console_app.STATIC_DIR.name, "static")
    check("面板目录同理", console_app.PANELS_DIR.name, "panels")


def section_frozen_summary() -> None:
    print("\n[8] 冻结时的一整份快照（出问题先看这个）")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "config.toml").write_text("[app]\n", encoding="utf-8")
        (tmp / "main.py").write_text("", encoding="utf-8")
        exe = tmp / "app.exe"
        exe.write_bytes(b"")
        code = (
            "import sys, json;"
            f"sys.frozen=True; sys._MEIPASS=r'{tmp}'; sys.executable=r'{exe}';"
            "from voice_loop import paths;"
            "print(json.dumps(paths.describe(), ensure_ascii=False))"
        )
        out = child(code)
        try:
            snap = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            snap = {}
            check("快照能解析", False, detail=out.stdout[-200:] + out.stderr[-200:])
        check("快照 frozen=True", snap.get("frozen"), True)
        check("快照 app_root 指向假项目", snap.get("app_root"), str(tmp))
        check("快照里有 exe 路径", snap.get("exe"), str(exe))
        check("快照记了环境变量（便于排查）", sorted((snap.get("env") or {}).keys()),
              ["LOCAL_AI_PYTHON", "LOCAL_AI_ROOT"])


def main() -> int:
    print("=" * 70)
    print(" 路径 / 冻结 / 解释器 自测（纯离线）")
    print("=" * 70)
    section_source()
    section_env_override()
    section_frozen()
    section_python()
    section_consistency()
    section_version()
    section_no_regression()
    section_frozen_summary()
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
