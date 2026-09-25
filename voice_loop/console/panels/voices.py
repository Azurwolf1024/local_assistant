"""面板：角色与声线。

能做的事：看每个角色的人设要点、唤醒词、参考音频与专属模型目录；
**切过去**（服务里真的换声线）；**试听一句**（用服务已加载的 TTS 念出来）。

★真正出声的一定是服务★：控制台自己加载 ZipVoice 要好几百 MB 到几 GB，
而且两个进程抢同一张声卡没好处。所以这里全部走信箱（``voice_loop/control.py``），
服务没在跑时如实报错，不假装成功。
"""

from __future__ import annotations

from ..registry import Panel

PANEL = Panel(
    id="voices",
    title="角色 / 声线",
    order=50,
    hint="看角色、切声线、试听（需要服务在跑）",
    needs_service=True,
)


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    def character_rows() -> list[dict]:
        registry = ctx.characters()
        default_id = str(ctx.settings.persona.default or "")
        resolved = registry.get(default_id) or registry.default()
        resolved_id = getattr(resolved, "id", "") or default_id
        rows = []
        for char in registry.all():
            ref = str(getattr(char, "voice_ref", "") or "")
            ref_path = ctx.settings.resolve(ref) if ref else None
            ref_files = []
            if ref_path is not None and ref_path.parent.is_dir():
                ref_files = sorted(
                    f"{p.relative_to(ctx.root).as_posix()}"
                    for p in ref_path.parent.glob("*.wav")
                )
            rows.append({
                "id": char.id,
                "name": char.name,
                "title": char.title,
                "background": (char.background or "")[:400],
                "enabled": bool(char.enabled),
                "is_default": bool(char.default) or char.id == resolved_id,
                "user_title": char.user_title,
                "wake_words": list(char.wake_words or []),
                "aliases": {k: list(v or []) for k, v in (char.aliases or {}).items()},
                "ack": char.ack or "",
                "style": list(char.style or []),
                "voice": char.voice or "",
                "voice_ref": ref,
                "voice_ref_exists": bool(ref_path and ref_path.exists()),
                "voice_ref_text": char.voice_ref_text or "",
                "voice_model": char.voice_model or "",
                "voice_model_exists": bool(
                    char.voice_model and ctx.settings.resolve(char.voice_model).is_dir()
                ),
                "voice_dir": char.voice_dir or "",
                "ref_candidates": ref_files[:40],
                "lines": [{"scene": ln.get("scene", ""), "text": ln.get("text", "")}
                          for ln in (char.lines or [])][:8],
                "notes": char.notes or "",
            })
        return rows

    @app.get("/api/voices")
    def api_list():
        settings = ctx.settings
        try:
            chars = character_rows()
            default_name = next((c["name"] for c in chars if c["is_default"]), "")
            error = ""
        except Exception as exc:  # noqa: BLE001 - 角色文件坏了也要能打开这一页
            chars, default_name, error = [], "", f"{type(exc).__name__}: {exc}"
        return {
            "characters": chars,
            "error": error,
            "default": settings.persona.default,
            "resolved_default": default_name or settings.persona.default,
            "persona_file": settings.persona.file,
            "tts": {
                "backend": settings.tts.backend,
                "clone_dir": settings.tts.clone_dir,
                "clone_audio": settings.tts.clone_audio,
                "clone_steps": settings.tts.clone_steps,
                "model": settings.tts.model,
            },
            "mailbox": ctx.channel.status(),
        }

    @app.post("/api/voices/switch")
    def api_switch(payload: dict = Body(default={})):
        """切声线：交给服务做（它持有引擎），并等回执确认到底切没切。"""
        cid = str((payload or {}).get("id") or "").strip()
        if not cid:
            raise HTTPException(status_code=400, detail="缺少 id")
        reply = ctx.call_service("character", timeout=float((payload or {}).get("timeout") or 20.0), id=cid)
        return {
            "ok": reply.ok,
            "message": reply.text if reply.ok else reply.error,
            "data": reply.data,
            "seconds": round(reply.seconds, 2),
        }

    @app.post("/api/voices/audition")
    def api_audition(payload: dict = Body(default={})):
        """试听：先切（可选）再念一句。两步都走信箱，每一步都要回执。"""
        body = payload or {}
        cid = str(body.get("id") or "").strip()
        text = str(body.get("text") or "").strip() or "你好呀，我是本地语音助手。"
        switched = None
        if cid and body.get("switch", True):
            reply = ctx.call_service("character", timeout=30.0, id=cid)
            switched = {"ok": reply.ok, "message": reply.text if reply.ok else reply.error}
            if not reply.ok:
                return {"ok": False, "error": "切换失败：" + reply.error, "switch": switched}
        # 切声线后 TTS 要卸载重载 + 念一句，给足时间（实测首次 10 秒量级）
        say = ctx.call_service("say", timeout=float(body.get("timeout") or 60.0), text=text)
        return {
            "ok": say.ok,
            "error": say.error,
            "text": say.text,
            "seconds": round(say.seconds, 2),
            "switch": switched,
            "data": say.data,
        }

    @app.get("/api/voices/lines")
    def api_lines(cid: str = ""):
        """拿某个角色的示例台词，方便一键试听（听的要是她真会说的话）。"""
        for row in character_rows():
            if row["id"] == cid:
                return {"id": cid, "lines": row["lines"], "wake_words": row["wake_words"]}
        raise HTTPException(status_code=404, detail=f"没有角色 {cid!r}")
