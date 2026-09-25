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
from pathlib import Path

from ..control import ControlChannel
from .. import service_ctl
from ..settings import Settings
from .bus import EventBus
from .follow import Watcher
from .registry import Panel, Registry

STATIC_DIR = Path(__file__).resolve().parent / "static"
PANELS_PACKAGE = "voice_loop.console.panels"


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

    # ------------------------------------------------------------------ 基础接口
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
            "started_at": ctx.started_at,
            "python": f"{__import__('sys').version.split()[0]}",
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


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    logger: logging.Logger | None = None,
) -> int:
    """起控制台（阻塞）。返回进程退出码。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("控制台需要 uvicorn：pip install fastapi uvicorn") from exc

    log = logger or logging.getLogger("voice_loop.console")
    app = create_app(settings, log)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print("\n" + "=" * 62)
    print("  本地语音助手 · 控制台")
    print(f"  打开：{url}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  ★注意★ 绑定在 {host}：同一局域网的人也能访问并改你的日程")
    print("  Ctrl+C 退出（不影响语音服务本身）")
    print("=" * 62 + "\n")

    # ★为什么要提前自己查端口★：uvicorn 撞端口时只打一行 `[Errno 10048] error while
    # attempting to bind …` 的 error 日志，然后 **`sys.exit(1)`** —— 用户看到的就是
    # 「输出了个 1，什么也没说」。这里换成人话，并且把「直接开那一个」和「换端口」都写出来。
    if port_in_use(host, port):
        print(f"★端口 {port} 已经有程序在听★ —— 多半是**已经有一个控制台在跑**。")
        print(f"  ① 先用浏览器打开现有那个：{url}")
        print(f"     （能打开就是它，不用再起一个；两个控制台改同一批文件也不会更安全）")
        print(f"  ② 确实要再起一个 → 换个端口：python main.py ui --port {int(port) + 1}")
        print(f"  ③ 想看是谁占着 → PowerShell: Get-NetTCPConnection -LocalPort {int(port)} "
              f"| Select-Object OwningProcess")
        return 2

    if open_browser:
        def _open() -> None:
            time.sleep(1.0)                       # 等 uvicorn 起来再开，免得看到「无法连接」
            try:
                import webbrowser  # noqa: PLC0415

                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001 - 打不开浏览器也要能把服务跑起来
                log.warning(f"没能自动打开浏览器（{exc}），请手动访问 {url}")

        threading.Thread(target=_open, name="console-open", daemon=True).start()

    print("（网页开着的时候，在终端里按 Ctrl+C 也能退出）\n", flush=True)

    # ★为什么要自己接一层 Server★：uvicorn 的关闭顺序是
    #   ① 停止收新连接 → ② **等活跃连接自己结束** → ③ 跑 lifespan shutdown。
    # 而 SSE 是「永不结束的响应」，② 会一直等下去；等轮不到 ③ 里的 `bus.close()`
    # 叫醒订阅者，结果就是用户看到的「终端 Ctrl+C 退不出、只能关网页」。
    # 所以在 ① 之前就先叫醒订阅者，让 SSE 干净收尾（timeout_graceful_shutdown 只当兵底）。
    class ConsoleServer(uvicorn.Server):
        async def shutdown(self, sockets=None):
            try:
                app.state.ctx.bus.close()
            except Exception as exc:  # noqa: BLE001 - 关不掉也得让它继续退
                log.warning(f"关闭实时通道失败（继续退出）：{exc}")
            await super().shutdown(sockets)

    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3.0,
    )
    try:
        ConsoleServer(config).run()
    except KeyboardInterrupt:      # 有些终端下 Ctrl+C 会直接抛到这里
        pass
    except SystemExit as exc:      # 竞态：刚查完端口就被别人抢了
        if exc.code not in (0, None):
            print(f"\n★控制台没能起来（退出码 {exc.code}）★ 看上面一行 uvicorn 的报错："
                  f"最常见的就是端口 {port} 被占（改用 --port {int(port) + 1} 试试）。")
            return 1
        raise
    print("\n控制台已退出（语音服务不受影响，要停服务用 python main.py stop）")
    return 0
