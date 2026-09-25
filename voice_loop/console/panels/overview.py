"""面板：状态概览。

一个页面回答「现在什么情况」：服务在不在跑、模型是什么、当前是谁、
今天还剩哪些提醒、信箱通不通。这里**只读**（改数据的操作在各专门面板里），
除了三个「一键」：试听一句、弹个提醒、重载角色文件。
"""

from __future__ import annotations

from datetime import datetime

from ...service_ctl import status as service_status
from ..registry import Panel

PANEL = Panel(
    id="overview",
    title="概览",
    order=10,
    hint="服务状态、当前角色、今天的提醒",
)


def register(app, ctx) -> None:
    from fastapi import Body  # noqa: PLC0415

    @app.get("/api/overview")
    def api_overview():
        settings = ctx.settings
        status = service_status(settings)
        today = _today_events(ctx)
        mailbox = ctx.channel.status()

        characters = []
        default_id = ""
        try:
            registry = ctx.characters()
            current = registry.get(settings.persona.default) or registry.default()
            default_id = getattr(current, "id", "") or ""
            characters = [
                {
                    "id": c.id,
                    "name": c.name,
                    "title": c.title,
                    "enabled": c.enabled,
                    "is_default": bool(c.default) or (current is not None and c.id == current.id),
                    "wake_words": list(c.wake_words or []),
                    "voice": c.voice or "",
                    "voice_ref": c.voice_ref or "",
                    "voice_model": c.voice_model or "",
                }
                for c in registry.all()
            ]
        except Exception as exc:  # noqa: BLE001 - 角色文件坏了不该让概览页白屏
            characters = [{"error": f"{type(exc).__name__}: {exc}"}]

        live = _live_status(ctx)
        return {
            "started_at": ctx.started_at,
            "service": status.as_dict(),
            "mailbox": mailbox,
            "live": live,
            "tts": {
                "backend": settings.tts.backend,
                "model": settings.tts.model,
                "clone_dir": settings.tts.clone_dir,
                "clone_steps": settings.tts.clone_steps,
                "out_tilt": [settings.tts.out_tilt_hz, settings.tts.out_tilt_db],
                "pitch_guard": [settings.tts.pitch_guard_st, settings.tts.pitch_guard_tries],
                "text_guard": settings.tts.text_guard_min,
                "trim_min_gap_ms": settings.tts.trim_min_gap_ms,
            },
            "asr": {
                "strategy": settings.asr.strategy,
                "sensevoice": settings.asr.sensevoice_model,
                "whisper": settings.asr.whisper_model,
            },
            "llm": {
                "model": settings.llm.model,
                "host": settings.llm.host,
                "router": getattr(settings.llm, "router", ""),
                "route": getattr(settings.llm, "route", ""),
                "temperature": settings.llm.temperature,
            },
            "persona": {
                "default": settings.persona.default,
                "resolved_default": default_id,
                "file": settings.persona.file,
                "characters": characters,
            },
            "events": {
                "today": today,
                "total": len(ctx.events().load()),
                "due_next": _next_up(ctx),
            },
            "paths": {
                "root": str(ctx.root),
                "log": str(status.log_file),
                "console_dir": str(ctx.channel.root),
            },
        }

    @app.post("/api/overview/audition")
    def api_audition(payload: dict = Body(default={})):
        """让服务念一句（复用服务里已加载的 TTS）。服务没跑就明说，不假装成功。"""
        text = str((payload or {}).get("text") or "").strip() or "你好呀，我是本地语音助手。"
        reply = ctx.call_service("say", timeout=float((payload or {}).get("timeout") or 25.0), text=text)
        return {
            "ok": reply.ok,
            "text": reply.text,
            "error": reply.error,
            "seconds": round(reply.seconds, 2),
            "data": reply.data,
        }

    @app.post("/api/overview/toast")
    def api_toast(payload: dict = Body(default={})):
        title = str((payload or {}).get("title") or "控制台")
        text = str((payload or {}).get("text") or "这是一条测试提醒")
        reply = ctx.call_service("toast", timeout=12.0, title=title, text=text)
        return {"ok": reply.ok, "error": reply.error, "message": reply.text or reply.error}

    @app.post("/api/overview/ping")
    def api_ping():
        """问服务「你是谁、现在什么状态」——比看 pid 可靠（进程活着≠服务健康）。"""
        reply = ctx.call_service("ping", timeout=8.0)
        return {"ok": reply.ok, "error": reply.error, "seconds": round(reply.seconds, 2), "data": reply.data}


def _live_status(ctx) -> dict:
    """服务侧的实时状态：只报告「最近一次 ping 的结果」，不在这里发起 ping（太慢）。

    前端加载时会顺手打一次 `/api/overview/ping`，结果缓存在 ctx.cache 里。
    """
    return dict(ctx.cache.get("last_ping") or {})


def _today_events(ctx) -> list[dict]:
    """今天的事件（日程 + 闹钟混在一起，本来就是一种东西）。"""
    now = datetime.now()
    day = now.date()
    out = []
    for item in ctx.events().load():
        when = _start_of(item, day)
        if when is None:
            continue
        if when.date() != day and not _recurs_today(item, day):
            continue
        out.append(
            {
                "id": item.get("id"),
                "title": item.get("title") or "闹钟",
                "category": item.get("category") or "",
                "time": when.strftime("%H:%M"),
                "at": when.strftime("%Y-%m-%d %H:%M"),
                "repeat": item.get("repeat") or "once",
                "done": _is_done(item, when),
                "location": item.get("location") or "",
            }
        )
    out.sort(key=lambda x: x["time"])
    return out


def _next_up(ctx) -> dict | None:
    now = datetime.now()
    best = None
    for item in ctx.events().load():
        when = _start_of(item, now.date())
        if when is None or when < now:
            continue
        if best is None or when < best[1]:
            best = (item, when)
    if best is None:
        return None
    item, when = best
    return {
        "id": item.get("id"),
        "title": item.get("title") or "闹钟",
        "at": when.strftime("%Y-%m-%d %H:%M"),
        "in_minutes": int((when - now).total_seconds() // 60),
    }


def _start_of(item: dict, day) -> datetime | None:
    from ...events import parse_dt  # noqa: PLC0415

    if item.get("start"):
        return parse_dt(item["start"])
    time_text = str(item.get("time") or "").strip()
    if not time_text:
        return None
    try:
        hour, minute = (int(x) for x in time_text.split(":")[:2])
    except (ValueError, TypeError):
        return None
    return datetime(day.year, day.month, day.day, hour, minute)


def _recurs_today(item: dict, day) -> bool:
    from ...events import repeat_of  # noqa: PLC0415

    kind = repeat_of(item)
    if kind == "once":
        return False
    if kind == "daily":
        return True
    if kind == "weekdays":
        return day.weekday() < 5
    if kind == "weekly":
        return int(item.get("weekday") or 0) % 7 == day.weekday()
    if kind == "monthly":
        return int(item.get("day") or 0) == day.day
    if kind == "yearly":
        return int(item.get("month") or 0) == day.month and int(item.get("day") or 0) == day.day
    return False


def _is_done(item: dict, when: datetime) -> bool:
    state = item.get("state") or {}
    key = when.strftime("%Y-%m-%d %H:%M")
    return key in [str(x) for x in (state.get("done") or [])]
