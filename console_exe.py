"""控制台 exe 的入口（★只给冻结用；源码运行请走 ``python main.py ui``★）。

为什么单独一个文件而不是直接冻 ``main.py``：
    ``main.py`` 是**语音服务**的命令行（会 import numpy / sounddevice / 模型那一套），
    而控制台 exe 只需要 fastapi + 面板，冻 ``main.py`` 等于把整套语音栈塞进 exe。
    这里只 import ``voice_loop.console``，包能小 5 倍，启动也快。

双击时（没有命令行参数）它做这几件事，顺序都是为「像个软件」：
    1. **先把 stdout/stderr 接到日志文件**（exe 是无窗口的，print 掉了就没人看得见）
    2. 找项目根（``LOCAL_AI_ROOT`` → exe 附近往上找 config.toml + main.py）
    3. 清掉上次更新留下的 ``*.old``（见 ``voice_loop/update.py``）
    4. 端口已经有控制台在跑 → ★不报错，直接把那个打开★（双击两次是常见动作）
    5. 开窗口（pywebview → Edge 应用窗口 → 浏览器，见 ``console/window.py``）

命令行（给快捷方式加参数用）：
    --root <目录>      指定项目根（等价于环境变量 LOCAL_AI_ROOT）
    --port/--host      监听端口与地址
    --window <后端>     auto / pywebview / edge / browser
    --no-window        不要窗口，用系统浏览器
    --console          日志也打到附加的控制台（排查用）
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path


def _alert(title: str, text: str) -> None:
    """弹一个系统消息框（无窗口 exe 里，这是唯一能把话说给用户听的地方）。"""
    print(f"{title}: {text}", flush=True)
    if os.name != "nt":
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)  # 0x10 = 错误图标
    except Exception:  # noqa: BLE001 - 弹不出来也不能再抛一个异常
        pass


def _setup_logging(root: Path, *, console: bool) -> Path | None:
    """把 stdout/stderr 接到日志文件（★必须在任何 print 之前★）。

    冻结成 ``--noconsole`` 的 exe 里 ``sys.stdout`` 是 **None**，不接的话第一次 print
    就抛异常（``main.py`` 里那套 ``ensure_std_streams`` 就是为这个见过的坑写的）。
    ``--console``（排查用）时不重定向，日志直接看窗口。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # GBK 控制台编不出的字符不许炸
        except Exception:  # noqa: BLE001
            pass
    if console and sys.stdout is not None and sys.stderr is not None:
        return None
    # ★注意★：windowed 构建下就算写了 --console 也可能没有真的控制台
    # （stdout 是 None）—— 那就还是得接日志文件，否则第一次 print 就抛异常。
    log_path = root / "sessions" / "console-exe.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        # 日志都写不了（只读目录）：至少保证 stdout 有个去处，别让 print 抛异常
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = sys.stdout
        return None
    sys.stdout = handle
    sys.stderr = handle
    import time  # noqa: PLC0415

    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 控制台 exe 启动 ===", flush=True)
    return log_path


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="local-assistant-console",
                                 description="本地语音助手 · 控制台（exe）")
    ap.add_argument("--root", default="", help="项目根（默认：exe 附近往上找）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--window", default="auto",
                    help="窗口后端：auto / pywebview / edge / browser")
    ap.add_argument("--no-window", action="store_true", help="用系统浏览器，不开应用窗口")
    ap.add_argument("--console", action="store_true", help="日志同时打到控制台窗口")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：起一下控制台、自己请求几个地址、然后退出（排查用）")
    ap.add_argument("--version", action="store_true", help="打印版本后退出")
    return ap.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    from voice_loop import paths  # noqa: PLC0415

    if args.root:
        paths.set_app_root(args.root)
    root = paths.app_root()
    log_path = _setup_logging(root, console=bool(args.console))
    if log_path:
        print(f"项目根 {root} · 日志 {log_path}")

    if args.version:
        from voice_loop import __version__  # noqa: PLC0415

        print(f"local_assistant {__version__}（{paths.describe()['app_root']}）")
        return 0

    try:
        from voice_loop import update  # noqa: PLC0415

        removed = update.cleanup_old_exe()
        if removed:
            print(f"清掉了上次更新留下的：{', '.join(removed)}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不该拦住启动
        print(f"（清理旧 exe 时出了点问题，忽略：{exc}）")

    if not paths.looks_like_project(root):
        _alert(
            "找不到项目",
            f"这里不像项目根（缺 config.toml 或 main.py）：\n{root}\n\n"
            "怎么办：\n"
            "  1) 把控制台 exe 放到项目目录里（和 main.py 同一层），或\n"
            f"  2) 设环境变量 {paths.ENV_ROOT} 指到项目目录，或\n"
            "  3) 给快捷方式加参数：--root \"D:\\你的项目目录\"\n\n"
            "还没装过？先跑项目里的 install.ps1。",
        )
        return 3

    try:
        from voice_loop.console import app as console_app  # noqa: PLC0415
        from voice_loop.console import window as window_mod  # noqa: PLC0415
        from voice_loop.settings import load_settings  # noqa: PLC0415
    except ImportError as exc:
        _alert("控制台缺依赖", f"没装齐 fastapi/uvicorn：\n{exc}\n\n先跑：python main.py setup")
        return 2

    settings = load_settings(root / "config.toml")

    # ★端口上已经有一个 → 直接开它★：双击两次是最常见的动作，
    # 那时用户要的是「把界面给我」，不是一句「端口被占」。
    if console_app.port_in_use(args.host, args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"端口 {args.port} 已经有控制台在跑，直接打开它：{url}")
        backend = "browser" if args.no_window else args.window
        ok, note = window_mod.open_window(url, title="本地语音助手 · 控制台", backend=backend,
                                         profile_dir=settings.sessions_dir / "window")
        if not ok:
            import webbrowser  # noqa: PLC0415

            webbrowser.open(url)
            print("（已改用浏览器）")
        return 0

    window = "browser" if args.no_window else args.window
    print(f"窗口后端 {window}（可用：{window_mod.choose(window)}）")
    if args.selftest:
        return _selftest(settings, console_app, args)
    return console_app.serve(settings, host=args.host, port=int(args.port),
                            open_browser=True, window=window)


def _selftest(settings, console_app, args) -> int:
    """起一次控制台并自己请求几个关键地址 —— **不开窗口**，排查用。

    ★为什么要有★：exe 是无窗口的，双击没反应时看不出哪里坏了。
    这个模式把「面板装上了吗、静态文件在不在、接口答不答」一次性摆出来，
    结果写在 ``sessions/console-exe.log`` 里（也是给用户贴给别人看的那份）。
    """
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    app = console_app.create_app(settings)
    server = console_app._make_server(uvicorn, app, args.host, int(args.port))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    ids = app.state.ctx.registry.ids()
    print(f"自检：面板 {ids}")
    ok = bool(ids)
    base = f"http://{args.host}:{args.port}"
    failed: dict = {}
    for url in ("/api/meta", "/style.css", "/panels/persona.js", "/api/service"):
        try:
            with urllib.request.urlopen(base + url, timeout=10) as resp:
                body = resp.read()
            print(f"自检：{url} → {resp.status}（{len(body)} 字节）")
            if url == "/api/meta":
                import json  # noqa: PLC0415

                failed = (json.loads(body.decode("utf-8")) or {}).get("panels_failed") or {}
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"自检：{url} → 失败 {type(exc).__name__}: {exc}")
    print(f"自检：装不上的面板 {failed or '（无）'}")
    if failed:
        ok = False
    server.should_exit = True
    time.sleep(0.6)
    print(f"自检结论：{'通过' if ok else '★没通过★'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(_parse(argv))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 双击运行，崩溃必须留痕 + 说话
        text = traceback.format_exc()
        try:
            root = Path(os.environ.get("LOCAL_AI_ROOT") or Path(sys.executable).resolve().parent)
            (root / "sessions").mkdir(parents=True, exist_ok=True)
            (root / "sessions" / "console-exe-crash.log").write_text(text, encoding="utf-8")
        except OSError:
            pass
        _alert("控制台启动失败", text[-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
