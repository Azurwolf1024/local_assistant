"""面板：状态概览。

一个页面回答「现在什么情况」：服务在不在跑、模型是什么、当前是谁、
今天还剩哪些提醒、信箱通不通、记忆上次同步到什么时候。这里**只读**（改数据的操作在各专门面板里），
除了四个「一键」：试听一句、弹个提醒、重载角色文件、把记忆同步到云端。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

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
                    # ★她实际用哪个嗓子★：卡片上只说 piper 声线会骗人 ——
                    # backend=api 时真正出声的是官方 TTS 的音色（tts_voice）
                    "backend": c.backend or "",
                    "tts_voice": getattr(c, "tts_voice", "") or "",
                    "tts_line": _voice_line(settings, c),
                    "engine": c.engine or getattr(settings.api, "engine", "local"),
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
            "asr": _asr_state(settings),
            "llm": {
                "model": settings.llm.model,
                "host": settings.llm.host,
                "router": getattr(settings.llm, "router", ""),
                "route": getattr(settings.llm, "route", ""),
                "temperature": settings.llm.temperature,
            },
            "api": {
                "enabled": bool(getattr(settings.api, "enabled", True)),
                "engine": getattr(settings.api, "engine", "local"),
                "model": settings.api.model,
                "base_url": settings.api.base_url,
                "key_env": settings.api.api_key_env,
                "has_key": bool(settings.api.api_key),
            },
            # ★工具从哪来★：概览只报告「有几个服务器、各自露不露」，真实清单在「工具」页
            "mcp": {
                "enabled": bool(getattr(getattr(settings, "mcp", None), "enabled", True)),
                "servers": [
                    {
                        "name": str(getattr(s, "name", "") or ""),
                        "transport": str(getattr(s, "transport", "inproc") or "inproc"),
                        "hidden": bool(getattr(s, "hidden", False)),
                        "allow": len(list(getattr(s, "tools", None) or [])),
                    }
                    for s in (getattr(getattr(settings, "mcp", None), "servers", None) or [])
                ],
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
            "memory_sync": _memory_sync_state(settings),
            "memory_dream": _dream_state(settings),
        }

    @app.post("/api/overview/dream")
    def api_dream(payload: dict = Body(default={})):
        """手动做一次梦（等价于 `python main.py dream`）。

        ★默认走规则版★：控制台这一个按钮不该悄悄花云端 token；
        要 agent 版就等夜里的调度器，或者配置文件里指定 `[memory.dream] model`。
        """
        from ...memory import MemoryHub  # noqa: PLC0415
        from ...memory.dream import Dreamer  # noqa: PLC0415

        settings = ctx.settings
        who = str((payload or {}).get("who") or "").strip() or _default_char(settings)
        try:
            hub = MemoryHub(settings)
            report = Dreamer(settings, hub, who).run(
                day=str((payload or {}).get("date") or ""),
                dry_run=bool((payload or {}).get("dry_run")),
                force=bool((payload or {}).get("force")),
                source="rule",
            )
        except Exception as exc:  # noqa: BLE001 - 控制台不能因为做梦失败就 500
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "summary": ""}
        return {"ok": report.ok, "summary": report.summary(),
                "digest": report.digest, "skipped": report.skipped[:8],
                "counts": hub.for_character(who).context.counts()}

    @app.post("/api/overview/memory-sync")
    def api_memory_sync(payload: dict = Body(default={})):
        """把记忆同步到私有仓库（见第 15 节 / voice_loop/memory/sync.py）。

        ★为什么放在概览页★：同步是「一次动作」，跟记忆本身不是一回事；
        放在概览就是让人想起来才点一下，而不是每轮对话都去碰网络。
        """
        from ...memory.sync import MemorySync  # noqa: PLC0415

        settings = ctx.settings
        body = payload or {}
        sync = MemorySync(settings)
        try:
            if bool(body.get("pull")):
                report = sync.pull(dry_run=bool(body.get("dry_run")))
            elif bool(body.get("status")):
                report = sync.status()
            else:
                report = sync.sync(dry_run=bool(body.get("dry_run")))
        except Exception as exc:  # noqa: BLE001 - 控制台不能因为 git 挂了就 500
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "summary": ""}
        return {**report.as_dict(), "summary": report.summary()}

    @app.post("/api/overview/audition")
    def api_audition(payload: dict = Body(default={})):
        """让服务念一句（复用服务里已加载的 TTS）。服务没跑就明说，不假装成功。"""
        text = str((payload or {}).get("text") or "").strip() or "你好呀，我是本地语音助手。"
        # ★给足 60 秒★：服务是懒加载的，第一次 `say` 要把声线加载进来（实测 11 秒量级，
        # 机器忙时更久）。以前给 25 秒，机器一慢就会「超时但其实念出来了」。
        timeout = float((payload or {}).get("timeout") or 60.0)
        reply = ctx.call_service("say", timeout=timeout, text=text)
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


def _voice_line(settings, char) -> str:
    """「她现在用哪个嗓子」一行字（跟角色卡/声线页同一份实现）。"""
    from ..persona_card import describe_voice  # noqa: PLC0415

    return describe_voice(settings, char)


def _memory_sync_state(settings) -> dict:
    """记忆云端同步的「现在什么情况」：配没配、上次同步到什么时候、几个角色。

    ★只读本地文件★：概览页不该为了显示一行字就去 fetch 远端（慢，还会因为断网变成红字）。
    """
    cfg = getattr(settings, "memory", None)
    want = str(getattr(cfg, "sync_dir", "") or "memory_cloud")
    root = Path(str(getattr(settings, "root", ".") or "."))
    clone = Path(want) if Path(want).is_absolute() else root / want
    manifest = clone / "manifest.json"
    out = {
        "enabled": bool(getattr(cfg, "sync_on_close", False)),
        "remote": str(getattr(cfg, "sync_remote", "") or ""),
        "dir": str(clone),
        "cloned": (clone / ".git").is_dir(),
        "updated_at": "",
        "characters": 0,
        "counts": {},
    }
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out["updated_at"] = str(data.get("updated_at") or "")
    out["counts"] = dict(data.get("counts") or {})
    out["characters"] = int(out["counts"].get("characters") or 0)
    return out


def _asr_state(settings) -> dict:
    """识别现状（★从注册表来★，不写死引擎名）：策略一行 + 每个后端的就绪情况 + 词库条数。"""
    out: dict = {"summary": "", "providers": [], "lexicon": {}, "kws": False, "cloud": False}
    try:
        from ...asr.lexicon import build_lexicon  # noqa: PLC0415
        from ...asr.registry import AsrRegistry  # noqa: PLC0415

        registry = AsrRegistry(settings)
        registry.discover()
        out["summary"] = registry.summary()
        out["spec"] = registry.default_spec().display()
        out["providers"] = [
            {"id": item["id"], "label": item["label"], "ready": item["ready"],
             "why": item["why"], "heavy": item["heavy"], "hotwords": item["hotwords"],
             "primary": item["primary"], "verify": item["verify"]}
            for item in registry.describe()
        ]
        out["missing"] = registry.missing()
        lex = build_lexicon(settings)
        out["lexicon"] = lex.stats() if lex is not None else {"total": 0}
        out["cloud"] = bool(getattr(settings.asr, "allow_cloud", False))
        out["kws"] = bool(getattr(getattr(settings.asr, "kws", None), "enabled", False))
    except Exception as exc:  # noqa: BLE001 - 概览页读不到也不该 500
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _dream_state(settings) -> dict:
    """L1 连续上下文 / 做梦的「现在什么情况」：几个角色、各有多少轮、缓存命中率。

    ★只读本地文件★（`context/state.json`），不加载模型、不碰网络。
    """
    out = {"enabled": False, "characters": [], "cache": {}, "window": ""}
    cfg = getattr(settings, "memory", None)
    if not cfg or not getattr(cfg, "enabled", True):
        return out
    dcfg = getattr(cfg, "dream", None)
    ctx_cfg = getattr(cfg, "context", None)
    out["enabled"] = bool(getattr(dcfg, "enabled", False)) and \
        bool(getattr(ctx_cfg, "enabled", True))
    out["window"] = str(getattr(dcfg, "window", "") or "")
    out["night_min_tokens"] = int(getattr(dcfg, "night_min_tokens", 0) or 0)
    out["hard_tokens"] = int(getattr(dcfg, "hard_tokens", 0) or 0)
    want = str(getattr(cfg, "dir", "") or "")
    root = Path(str(getattr(settings, "root", ".") or "."))
    base = Path(want) if Path(want).is_absolute() else root / (want or "data/memory")
    total_prompt = total_hit = 0
    try:
        rooms = sorted(p for p in base.iterdir() if (p / "context").is_dir())
    except OSError:
        return out
    for room in rooms:
        try:
            state = json.loads((room / "context" / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        today = (state.get("cache") or {}).get(datetime.now().date().isoformat()) or {}
        prompt = int(today.get("prompt") or 0)
        hit = int(today.get("hit") or 0)
        total_prompt += prompt
        total_hit += hit
        segs = len(list((room / "context" / "segments").glob("seg-*.jsonl")))
        out["characters"].append({
            "id": room.name,
            "segments": segs,
            "open_turns": int(state.get("turns") or 0),
            "last_dream": str(state.get("last_dream_day") or ""),
            "prompt": prompt,
            "hit": hit,
        })
    out["cache"] = {"prompt": total_prompt, "hit": total_hit,
                    "ratio": round(total_hit / total_prompt, 3) if total_prompt else 0.0}
    return out


def _default_char(settings) -> str:
    """默认角色（跟助手的 default 一致：先看 [persona] default，再看索引的 default）。"""
    who = str(getattr(getattr(settings, "persona", None), "default", "") or "").strip()
    if who:
        return who
    try:
        from ...persona import CharacterRegistry  # noqa: PLC0415

        char = CharacterRegistry(settings.resolve(settings.persona.file)).default()
        return char.id if char else "default"
    except Exception:  # noqa: BLE001
        return "default"


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
