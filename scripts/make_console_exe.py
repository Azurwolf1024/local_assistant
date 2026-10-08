"""把控制台冻成一个能双击的 exe（Windows）。

    python scripts/make_console_exe.py              # onedir（推荐：启动 1~2 秒）
    python scripts/make_console_exe.py --onefile    # 单文件（好分发，但每次启动要解包）
    python scripts/make_console_exe.py --with-webview   # 连 pywebview 一起打（真原生窗口）
    python scripts/make_console_exe.py --check      # 只看会怎么打，不真打

★为什么要有这个脚本，而不是在 README 里写一行 PyInstaller 命令★：那条命令里有
**四类必须记住的坑**，任何一条忘了就是一个能跑但残废的 exe（都是实测踩出来的）：

1. **面板是「按文件路径动态加载」的** —— PyInstaller 静态分析看不到面板内部的懒导入，
   于是 ``voice_loop.event_text``、``persona_card`` 不会进包，装出来的控制台会
   静悄悄少两个标签页（schedule 与 persona）。所以这里用 AST 把
   ``console/panels/*.py`` 里的 ``from ... import ...`` **全扫出来**当 hidden-import。
2. **一放行懒导入，体积就从 65 MB 涨到 354 MB** —— 面板经 ``clone`` 摸到语音栈
   （numpy/sherpa/openvino/torch…）。控制台**永远不执行**它们（它只读文件 + 投信箱命令），
   所以按 :data:`EXCLUDES` 显式排除；这也顺手把启动时间压回 1 秒级。
3. **static / panels / VERSION 不在代码里**，得 ``--add-data`` 带进去，
   否则界面 404、版本号变 ``0.0.0+unknown``。
4. **无窗口 exe 里 ``sys.stdout`` 是 None** —— 入口 ``console_exe.py`` 先把输出接进
   ``sessions/console-exe.log`` 再干活（这也是 ``main.py`` 里早有的那个坑）。

默认 **onedir**：一个文件夹里放着 exe 与依赖。理由见 ``--onefile`` 的说明
（实测 onefile 双击到界面能答 6.6 s，onedir 1.6 s）。
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import __version__  # noqa: E402

ENTRY = ROOT / "console_exe.py"
NAME = "local-assistant-console"

EXCLUDES = [
    "numpy", "scipy", "pandas", "matplotlib", "PIL", "cv2", "sounddevice", "soundfile",
    "sherpa_onnx", "onnxruntime", "openvino", "torch", "transformers", "optimum",
    "ollama", "httpx", "aiohttp", "sklearn", "IPython", "pytest", "setuptools",
]
"""★控制台不碰的重家什★：排除它们体积 354 MB → 65 MB，启动也快一截。

（控制台只做三件事：读 json/toml、往信箱目录投命令、发静态文件。
真正用模型的是**另一个进程** —— 语音服务，由 ``python main.py listen`` 起。）
"""

ADD_DATA = [
    ("voice_loop/console/static", "voice_loop/console/static"),
    ("voice_loop/console/panels", "voice_loop/console/panels"),
    ("VERSION", "."),
]
"""界面文件 + 面板源码 + 版本号：不在 import 图里，必须显式带上。

★panels 是按文件路径加载的★（见 ``console/app.py`` 的 ``PANELS_DIR``），
所以它们得作为**数据文件**躺在原来的相对位置上，不能只当模块打进去 ——
这也是「热插拔面板」这个设计在 exe 里的代价。
"""

WEBVIEW_PACKAGES = ["webview", "clr_loader", "pythonnet"]
"""``--with-webview`` 时要整包收进来的（pywebview 在 Windows 上靠 pythonnet 调 WebView2）。"""


# --------------------------------------------------------------------------- #
# 面板的懒导入（坑 1）
# --------------------------------------------------------------------------- #
def _module_of(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def lazy_imports(paths: list[Path]) -> list[str]:
    """扫出这些文件里 import 的 ``voice_loop.*`` 子模块（按文件路径加载的模块只有靠它）。"""
    found: set[str] = set()
    for path in paths:
        module = _module_of(path)
        package_parts = module.split(".")[:-1]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("voice_loop"):
                        found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = ("." * node.level) + (node.module or "")
                target = _resolve_relative(base, package_parts)
                if not target or not target.startswith("voice_loop"):
                    continue
                found.add(target)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    found.add(f"{target}.{alias.name}")
                    # 也可能是「包里的模块」（from ... import event_text → voice_loop.event_text）
                    sibling = ROOT.joinpath(*target.split("."), f"{alias.name}.py")
                    if sibling.is_file():
                        found.add(f"{target}.{alias.name}")
    return sorted(found)


def _resolve_relative(base: str, package_parts: list[str]) -> str:
    """把 ``..x`` / ``.y`` 这种相对导入还原成绝对模块名。"""
    level = len(base) - len(base.lstrip("."))
    rest = base.lstrip(".")
    if level == 0:
        return rest
    keep = package_parts[: len(package_parts) - (level - 1)] if level > 1 else package_parts
    return ".".join([*keep, rest]) if rest else ".".join(keep)


def hidden_imports() -> list[str]:
    """★打 exe 时真正要补的那串★：面板 + 面板用的路由模块 + 包根。"""
    panels = sorted((ROOT / "voice_loop" / "console" / "panels").glob("*.py"))
    modules = sorted((ROOT / "voice_loop" / "console").glob("*.py"))
    targets = panels + modules
    got = {m for m in lazy_imports(targets) if m.startswith("voice_loop")}
    # 只留真实存在的模块名（``voice_loop.events`` 可能被写成 ``voice_loop.events.EventStore``）
    clean: list[str] = []
    for name in sorted(got):
        parts = name.split(".")
        candidate = ROOT.joinpath(*parts).with_suffix(".py")
        package = ROOT.joinpath(*parts, "__init__.py")
        if candidate.is_file() or package.is_file():
            clean.append(name)
    return clean


# --------------------------------------------------------------------------- #
# Windows 版本信息（exe 属性里那一页）
# --------------------------------------------------------------------------- #
def version_file() -> Path:
    """生成 PyInstaller 认的版本信息文件（``--version-file``）。"""
    nums = [int(x) for x in (__version__.split(".") + ["0", "0", "0", "0"])[:4]
            if x.isdigit()] or [0, 0, 0, 0]
    while len(nums) < 4:
        nums.append(0)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple(nums)}, prodvers={tuple(nums)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'local_assistant'),
        StringStruct('FileDescription', '本地语音助手 · 控制台'),
        StringStruct('FileVersion', '{__version__}'),
        StringStruct('ProductName', 'local_assistant'),
        StringStruct('ProductVersion', '{__version__}'),
        StringStruct('OriginalFilename', '{NAME}.exe'),
        StringStruct('LegalCopyright', ''),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    path = ROOT / "build" / f"{NAME}-version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 组装命令行 / 打包
# --------------------------------------------------------------------------- #
def plan(*, onefile: bool = False, with_webview: bool = False, noconsole: bool = True,
         out: Path | None = None, name: str = NAME) -> tuple[list[str], dict]:
    """拼出 PyInstaller 的命令行（``--check`` 就是只打印它）。"""
    out = out or (ROOT / "dist")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if not noconsole else "--windowed",
        "--name", name,
        "--distpath", str(out),
        "--workpath", str(ROOT / "build" / f"{name}-work"),
        "--specpath", str(ROOT / "build"),
        "--version-file", str(version_file()),
        "--collect-submodules", "voice_loop.console",
    ]
    for mod in hidden_imports():
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for src, dst in ADD_DATA:
        cmd += ["--add-data", f"{ROOT / src}{os.pathsep}{dst}"]
    if with_webview:
        for pkg in WEBVIEW_PACKAGES:
            cmd += ["--collect-all", pkg]
    else:
        for pkg in WEBVIEW_PACKAGES:
            cmd += ["--exclude-module", pkg]
    cmd.append(str(ENTRY))
    info = {
        "version": __version__,
        "mode": "onefile" if onefile else "onedir",
        "window": "windowed" if noconsole else "console",
        "webview": with_webview,
        "hidden": hidden_imports(),
        "excludes": EXCLUDES,
        "out": str(out),
    }
    return cmd, info


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def build(*, onefile: bool = False, with_webview: bool = False, noconsole: bool = True,
          out: Path | None = None, name: str = NAME, echo=print) -> tuple[bool, dict]:
    """真打一次。返回 (成功没, 结果信息)。"""
    out = out or (ROOT / "dist")
    cmd, info = plan(onefile=onefile, with_webview=with_webview, noconsole=noconsole,
                     out=out, name=name)
    echo(f"打 exe：{'单文件' if onefile else '文件夹'}（{info['window']}）"
         f"{' + pywebview' if with_webview else ''}")
    echo(f"  补进来的模块 {len(info['hidden'])} 个：{'、'.join(info['hidden'])}")
    echo("  PyInstaller 正在跑（第一次要 1~3 分钟）…")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        info["error"] = (proc.stdout or "")[-1200:] + (proc.stderr or "")[-2000:]
        info["ok"] = False
        return False, info
    target = out / (f"{name}.exe" if onefile else name)
    info["target"] = str(target)
    info["bytes"] = size_of(target) if target.exists() else 0
    info["ok"] = bool(target.exists())
    echo(f"  产物：{target}（{info['bytes'] / 1048576:.1f} MB）")
    return info["ok"], info


def main() -> int:
    ap = argparse.ArgumentParser(description="把控制台打成 exe")
    ap.add_argument("--onefile", action="store_true", help="打成单个 exe（启动慢，好分发）")
    ap.add_argument("--with-webview", action="store_true",
                    help="连 pywebview 一起打（真原生窗口，体积会大不少）")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口（排查用）")
    ap.add_argument("--out", default="", help="产物目录（默认 dist/）")
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--check", action="store_true", help="只打印计划和命令，不真打")
    args = ap.parse_args()

    if args.check:
        cmd, info = plan(onefile=args.onefile, with_webview=args.with_webview,
                         noconsole=not args.console,
                         out=Path(args.out) if args.out else None, name=args.name)
        print(f"版本 {info['version']} · {info['mode']} · {info['window']}"
              f"{' · pywebview' if info['webview'] else '（窗口走 Edge/浏览器）'}")
        print(f"补进来的模块（{len(info['hidden'])}）：")
        for mod in info["hidden"]:
            print(f"  + {mod}")
        print("排除的重库：" + "、".join(info["excludes"]))
        print("\n命令：\n  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        return 0

    ok, info = build(onefile=args.onefile, with_webview=args.with_webview,
                     noconsole=not args.console,
                     out=Path(args.out) if args.out else None, name=args.name)
    if not ok:
        print("★打包失败★")
        print(info.get("error", ""))
        return 1
    print("\n下一步：把产物放到项目里（和 main.py 同一层），双击就是控制台：")
    print(f"   · {info['target']}")
    print("   control/（onedir）整个文件夹都可以放到项目根，exe 会自己往上找 config.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
