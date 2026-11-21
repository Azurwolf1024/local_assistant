"""控制台主体：FastAPI 应用、共享上下文、面板装载、静态文件。

``python main.py ui`` 最终就调 :func:`serve`。

★设计取舍★（写下来免得以后有人把它改成「一个大 all-in-one 文件」）：
    - 本文件只管**基础设施**：静态文件、面板清单、实时通道、错误处理、日志。
      业务逻辑一律在 ``panels/*.py`` 里，加功能不该改本文件。
    - 所有面板共享 :class:`ConsoleContext`（配置、事件存储、备忘、命令通道、总线），
      它**故意不做懒加载之外的事**：控制台进程不加载任何模型/音频设备。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from ..control import ControlChannel
from .. import service_ctl
from ..paths import describe as describe_paths
from ..settings import Settings
from . import window as window_mod
from .bus import EventBus
from .follow import Watcher
from .registry import Panel, Registry

STATIC_DIR = Path(__file__).resolve().parent / "static"
PANELS_DIR = Path(__file__).resolve().parent / "panels"
PANELS_PACKAGE = "voice_loop.console.panels"


def panel_stamp() -> str:
    """面板代码的指纹：``panels/*.py`` + ``static/panels/*.js`` + ``registry.py`` 的哈希。

    ★为什么需要它★：控制台的静态脚本是**每次；请求从磁盘现取**的，而路由/接口是
    **进程启动时装载**的。只改代码不重启，就会出现「页面是新的、后端是旧的」
    这种半新半旧状态（新面板去调一个旧进程里不存在的接口）——
    现象往往只是一个空荡荡的页面，还什么都不报。
    所以 ``/api/meta`` 同时给「启动时的指纹」与「现在的指纹」，不一致就是该重启了。
    """
    import hashlib  # noqa: PLC0415

    try:
        base = Path(__file__).resolve().parent
        files = sorted(PANELS_DIR.glob("*.py")) + sorted((STATIC_DIR / "panels").glob("*.js"))
        files += sorted(base.glob("*.py"))            # registry / persona_card / window / bus…
        # ★面板 register() 里 import 过、或路由会走到的包内模块★：它们在进程里只加载一次，
        # 改了不重启就会出现「后端还是旧的」（`voice_loop/persona_import.py` 就是这么加进来的：
        # 界面上的批量导入跟着旧逻辑跑，页面却看不出哪里不对）。
        for rel in ("persona_import.py", "update.py", "manifest.py", "persona.py"):
            files.append(base.parent / rel)
        digest = hashlib.sha1()
        for path in files:
            if not path.is_file():
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:12]
    except OSError:                      # 读不动就不报不一致（宁可没提示，也别瞎报）
        return ""


@dataclass
class ConsoleContext:
    """面板共享的东西。一个进程一份（由 :func:`create_app` 建好并塞进 ``app.state``）。"""

    settings: Settings
    bus: EventBus
    registry: Registry
    channel: ControlChannel
    watcher: Watcher
    log: logging.Logger
    started_at: float = field(default_factory=time.time)
    cache: dict = field(default_factory=dict)
    """面板可以往里塞缓存（比如角色索引），互不干扰——用面板 id 当 key 前缀更清楚。"""

    # ---------------------------------------------------------------- 便捷方法
    @property
    def root(self) -> Path:
        return self.settings.root

    def events(self):
        """统一事件存储（日程/闹钟/事件链都是它）。**每次新建**，靠文件 mtime 保证新鲜。"""
        # 为什么每次新建：面板是给另一个进程（服务）共用的数据，缓存反而会用到旧快照。
        # EventStore 本身很轻（只是包一个 JsonStore），新建它不花钱。
        from ..events import EventStore  # noqa: PLC0415 - 免得控制台启动就拉起全部模块

        return EventStore(self.root / "data" / "events.json")

    def memos(self):
        """备忘存储（``data/memos.json``，包装格式，元素 {content, done}）。"""
        from ..store import JsonStore  # noqa: PLC0415

        return JsonStore(self.root / "data" / "memos.json", default=[])

    def characters(self):
        """角色注册表（data/characters.json + data/personas/*.json）。"""
        from ..persona import CharacterRegistry  # noqa: PLC0415

        return CharacterRegistry(self.settings.resolve(self.settings.persona.file))

    def call_service(self, cmd: str, *, timeout: float = 20.0, **args):
        """给服务投一条命令并等回执——**先看服务在不在跑**。

        为什么不让调用方自己等超时：服务没启动时，`channel.call` 会老老实实等满
        ``timeout``（试听那条设了 25 秒），用户点一下按钮就得盯着一动不动的界面干等。
        ★先查进程再投命令★，立刻就能说「服务没启动，先点右上角启动」，这才是界面该有的反应。
        """
        from ..control import Reply  # noqa: PLC0415

        status = service_ctl.status(self.settings)
        if not status.running:
            why = "服务没在跑"
            if status.pid_stale or status.log_pending:
                why = "服务没在跑（有上次留下的残留文件，点「启动服务」会顺手清掉）"
            return Reply(
                id="", ok=False, error=f"{why}——先点右上角「启动服务」", at=time.time(), seconds=0.0
            )
        return self.channel.call(cmd, timeout=timeout, **args)

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)


def load_panels(app, ctx: ConsoleContext) -> tuple[list[str], dict[str, str]]:
    """导入 ``voice_loop.console.panels`` 的 ``ALL`` 清单里每个模块并注册。

    每个模块要做两件事：提供 ``PANEL``（元数据 → 前端标签）与 ``register(app, ctx)``
    （挂自己的路由）。★两个都要★——只 import 不调 register 的话，路由根本不存在，
    请求会掉到静态文件那边（表现是莫名其妙的 405，踩过）。

    ★坏一个面板不能拖垮整个控制台★：单个模块导入/注册失败只记下来，界面照样能用
    （错误会出现在 ``/api/meta`` 里，一眼能看出是谁坏了）。
    """
    ok: list[str] = []
    bad: dict[str, str] = {}
    try:
        package = importlib.import_module(PANELS_PACKAGE)
    except Exception as exc:  # noqa: BLE001
        return [], {"(panels 包)": f"{type(exc).__name__}: {exc}"}
    names: list[str] = list(getattr(package, "ALL", []))
    for name in names:
        try:
            module = importlib.import_module(f"{PANELS_PACKAGE}.{name}")
            panel = getattr(module, "PANEL", None)
            if not isinstance(panel, Panel):
                raise TypeError(f"{name}.PANEL 必须是一个 Panel")
            if ctx.registry.get(panel.id) is None:
                ctx.registry.add(panel, owner=name)
            register = getattr(module, "register", None)
            if not callable(register):
                raise TypeError(f"{name} 缺少 register(app, ctx)")
            register(app, ctx)
            ok.append(panel.id)
        except Exception as exc:  # noqa: BLE001
            bad[name] = f"{type(exc).__name__}: {exc}"
            ctx.log.warning(f"面板 {name} 装载失败（已跳过）：{exc}")
    return ok, bad


def create_app(settings: Settings, logger: logging.Logger | None = None):
    """建 FastAPI 应用。测试直接用它，不必真的监听端口。"""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - 只在没装依赖时发生
        raise SystemExit(
            "控制台需要 fastapi 与 uvicorn：\n"
            "    pip install fastapi uvicorn\n"
            f"（原始错误：{exc}）"
        ) from exc

    log = logger or logging.getLogger("voice_loop.console")
    bus = EventBus(log)
    registry = Registry()
    channel = ControlChannel(settings.resolve(settings.app.console_dir))
    channel.ensure()
    channel.prune()
    watcher = Watcher(settings, bus, log)
    ctx = ConsoleContext(
        settings=settings, bus=bus, registry=registry, channel=channel, watcher=watcher, log=log
    )

    app = FastAPI(
        title="本地语音助手 · 控制台",
        description="本机工具：改日程/闹钟、看日志、切声线、文字对话。只监听回环地址。",
        version="1.0.0",
    )
    app.state.ctx = ctx

    loaded, failed = load_panels(app, ctx)
    log.info(f"控制台面板：{', '.join(loaded) or '（无）'}")
    if failed:
        log.warning(f"有 {len(failed)} 个面板装不上：{failed}")

    # 启动时记一份面板代码指纹：以后 /api/meta 里跟「现在磁盘上的」对不上，
    # 就说明这份代码改过而进程没重启（前端会当场提醒）。
    startup_stamp = panel_stamp()

    # ------------------------------------------------------------------ 基础接口
    @app.post("/api/app/quit")
    def api_app_quit():
        """★退出应用★（`python main.py stop --app` 与将来的界面按钮都走这里）。

        ★为什么走 HTTP 而不是直接 kill★：退出要按顺序做事（**先停语音服务**，再关窗口，
        最后让 uvicorn 收尾）。硬杀做不到这些，还会留下「服务还在跑、应用没了」。
        所以这里只是把活儿丢给 ``serve()`` 里那个 ``_Application.quit``，立刻返回。
        """
        quit_fn = getattr(app.state, "quit_app", None)
        if quit_fn is None:
            return {"ok": False,
                    "error": "这个进程没有接退出动作（用 main.py ui 起的时候才有）"}
        threading.Thread(target=quit_fn, name="app-quit-api", daemon=True).start()
        return {"ok": True, "message": "正在退出应用（先停服务、再关窗口）"}

    @app.get("/api/meta")
    def api_meta():
        """界面启动时拉的第一份数据：我是谁、有哪些面板、数据在哪。"""
        return {
            "app": "本地语音助手 · 控制台",
            "root": str(settings.root),
            "config": str(settings.config_path) if hasattr(settings, "config_path") else "config.toml",
            "console_dir": str(channel.root),
            "log_file": str(watcher.log_path),
            "panels": [p.as_dict() for p in registry.all()],
            "panels_failed": failed,
            # ★两个指纹不一样 = 面板代码改过、但这个进程没重启★（前端会弹一句提醒）
            "panels_stamp": startup_stamp,
            "panels_stamp_now": panel_stamp(),
            "started_at": ctx.started_at,
            "python": f"{__import__('sys').version.split()[0]}",
            # ★「我是被谁起的」★：exe 还是源码、项目根在哪、能选哪个窗口后端 ——
            # 这几条不写在界面上，出问题时（找不到配置、服务起不来）就得靠猜。
            "paths": describe_paths(settings.root),
            "window": window_mod.describe(),
        }

    @app.get("/api/panels")
    def api_panels():
        return [p.as_dict() for p in registry.all()]

    @app.get("/api/stream")
    async def api_stream(kinds: str | None = None):
        """SSE：日志行 + 服务状态变化（``?kinds=log,status`` 可选过滤）。"""
        from fastapi.responses import StreamingResponse  # noqa: PLC0415

        want = tuple(k.strip() for k in kinds.split(",")) if kinds else None
        return StreamingResponse(bus.subscribe(want), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/health")
    def api_health():
        return {
            "ok": True,
            "uptime": ctx.uptime,
            "subscribers": bus.subscribers,
            "panels": registry.ids(),
            "mailbox": channel.status(),
        }

    # ------------------------------------------------------------------ 服务启停
    # 放在核心而不是某个面板里：顶栏每个页面都要用（状态灯 + 两个按钮）。
    # ★启停一律交给 main.py（service_ctl）★，控制台绝不自己碰 pid/stop 文件——
    # 那是服务的生命周期代码才该做的事，越权会做出「服务哑了但界面说在跑」这种事。
    @app.get("/api/service")
    def api_service():
        st = service_ctl.status(settings)
        return {
            **st.as_dict(),
            "last_log_lines": service_ctl.tail_lines(settings, 5),
            "mailbox": channel.status(),
            "actions": {
                "can_start": not st.running,
                "can_stop": st.running or st.pid_stale or st.log_pending,
            },
        }

    @app.post("/api/service/start")
    def api_service_start():
        # ★等它真的起来才回成功★（最多 25 秒）：以前回一句「已发出命令」就算成功，
        # 服务其实没起来（缺模型、端口/设备被占）时界面会说「已启动」= 看起来就是启停坏了
        started, message = service_ctl.start_service(settings)
        st = service_ctl.status(settings)
        return {
            "started": started,
            "message": message,
            "status": st.as_dict(),
            "log_lines": service_ctl.tail_lines(settings, 6),
        }

    @app.post("/api/service/stop")
    def api_service_stop():
        stopped, message = service_ctl.stop_service(settings)
        st = service_ctl.status(settings)
        return {
            "stopped": stopped,
            "message": message,
            "status": st.as_dict(),
            "log_lines": service_ctl.tail_lines(settings, 6),
        }

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        """★任何未捕获异常都变成结构化 JSON★：前端统一显示红条，而不是一页白屏。"""
        log.exception(f"控制台接口出错：{request.method} {request.url.path}")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"{type(exc).__name__}: {exc}", "path": request.url.path},
        )

    @app.middleware("http")
    async def fresh_static(request: Request, call_next):
        """★静态文件一律「先校验再用」★（no-cache，不是不缓存）。

        这个控制台是本地工具，代码随时在改 —— 默认的启发式缓存会让浏览器拿着旧
        `app.js` / `style.css` 不放（实测：改了 CSS 刷新页面还是老样子，而 JS 是新的，
        最难查的就是这种「有的新有的旧」）。加上 no-cache 之后浏览器每次都会带
        `If-None-Match` 来问一句，没变就是 304（几毫秒），变了立刻是新内容。
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html", ".json", ".svg")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ------------------------------------------------------------------ 生命周期
    @app.on_event("startup")
    async def on_startup():
        import asyncio  # noqa: PLC0415

        bus.bind(asyncio.get_running_loop())
        watcher.start()
        log.info("控制台已启动（只监听本机回环地址）")

    @app.on_event("shutdown")
    async def on_shutdown():
        # ★顺序有讲究：先停跟随线程，再关订阅者★
        # 关订阅者会把挂着 SSE 的网页叫醒收尾；不做的话 uvicorn 会一直等那条
        # 永远不结束的响应（用户在终端 Ctrl+C 就退不出来了）。
        watcher.stop()
        bus.close()
        log.info("控制台已退出")

    # ------------------------------------------------------------------ 静态文件
    # 放在最后：/api/* 由上面的路由先匹配，剩下的才轮到静态文件
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


def port_in_use(host: str, port: int) -> bool:
    """端口上是否已经有人在听（用 connect 探一下，比 bind 试探安全）。"""
    import socket  # noqa: PLC0415

    target = host or "127.0.0.1"
    if target in ("0.0.0.0", "::"):
        target = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((target, int(port))) == 0


class _Application:
    """应用的「退出 / 显示窗口」协调器：托盘菜单和终端 Ctrl+C 都走这里。

    ★为什么单独一个对象★：退出要做三件事且顺序固定（停服务 → 关窗口 → 通知主线程），
    而触发点有两个（托盘线程、主线程 Ctrl+C）。放在一处才不会两处各写一遍、慢慢分叉。
    ★必须幂等★：托盘连点两下「退出」、或先 Ctrl+C 再从托盘点退出，都不能停两次服务。
    """

    def __init__(self, settings: Settings, *, log: logging.Logger,
                 server: Any, done: threading.Event, grace: float = 20.0) -> None:
        self.settings = settings
        self.log = log
        self.server = server
        self.done = done
        self.grace = grace
        self.window: Any = None
        self._lock = threading.Lock()
        self._quitting = False

    def set_window(self, window: Any) -> None:
        self.window = window

    def show_window(self) -> tuple[bool, str]:
        """把主窗口叫回来（双击托盘图标 / 菜单「显示控制台窗口」）。"""
        window = self.window
        if window is None:
            return False, "这个应用没开窗口（--window off），直接用浏览器打开控制台地址"
        return window.show()

    def quit(self, reason: str = "") -> None:
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        why = f"（{reason}）" if reason else ""
        self.log.info(f"[应用] 开始退出{why}")
        try:
            from .. import service_ctl  # noqa: PLC0415

            if service_ctl.status(self.settings).running:
                ok, msg = service_ctl.stop_service(self.settings, timeout=self.grace)
                self.log.info(f"[应用] 停服务：{msg}" if ok else f"[应用] 停服务没成：{msg}")
        except Exception as exc:  # noqa: BLE001 - 退出流程不能因为停服务失败而卡住
            self.log.warning(f"[应用] 停服务出错（继续退出）：{exc}")
        window = self.window
        if window is not None:
            try:
                window.destroy()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"[应用] 关窗口出错（继续退出）：{exc}")
        self.done.set()


def _ensure_service(settings: Settings, log: logging.Logger) -> str:
    """应用启动时把语音服务带起来（★「启动应用默认启动服务」就是这个函数★）。"""
    from .. import service_ctl  # noqa: PLC0415

    try:
        st = service_ctl.status(settings)
        if st.running:
            return f"语音服务已经在跑（PID {st.pid}）"
        ok, msg = service_ctl.start_service(settings)
        return msg if ok else f"语音服务没起来：{msg}"
    except Exception as exc:  # noqa: BLE001 - 服务起不来也要能看界面
        log.warning(f"[应用] 启动语音服务失败（界面照常可用）：{exc}")
        return f"语音服务没起来：{exc}"


def _wait_started(server: Any, thread: threading.Thread, log: logging.Logger,
                  timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while not server.started and time.time() < deadline:
        if not thread.is_alive():
            log.warning("[应用] uvicorn 线程已退出，控制台没起来")
            return False
        time.sleep(0.05)
    if not server.started:
        log.warning(f"[应用] 控制台 {timeout:g} 秒内没起来")
        return False
    return True


def _install_exit_handlers(app_ctl: "_Application") -> dict:
    """把 Ctrl+C / Ctrl+Break / SIGTERM 变成「退出应用」。返回原处理器，退出时还回去。

    ★为什么必须自己装★：uvicorn 现在跑在**后台线程**里（主线程要让给窗口或等待），
    而它的信号处理只在主线程安装 —— 不装的话 Windows 会用默认处理器**直接把进程打死**
    （退出码 0xC000013A），连一句「正在退出」都来不及打。这是 2026-11-21 改架构后
    真机踩到的（`test_console_exit` 立刻变红）。
    """
    import signal  # noqa: PLC0415

    saved: dict = {}

    def _handler(signum, _frame) -> None:  # noqa: ANN001
        print(f"\n收到退出信号（{signum}），正在退出应用…", flush=True)
        # 不在信号处理器里干重活（停服务要等几秒）：丢给一个线程，主线程继续等 done
        threading.Thread(target=app_ctl.quit, args=(f"信号 {signum}",),
                         name="app-signal-quit", daemon=True).start()

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):   # 不在主线程 / 平台不支持
            pass
    return saved


def _restore_exit_handlers(saved: dict) -> None:
    import signal  # noqa: PLC0415

    for sig, handler in (saved or {}).items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    window: str = "",
    logger: logging.Logger | None = None,
    tray: bool = True,
    service_on_start: bool = True,
) -> int:
    """起「应用」（阻塞到退出）。返回进程退出码。

    ★这里起的不只是一个网页，是一个应用★（2026-11-21 重新定位，见 tray.py 说明）：
    应用 = 控制台窗口 + **它的托盘图标** + 被它带着的语音服务。

    ``window``：``off/headless`` 不开窗口（只有托盘，双击图标也不开窗，用浏览器看）；
    ``browser`` 开浏览器标签；``auto`` = pywebview → Edge 应用窗口 → 浏览器。

    ★线程分配（改之前先读这段）★：

    ==========  ==========================================================
    uvicorn     后台线程（它要跑 asyncio；主线程要留给窗口或等待）
    pystray     后台线程（自己的消息泵）
    窗口        pywebview 必须主线程；edge / browser 的窗口是**别的进程**，不占主线程
    主线程      pywebview 时跑窗口事件循环，否则等 ``done`` 事件
    ==========  ==========================================================
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("控制台需要 uvicorn：pip install fastapi uvicorn") from exc

    from .. import service_ctl  # noqa: PLC0415
    from .. import tray as tray_mod  # noqa: PLC0415

    log = logger or logging.getLogger("voice_loop.console")
    app = create_app(settings, log)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print("\n" + "=" * 62)
    print("  本地语音助手 · 控制台")
    print(f"  打开：{url}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  ★注意★ 绑定在 {host}：同一局域网的人也能访问并改你的日程")
    print("  关掉窗口 = 收到托盘（不退出）；退出应用用托盘菜单或 Ctrl+C")
    print("=" * 62 + "\n")

    # ★为什么要提前自己查端口★：uvicorn 撞端口时只打一行 `[Errno 10048] error while
    # attempting to bind …` 的 error 日志，然后 **`sys.exit(1)`** —— 用户看到的就是
    # 「输出了个 1，什么也没说」。这里换成人话，并且把「直接开那一个」和「换端口」都写出来。
    if port_in_use(host, port):
        print(f"★端口 {port} 已经有程序在听★ —— 多半是**应用已经在跑了**。")
        print(f"  ① 直接用现有那个：{url}")
        print(f"  ② 确实要再起一个 → 换个端口：python main.py ui --port {int(port) + 1}")
        print(f"  ③ 想看是谁占着 → PowerShell: Get-NetTCPConnection -LocalPort {int(port)} "
              f"| Select-Object OwningProcess")
        return 2

    want = (window or "").strip().lower()
    # ★三种「不开窗口」★：显式 --window off；或者压根没要窗口（--no-browser 且没指定后端）。
    # 后者很重要：`main.py ui --no-browser` 从以前到现在都表示「别弹任何东西」。
    headless = want in ("off", "none", "headless") or (not want and not open_browser)

    # ★记账★：`sessions/console.pid` 让 `main.py stop --app` 之类能认人（应用自己退出时也会清）。
    service_ctl.write_console_pid(settings, port)
    server = None
    thread: threading.Thread | None = None
    tray_obj = None
    done = threading.Event()
    prev_handlers: dict = {}
    try:
        if service_on_start:
            print(f"  语音服务：{_ensure_service(settings, log)}", flush=True)

        server = _make_server(uvicorn, app, host, port)
        thread = threading.Thread(target=server.run, name="console-uvicorn", daemon=True)
        thread.start()
        if not _wait_started(server, thread, log):
            return 1

        grace = float(getattr(getattr(settings, "app", None), "tray_stop_grace", 20.0) or 20.0)
        app_ctl = _Application(settings, log=log, server=server, done=done, grace=grace)
        # 让 HTTP 也能退出应用（`main.py stop --app` 走这条，比硬杀干净）：
        # 先停语音服务 → 关窗口 → uvicorn 收尾，一条龙。
        app.state.quit_app = lambda: app_ctl.quit("HTTP /api/app/quit")
        prev_handlers = _install_exit_handlers(app_ctl)

        win = None
        if not headless:
            prefer = want or "auto"
            win = window_mod.prepare_window(url, prefer=prefer,
                                            profile_dir=settings.sessions_dir / "window",
                                            logger=log)
            app_ctl.set_window(win)
            print(f"  窗口：{win.describe()}（关掉它 = 收到托盘，应用还在）", flush=True)
        else:
            # headless 也可以「显示窗口」：双击托盘图标就开个浏览器标签
            app_ctl.set_window(window_mod.prepare_window(url, prefer="browser", logger=log))
            print("  窗口：不开（托盘里双击图标会开浏览器标签；要应用窗口就加 --window auto）",
                  flush=True)

        if tray:
            tray_obj = tray_mod.start_app_tray(
                settings, log,
                show_window=app_ctl.show_window,
                quit_app=lambda: app_ctl.quit("托盘菜单"),
            )
            # ★成功也要说一句★：exe 是 windowed 的，日志是唯一能看见的通道 ——
            # 「没有这行」不该是判断托盘好坏的唯一线索（以前只有失败才有输出）。
            if tray_obj is None:
                print("  托盘：没起来（不影响网页；pip install pystray pillow 可修）", flush=True)
            else:
                print("  托盘：已就绪（关窗口不退出；双击图标叫回窗口）", flush=True)

        # ---- 开窗口 ----
        if win is not None:
            if win.blocks_main_thread:
                try:
                    win.run()                       # 阻塞到窗口被 destroy
                except KeyboardInterrupt:
                    app_ctl.quit("Ctrl+C")
                except Exception as exc:  # noqa: BLE001 - 窗口起不来不能变成应用起不来
                    print(f"★窗口起不来（{exc}）★ 改用浏览器；托盘双击可再试。", flush=True)
                    fallback = window_mod.prepare_window(
                        url, prefer="browser", logger=log)
                    app_ctl.set_window(fallback)
                    fallback.run()
            else:
                win.run()                           # edge/browser：开完就返回

        # ---- 等退出（托盘「退出应用」/ 关掉最后的东西 / Ctrl+C）----
        try:
            while not done.wait(0.3):
                pass
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，正在退出应用…", flush=True)
            app_ctl.quit("Ctrl+C")
    finally:
        _restore_exit_handlers(prev_handlers)
        if tray_obj is not None:
            tray_obj.stop()
        if server is not None:
            try:
                server.should_exit = True
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[应用] 让 uvicorn 收尾失败（忽略）：{exc}")
        if thread is not None:
            thread.join(timeout=8.0)
        service_ctl.clear_console_pid(settings)

    print("\n应用已退出（语音服务也已停止，要从命令行起：python main.py listen -B）")
    return 0


def _make_server(uvicorn, app, host: str, port: int):    # noqa: ANN001, ANN201
    """建 uvicorn 服务器。

    ★子类在这里现定义，不在模块顶层★：``create_app`` 不该因为「没装 uvicorn」
    就 import 不进来（自测里只用 create_app 起 App，不需要真监听端口）。

    ★为什么要接这一层★：uvicorn 的关闭顺序是

        ① 停止收新连接 → ② **等活跃连接自己结束** → ③ 跑 lifespan shutdown。

    而 SSE 是「永不结束的响应」，② 会一直等下去；等轮不到 ③ 里的 ``bus.close()``
    叫醒订阅者，结果就是用户看到的「终端 Ctrl+C 退不出、只能关网页」。
    所以在 ① 之前就先叫醒订阅者，让 SSE 干净收尾（``timeout_graceful_shutdown`` 只当兵底）。
    """

    class ConsoleServer(uvicorn.Server):
        async def shutdown(self, sockets=None):         # noqa: ANN001 - uvicorn 的签名
            try:
                app.state.ctx.bus.close()
            except Exception as exc:  # noqa: BLE001 - 关不掉也得让它继续退
                logging.getLogger("voice_loop.console").warning(
                    f"关闭实时通道失败（继续退出）：{exc}")
            await super().shutdown(sockets)

    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3.0,
    )
    return ConsoleServer(config)
