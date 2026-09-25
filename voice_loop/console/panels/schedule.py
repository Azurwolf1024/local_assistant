"""面板：日程 / 闹钟（同一张表，本项目里它们本来就是一种东西）。

★这里的「加一条」跟语音里说的走**同一套解析器**★（``voice_loop/event_text.py``）：
所以网页里打「明天下午三点组会，提前半小时」和对着麦克风说这句，落地的事件完全一样，
文案也是同一句（``render_added``）。不自己重写一份解析，就不会出现「网页能懂、语音不懂」。

写入一律走 :class:`~voice_loop.events.EventStore`（拿它的锁与 id 规则），
不直接写 JSON 文件——服务那边的提醒线程也在改这个文件（见 store.py 顶部说明）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..registry import Panel

PANEL = Panel(
    id="schedule",
    title="日程 / 闹钟",
    order=20,
    hint="查看、增删改；一句话添加；周视图",
)

CATEGORIES = [
    {"value": "", "label": "（无标签）"},
    {"value": "course", "label": "课程"},
    {"value": "meeting", "label": "会议"},
    {"value": "task", "label": "任务"},
    {"value": "activity", "label": "活动"},
]

REPEATS = [
    {"value": "once", "label": "只一次（闹钟）"},
    {"value": "daily", "label": "每天"},
    {"value": "weekdays", "label": "每个工作日"},
    {"value": "weekly", "label": "每周"},
    {"value": "biweekly", "label": "每两周"},
    {"value": "monthly", "label": "每月"},
    {"value": "yearly", "label": "每年"},
    {"value": "interval", "label": "按间隔"},
]

WEEKDAYS = [
    {"value": i, "label": f"周{'一二三四五六日'[i]}"} for i in range(7)
]


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    from ... import event_text, events  # noqa: PLC0415

    # ------------------------------------------------------------------ 读
    @app.get("/api/events/schema")
    def api_schema():
        """表单用的选项（前端不写死，以后加周期类型只改这里）。"""
        return {"repeats": REPEATS, "categories": CATEGORIES, "weekdays": WEEKDAYS}

    @app.get("/api/events")
    def api_list(q: str = "", category: str = "", kind: str = "all"):
        """全部事件 + 计算出来的「下一次什么时候、还剩多久」。"""
        store = ctx.events()
        now = datetime.now()
        out = []
        for item in store.load():
            occ = events.first_after(item, now - timedelta(seconds=1))
            text = events.describe(item)
            if q and q not in text:
                continue
            if category and str(item.get("category") or "") != category:
                continue
            repeating = events.has_repeat(item)
            if kind == "alarm" and repeating:
                continue
            if kind == "repeat" and not repeating:
                continue
            out.append({
                "id": item.get("id"),
                "title": events.display_title(item),
                "raw_title": events.title_of(item),
                "category": item.get("category") or "",
                "start": item.get("start") or "",
                "time": item.get("time") or "",
                "repeat": events.repeat_of(item),
                "repeat_text": events.repeat_text(item),
                "weekday": item.get("weekday"),
                "leads": events.leads_of(item),
                "duration_minutes": int(item.get("duration_minutes") or 0),
                "location": item.get("location") or "",
                "note": item.get("note") or "",
                "until": item.get("until") or "",
                "chain": item.get("chain") or None,
                "describe": text,
                "repeating": repeating,
                "needs_confirm": events.needs_confirm(item),
                "next_at": occ.strftime("%Y-%m-%d %H:%M") if occ else "",
                "in_minutes": int((occ - now).total_seconds() // 60) if occ else None,
                "skipped": events.skipped_of(item),
                "state": events.state_of(item),
                "item": item,          # 原样给前端（编辑表单要回填）
            })
        out.sort(key=lambda x: (x["next_at"] or "9999", str(x["id"])))
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "items": out,
            # ★手工编辑过 events.json 会留下没有 id 的条目★：那种条目改不了也删不了
            # （按 id 找），界面会提示点「整理数据」把它们补上 id。
            "missing_id": sum(1 for row in out if row["id"] is None),
        }

    @app.post("/api/events/tidy")
    def api_tidy():
        """给缺 id 的条目补上 id（就是拿 EventStore.save 过一遍）。

        ``EventStore.save`` 会给缺 id 的条目发新 id（见 events.py 里的注释：
        实测跳过这一步时，一批没 id 的条目会被当成同一个 None，一条指令就把整片删了）。
        """
        store = ctx.events()
        with store.locked():
            items = store.load(force=True)
            before = sum(1 for it in items if it.get("id") is None)
            store.save(items)                      # normalize + 补 id
            after = sum(1 for it in store.load(force=True) if it.get("id") is None)
        return {"ok": True, "fixed": before - after, "before": before}

    @app.get("/api/events/week")
    def api_week(start: str = "", days: int = 7):
        """周视图：从 start（默认今天）起 days 天，每天有哪些发生。"""
        store = ctx.events()
        try:
            base = datetime.strptime(start, "%Y-%m-%d").date() if start else datetime.now().date()
        except ValueError:
            raise HTTPException(status_code=400, detail="start 要写成 YYYY-MM-DD") from None
        days = max(1, min(31, int(days)))
        window_start = datetime.combine(base, datetime.min.time())
        window_end = window_start + timedelta(days=days)
        buckets = {i: [] for i in range(days)}
        for item in store.load():
            for occ in events.occurrences(item, window_start - timedelta(days=1), limit=40):
                if not (window_start <= occ < window_end):
                    continue
                buckets[(occ.date() - base).days].append({
                    "id": item.get("id"),
                    "title": events.display_title(item),
                    "time": occ.strftime("%H:%M"),
                    "at": occ.strftime("%Y-%m-%d %H:%M"),
                    "category": item.get("category") or "",
                    "duration_minutes": int(item.get("duration_minutes") or 0),
                    "done": events.is_done(item, occ),
                })
        for v in buckets.values():
            v.sort(key=lambda x: x["time"])
        today = datetime.now().date()
        return {
            "base": base.isoformat(),
            "days": [
                {
                    "date": (base + timedelta(days=i)).isoformat(),
                    "weekday": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
                    "label": "周" + "一二三四五六日"[(base + timedelta(days=i)).weekday()],
                    "is_today": (base + timedelta(days=i)) == today,
                    "items": buckets[i],
                }
                for i in range(days)
            ],
        }

    # ------------------------------------------------------------------ 增
    @app.post("/api/events")
    def api_add(payload: dict = Body(default={})):
        """两种加法：``{"text": "明天下午三点组会，提前半小时"}`` 或 ``{"fields": {...}}``。"""
        body = payload or {}
        now = datetime.now()
        text = str(body.get("text") or "").strip()
        if text:
            fields = event_text.extract(text, now)
            item = event_text.to_item(fields)
            if not item.get("title") and not body.get("allow_empty_title"):
                # 只说「定个闹钟」也不是错——但网页里得让用户知道标题是空的
                item["title"] = ""
        elif isinstance(body.get("fields"), dict):
            item = _item_from_fields(body["fields"])
        else:
            raise HTTPException(status_code=400, detail="要么给 text（一句话），要么给 fields（表单）")

        store = ctx.events()
        with store.locked():                      # ★读-改-写整段持锁★（服务也在写这个文件）
            saved = store.append(item)
        return {"ok": True, "item": saved, "sentence": _sentence(events, event_text, saved, now)}

    # ------------------------------------------------------------------ 改
    @app.patch("/api/events/{eid}")
    def api_update(eid: str, payload: dict = Body(default={})):
        store = ctx.events()
        old = store.by_id(eid)
        if old is None:
            raise HTTPException(status_code=404, detail=f"没有 id={eid} 的事件")
        body = dict(payload or {})
        confirm = bool(body.pop("confirm", False))
        if events.needs_confirm(old) and not confirm:
            raise HTTPException(
                status_code=409,
                detail="这是一条重复事件，改动会影响往后每一次，需要确认（confirm=1）",
            )
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else body
        patch = _patch_from_fields(fields)
        if not patch:
            raise HTTPException(status_code=400, detail="没有可改的字段")
        updated = store.update_by_id(eid, **patch)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"没有 id={eid} 的事件")
        now = datetime.now()
        return {"ok": True, "item": updated, "sentence": _sentence(events, event_text, updated, now, changed=True)}

    @app.delete("/api/events/{eid}")
    def api_delete(eid: str, confirm: bool = False):
        store = ctx.events()
        item = store.by_id(eid)
        if item is None:
            raise HTTPException(status_code=404, detail=f"没有 id={eid} 的事件")
        if events.needs_confirm(item) and not confirm:
            raise HTTPException(
                status_code=409,
                detail=f"「{events.display_title(item)}」是重复事件（{events.repeat_text(item)}），"
                       "删掉会影响往后每一次，需要确认（confirm=1）",
            )
        with store.locked():
            removed = store.remove_where(lambda it: str(it.get("id")) == str(eid))
        return {"ok": bool(removed), "removed": removed[0] if removed else None}

    # ------------------------------------------------------------------ 单次动作
    @app.post("/api/events/{eid}/skip")
    def api_skip(eid: str, payload: dict = Body(default={})):
        """跳过**某一次**（不是删事件）：下周三不去上课那种。"""
        return _single_shot(ctx, events, eid, payload, action="skip")

    @app.post("/api/events/{eid}/done")
    def api_done(eid: str, payload: dict = Body(default={})):
        """标记某一次已完成（事件链靠它：「A 完成后再提醒 B」）。"""
        return _single_shot(ctx, events, eid, payload, action="done")

    @app.post("/api/events/{eid}/undone")
    def api_undone(eid: str, payload: dict = Body(default={})):
        return _single_shot(ctx, events, eid, payload, action="undone")


# ---------------------------------------------------------------- 内部工具


def _single_shot(ctx, events, eid: str, payload: dict, *, action: str) -> dict:
    """skip / done / undone 共用的实现（都是「改某一个发生时刻的状态」）。"""
    from fastapi import HTTPException  # noqa: PLC0415

    store = ctx.events()
    item = store.by_id(eid)
    if item is None:
        raise HTTPException(status_code=404, detail=f"没有 id={eid} 的事件")
    now = datetime.now()
    at = events.parse_dt((payload or {}).get("at")) or events.first_after(
        item, now - timedelta(minutes=5)
    ) or events.parse_dt(item.get("start"))
    if at is None:
        raise HTTPException(status_code=400, detail="这条事件没有可操作的时间点")

    state = dict(item.get("state") or {})
    key = at.strftime("%Y-%m-%d %H:%M:%S")
    day_key = at.strftime("%Y-%m-%d")

    if action == "skip":
        days = [d for d in (state.get("skipped") or []) if str(d) != day_key]
        days.append(day_key)
        state["skipped"] = days[-40:]
        text = f"已经跳过 {day_key} 这一次"
    elif action == "done":
        done = [d for d in (state.get("done") or []) if str(d) != key]
        done.append(key)
        state["done"] = done[-40:]
        text = f"已标记 {key} 完成"
    else:
        state["done"] = [d for d in (state.get("done") or []) if str(d) != key]
        text = f"已取消 {key} 的完成标记"

    store.update_by_id(eid, state=state)
    return {"ok": True, "at": key, "message": text, "state": state}


def _item_from_fields(fields: dict) -> dict:
    """表单字段 → 事件条目（走 new_event，保持文件干净：空值不写进去）。"""
    from ... import events  # noqa: PLC0415

    start = str(fields.get("start") or "").strip()
    if start and len(start) == 16:              # 前端 datetime-local 给的是 2026-09-25T15:30
        start = start.replace("T", " ") + ":00"
    repeat = str(fields.get("repeat") or "once").strip() or "once"
    leads = fields.get("remind_before")
    if isinstance(leads, str):
        leads = [int(x) for x in leads.replace("，", ",").split(",") if x.strip().lstrip("-").isdigit()]
    return events.new_event(
        str(fields.get("title") or "").strip(),
        start=start,
        repeat=repeat,
        remind_before=[int(x) for x in (leads or [0])],
        duration_minutes=int(fields.get("duration_minutes") or 0),
        location=str(fields.get("location") or "").strip(),
        note=str(fields.get("note") or "").strip(),
        category=str(fields.get("category") or "").strip(),
        until=str(fields.get("until") or "").strip(),
        weekday=_int_or_none(fields.get("weekday")),
        month=_int_or_none(fields.get("month")),
        day=_int_or_none(fields.get("day")),
        time=str(fields.get("time") or "").strip(),
    )


def _patch_from_fields(fields: dict) -> dict:
    """编辑时的改动集。**空串会被 EventStore.normalize 当成「删掉这个字段」**。"""
    out: dict = {}
    for key in ("title", "category", "location", "note", "until", "time", "repeat", "start"):
        if key in fields:
            value = fields[key]
            if key == "start" and isinstance(value, str) and len(value) == 16:
                value = value.replace("T", " ") + ":00"
            out[key] = "" if value is None else str(value)
    for key in ("duration_minutes",):
        if key in fields:
            out[key] = int(fields[key] or 0)
    if "weekday" in fields:
        out["weekday"] = _int_or_none(fields["weekday"])
    if "remind_before" in fields:
        leads = fields["remind_before"]
        if isinstance(leads, str):
            leads = [x.strip() for x in leads.replace("，", ",").split(",")]
        out["remind_before"] = [int(x) for x in (leads or []) if str(x).lstrip("-").isdigit()]
    return out


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sentence(events, event_text, item: dict, now: datetime, *, changed: bool = False) -> str:
    """给用户看的一句话（★跟语音回复用同一个 render★，两边说法一致）。"""
    start = events.first_after(item, now - timedelta(minutes=5)) or events.parse_dt(item.get("start"))
    if start is None:
        return events.describe(item)
    try:
        return event_text.render_added(item, start, now, changed=changed)
    except Exception:  # noqa: BLE001 - 文案生成失败不影响数据
        return events.describe(item)
