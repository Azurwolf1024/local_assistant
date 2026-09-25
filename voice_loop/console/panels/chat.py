"""面板：文字对话（在网页里打字，让助手回答并出声）。

怎么工作：把这句话当 `ask` 命令投给**服务**——服务里已经装好了技能层、工具、LLM、TTS，
所以网页打字和对着麦克风说话走的是**同一条**处理链（区别只是没有 ASR 那一步）。
答案（文字）通过回执返回，声音由服务那边直接放出来。

★为什么不在控制台里自己起一个 LLM★：那是第二个进程加载一整套模型，
还会和服务的工具/记忆状态分裂（服务记着对话历史，控制台这份不知道）。
一条链路一个状态，调试起来才不糊涂。

历史记录只存在**控制台进程的内存**里（刷新就没了）：它是界面用的显示记录，
不是助手的对话记忆——真正的记忆在服务的 ``llm`` 里（``history_turns`` 控制）。
"""

from __future__ import annotations

from datetime import datetime

from ...service_ctl import status as service_status
from ..registry import Panel

PANEL = Panel(
    id="chat",
    title="文字对话",
    order=60,
    hint="在网页里打字，让助手回答并念出来（需要服务在跑）",
    needs_service=True,
)

MAX_HISTORY = 100


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    history: list[dict] = ctx.cache.setdefault("chat_history", [])

    @app.get("/api/chat/characters")
    def api_characters():
        """给「用哪个角色回答」的下拉框用（顺带把服务当前是谁也报一下）。"""
        rows = []
        error = ""
        try:
            for char in ctx.characters().all():
                rows.append({
                    "id": char.id,
                    "name": char.name,
                    "title": char.title or "",
                    "enabled": bool(char.enabled),
                })
        except Exception as exc:  # noqa: BLE001 - 角色文件坏了也不该让聊天页打不开
            error = f"{type(exc).__name__}: {exc}"
        live = ctx.channel.status()
        return {
            "items": rows,
            "error": error,
            "service_running": service_status(ctx.settings).running,
            "mailbox": live,
        }

    @app.get("/api/chat/history")
    def api_history():
        return {"items": history[-MAX_HISTORY:], "count": len(history)}

    @app.post("/api/chat/say")
    def api_say(payload: dict = Body(default={})):
        """只念不回答（短句确认用，比 ask 快得多，也不会消耗 LLM）。"""
        body = payload or {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="缺少 text")
        reply = ctx.call_service(
            "say",
            timeout=float(body.get("timeout") or 60.0),
            text=text,
            character=str(body.get("character") or ""),
        )
        entry = {
            "at": datetime.now().strftime("%H:%M:%S"),
            "role": "me" if reply.ok else "error",
            "text": text if reply.ok else f"没念成：{reply.error}",
            "seconds": round(reply.seconds, 2),
            "character": reply.data.get("character_name") if reply.data else "",
        }
        history.append(entry)
        return {"ok": reply.ok, "error": reply.error, "seconds": round(reply.seconds, 2), "entry": entry}

    @app.post("/api/chat/ask")
    def api_ask(payload: dict = Body(default={})):
        """走完整一轮：技能/工具 → LLM → 念出来。超时给足（本地 4B 模型首字可能要几秒）。"""
        body = payload or {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="缺少 text")
        timeout = float(body.get("timeout") or 90.0)
        who = str(body.get("character") or "").strip()
        history.append({
            "at": datetime.now().strftime("%H:%M:%S"),
            "role": "me",
            "text": text,
            "character": who,
            "seconds": None,
        })
        # ★角色跟这句话一起发过去★（在同一条命令里切），而不是「先切再问」
        # ——分两次的话中间可能被语音唤醒/另一个页面换走（见 control.execute 的说明）。
        reply = ctx.call_service("ask", timeout=timeout, text=text, character=who)
        if reply.ok:
            entry = {
                "at": datetime.now().strftime("%H:%M:%S"),
                "role": "bot",
                "text": reply.text or "（她没说话）",
                "character": reply.data.get("character_name") or "",
                "seconds": round(reply.seconds, 2),
                "detail": {
                    "total_seconds": reply.data.get("total_seconds"),
                    "first_audio": reply.data.get("first_audio"),
                    "character": reply.data.get("character_name"),
                    "tts": reply.data.get("tts"),
                    "extra": reply.data.get("extra"),
                },
            }
        else:
            entry = {
                "at": datetime.now().strftime("%H:%M:%S"),
                "role": "error",
                "text": ("没切成角色：" if "没有角色" in (reply.error or "") else "没有回执：") + (reply.error or ""),
                "seconds": round(reply.seconds, 2),
            }
        history.append(entry)
        del history[: max(0, len(history) - MAX_HISTORY)]
        return {"ok": reply.ok, "error": reply.error, "entry": entry, "seconds": round(reply.seconds, 2)}

    @app.post("/api/chat/clear")
    def api_clear():
        """只清**界面上的**记录；服务那边的对话记忆要用语音说「忘掉刚才」才清。"""
        n = len(history)
        history.clear()
        return {"ok": True, "cleared": n}
