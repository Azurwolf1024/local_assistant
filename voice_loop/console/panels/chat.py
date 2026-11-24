"""面板：文字对话（在网页里打字，让助手回答并出声）。

怎么工作：把这句话当 `ask` 命令投给**服务**——服务里已经装好了技能层、工具、LLM、TTS，
所以网页打字和对着麦克风说话走的是**同一条**处理链（区别只是没有 ASR 那一步）。
答案（文字）通过回执返回，声音由服务那边直接放出来。

★为什么不在控制台里自己起一个 LLM★：那是第二个进程加载一整套模型，
还会和服务的工具/记忆状态分裂（服务记着对话历史，控制台这份不知道）。
一条链路一个状态，调试起来才不糊涂。

★聊天记录（2026-11-24 改）★
- **按角色分文件**：`sessions/console/chat/<角色id>.jsonl`，一个角色一份。
  换角色 = 换一份记录；「跟随服务当前角色」时，看的是服务此刻那个角色的记录。
- **分页**：一页 `PAGE` 条，往上翻再要更早的（`?before=<下标>`），不是一次性全灌进浏览器。
- 它仍然只是**界面用的显示记录**，不是助手的对话记忆——真正的记忆在服务的 ``llm`` 里
  （`history_turns` / `keep_all_history` 控制）。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from ...service_ctl import status as service_status
from ..registry import Panel

PANEL = Panel(
    id="chat",
    title="文字对话",
    order=60,
    hint="在网页里打字，让助手回答并念出来（每个角色一份聊天记录，往上翻加载更早的）",
    needs_service=True,
)

PAGE = 40              # 一页多少条（界面往上翻时按这个数再要一页）
MAX_PAGE = 200         # 一次最多给多少条（防手改 URL 拖垮浏览器）
_CID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    base = ctx.settings.resolve(ctx.settings.app.console_dir) / "chat"
    base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 存取
    def _cid(raw: str) -> str:
        """角色 id 白名单化，顺便防目录穿越（角色文件是用户可改的）。"""
        cid = _CID_RE.sub("_", str(raw or "").strip())[:64]
        return cid or "default"

    def _file(cid: str):
        return base / f"{cid}.jsonl"

    def _meta_file(cid: str):
        return base / f"{cid}.meta.json"

    # ------------------------------------------------------- 云端记忆里的对话
    # ★为什么还要读这里★：`data/memory/<角色>/context/segments/` 才是**同步过的**
    # 对话原文（每轮一行：ts / role / text），换设备拉回来就能看见；
    # 控制台自己那份 jsonl 只是「界面记录」（带秒数、tts 等显示用字段）。
    # 两边合起来才是完整记录：云端老记录 + 本机还没归档的新记录。
    def _segments(cid: str) -> list[dict]:
        root = ctx.settings.resolve("data/memory") / cid / "context" / "segments"
        rows: list[dict] = []
        for path in sorted(root.glob("seg-*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(row, dict):
                            continue
                        role = str(row.get("role") or "")
                        text = str(row.get("text") or "").strip()
                        if role not in ("user", "assistant") or not text:
                            continue
                        rows.append({
                            "ts": _ts_of(row.get("ts")),
                            "role": role,
                            "text": text,
                            "uid": str(row.get("uid") or ""),
                        })
            except OSError:
                continue
        return rows

    def _ts_of(raw) -> float:
        """支持 ISO（2026-11-24T10:54:58）和 'YYYY-MM-DD HH:MM:SS' 两种写法。"""
        text = str(raw or "").strip().replace("T", " ")
        if not text:
            return 0.0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(text[:19] if len(text) >= 19 else text, fmt))
            except ValueError:
                continue
        return 0.0

    def _stamp(ts: float) -> str:
        if not ts:
            return ""
        dt = datetime.fromtimestamp(ts)
        today = datetime.now().date()
        return dt.strftime("%H:%M:%S") if dt.date() == today else dt.strftime("%m-%d %H:%M")

    def _cleared_at(cid: str) -> float:
        try:
            raw = json.loads(_meta_file(cid).read_text(encoding="utf-8"))
            return float((raw or {}).get("cleared_at") or 0.0)
        except Exception:  # noqa: BLE001 - 没有/坏了都当没清过
            return 0.0

    def _merged(cid: str) -> list[dict]:
        """云端分段 + 本机界面记录 → 一条时间线（去重：同角色同文本、时间接近算同一条）。"""
        floor = _cleared_at(cid)
        segs = [s for s in _segments(cid) if s["ts"] > floor]
        local = [m for m in _read(cid) if float(m.get("ts") or 0) > floor]

        used = [False] * len(segs)
        items: list[dict] = []
        for row in segs:
            items.append({
                "ts": row["ts"],
                "at": _stamp(row["ts"]),
                "role": "me" if row["role"] == "user" else "bot",
                "text": row["text"],
                "character": "",
                "seconds": None,
                "source": "memory",
            })

        for m in local:
            m_ts = float(m.get("ts") or 0)
            m_role = "me" if m.get("role") == "me" else ("bot" if m.get("role") == "bot" else "error")
            m_text = str(m.get("text") or "").strip()
            hit = -1
            for i, s in enumerate(segs):
                if used[i]:
                    continue
                if (s["role"] == ("user" if m_role == "me" else "assistant")
                        and s["text"] == m_text and abs(s["ts"] - m_ts) <= 300):
                    hit = i
                    break
            if hit >= 0:          # 云端已有这条：把界面上的元数据补进去，不重复显示
                used[hit] = True
                for it in items:
                    if it.get("source") == "memory" and it["text"] == m_text \
                            and it["role"] == m_role and abs(it["ts"] - segs[hit]["ts"]) < 1e-6:
                        it["seconds"] = m.get("seconds")
                        it["detail"] = m.get("detail") or it.get("detail")
                        it["character"] = m.get("character") or ""
                        it["source"] = "both"
                        break
                continue
            items.append({
                "ts": m_ts,
                "at": m.get("at") or _stamp(m_ts),
                "role": m_role,
                "text": m_text,
                "character": m.get("character") or "",
                "seconds": m.get("seconds"),
                "detail": m.get("detail"),
                "source": "console",
            })

        items.sort(key=lambda r: r.get("ts") or 0)
        return items

    def _read(cid: str) -> list[dict]:
        path = _file(cid)
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # 坏行跳过，别让整份记录打不开
                    if isinstance(row, dict):
                        out.append(row)
        except OSError:
            return out
        return out

    def _write(cid: str, items: list[dict]) -> None:
        try:
            with _file(cid).open("w", encoding="utf-8") as fh:
                for row in items:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _append(cid: str, entry: dict) -> dict:
        row = dict(entry)
        row.setdefault("at", datetime.now().strftime("%H:%M:%S"))
        row.setdefault("ts", time.time())
        try:
            with _file(cid).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return row

    def _pop_last(cid: str) -> dict | None:
        items = _read(cid)
        if not items:
            return None
        row = items.pop()
        _write(cid, items)
        return row

    def _service_cid() -> str:
        """服务此刻用的是哪个角色（「跟随服务当前角色」时用它定位记录）。"""
        try:
            reply = ctx.channel.call("ping", timeout=1.5)
            if reply and getattr(reply, "ok", False) and reply.data:
                return _cid(reply.data.get("character_id") or "")
        except Exception:  # noqa: BLE001 - 服务没跑/正忙都正常
            pass
        return ""

    # ------------------------------------------------------------------ 接口
    def _name_of(cid: str) -> str:
        try:
            for char in ctx.characters().all():
                if char.id == cid:
                    return char.name
        except Exception:  # noqa: BLE001 - 只是显示用
            pass
        return ""

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
        running = service_status(ctx.settings).running
        return {
            "items": rows,
            "error": error,
            "service_running": running,
            "service_character": _service_cid() if running else "",
            "mailbox": live,
            "page": PAGE,
        }

    @app.get("/api/chat/history")
    def api_history(character: str = "", limit: int = PAGE, before: int = -1):
        """一页聊天记录（从 `before` 往前数 `limit` 条）。

        `before` 是**整个文件里的下标**（-1 = 从最新开始）；返回的 `next_before`
        直接塞给下一次请求就是「再往前一页」。文件只往后追加，所以旧下标是稳定的。
        """
        cid = _cid(character or _service_cid() or "default")
        items = _merged(cid)
        total = len(items)
        end = total if before < 0 or before > total else int(before)
        step = max(1, min(int(limit or PAGE), MAX_PAGE))
        start = max(0, end - step)
        page = items[start:end]
        return {
            "character": cid,
            "character_name": _name_of(cid),
            "items": page,
            "total": total,
            "shown": len(page),
            "before": end,
            "next_before": start,
            "has_more": start > 0,
        }

    @app.post("/api/chat/say")
    def api_say(payload: dict = Body(default={})):
        """只念不回答（短句确认用，比 ask 快得多，也不会消耗 LLM）。"""
        body = payload or {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="缺少 text")
        who = str(body.get("character") or "").strip()
        cid = _cid(who or _service_cid() or "default")
        _append(cid, {"role": "me", "text": text, "character": who, "seconds": None})
        reply = ctx.call_service(
            "say", timeout=float(body.get("timeout") or 60.0), text=text, character=who,
        )
        data = reply.data or {}
        real = _cid(data.get("character_id") or cid)
        entry = {
            "role": "me" if reply.ok else "error",
            "text": text if reply.ok else f"没念成：{reply.error}",
            "seconds": round(reply.seconds, 2),
            "character": data.get("character_name") or "",
        }
        if real != cid:                     # 实际角色和预估不同：把刚才那条挪过去
            moved = _pop_last(cid)
            if moved is not None:
                _append(real, moved)
            cid = real
        entry = _append(cid, entry)
        return {"ok": reply.ok, "error": reply.error, "character": cid,
                "seconds": round(reply.seconds, 2), "entry": entry}

    @app.post("/api/chat/ask")
    def api_ask(payload: dict = Body(default={})):
        """走完整一轮：技能/工具 → LLM → 念出来。超时给足（本地 4B 模型首字可能要几秒）。"""
        body = payload or {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="缺少 text")
        timeout = float(body.get("timeout") or 90.0)
        who = str(body.get("character") or "").strip()
        cid = _cid(who or _service_cid() or "default")
        _append(cid, {"role": "me", "text": text, "character": who, "seconds": None})
        # ★角色跟这句话一起发过去★（在同一条命令里切），而不是「先切再问」
        # ——分两次的话中间可能被语音唤醒/另一个页面换走（见 control.execute 的说明）。
        reply = ctx.call_service("ask", timeout=timeout, text=text, character=who)
        data = reply.data or {}
        real = _cid(data.get("character_id") or cid)
        if real != cid:                     # 服务实际用的角色和预估不同：把 me 那条挪过去
            moved = _pop_last(cid)
            if moved is not None:
                _append(real, moved)
            cid = real
        if reply.ok:
            entry = _append(cid, {
                "role": "bot",
                "text": reply.text or "（她没说话）",
                "character": data.get("character_name") or "",
                "seconds": round(reply.seconds, 2),
                "detail": {
                    "total_seconds": data.get("total_seconds"),
                    "first_audio": data.get("first_audio"),
                    "tts": data.get("tts"),
                    "extra": data.get("extra"),
                },
            })
        else:
            entry = _append(cid, {
                "role": "error",
                "text": ("没切成角色：" if "没有角色" in (reply.error or "")
                         else "没有回执：") + (reply.error or ""),
                "seconds": round(reply.seconds, 2),
            })
        return {"ok": reply.ok, "error": reply.error, "character": cid,
                "entry": entry, "seconds": round(reply.seconds, 2)}

    @app.post("/api/chat/clear")
    def api_clear(payload: dict = Body(default={})):
        """清掉**这个角色此刻之前的聊天记录显示**。

        现在记录会从云端记忆（`data/memory/<角色>/context/segments/`）合并出来，
        光删 jsonl 是删不干净的，所以再写一个 `cleared_at` 水位：
        早于它的记录不再显示（记忆本身不动，只是界面不展示了）。
        """
        body = payload or {}
        cid = _cid(str(body.get("character") or "") or _service_cid() or "default")
        n = len(_merged(cid))
        try:
            _file(cid).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _meta_file(cid).write_text(
                json.dumps({"cleared_at": time.time()}, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            pass
        return {"ok": True, "cleared": n, "character": cid}
