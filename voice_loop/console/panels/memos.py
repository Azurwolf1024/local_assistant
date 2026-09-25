"""面板：备忘（``data/memos.json``）。

备忘比事件简单：就是一句话 + 做完没做完。所以这里**没有**编辑表单，
只有「加 / 勾掉 / 删」——加的时候复用语音那套清洗（``event_text.clean_memo_content``），
这样「记一下买牛奶」在网页里和对着麦克风说，落到文件里的都是「买牛奶」。

★关于下面那个触发词正则的重复★：``voice_loop/skills.py`` 里有一份一模一样的
（``TRIGGER_MEMO_ADD``），但 skills 会连带 import ``vision``（Pillow / OpenCV），
控制台不该为了剥两个词就把图像库拉起来。所以这里自带一份，并且用
``scripts/test_console.py`` 拿真实 skills 的正则对样本句逐条比对——
**两边一旦跑偏，测试会失败**，不会静静发展成「网页认识的词和语音不一样」。
"""

from __future__ import annotations

import re

from ..registry import Panel

# 与 skills.TRIGGER_MEMO_ADD 同义（只取「剥触发词」这一件事），见模块顶部说明。
_MEMO_TRIGGER = re.compile(
    r"(?:记\s*[一以衣]?\s*[下住录哈吓夏]|^记得|记住|记录(?:一下)?|备忘(?!录)(?:一下)?|mark)"
    r"\s*[:：,，]?\s*",
    re.IGNORECASE,
)

PANEL = Panel(id="memos", title="备忘", order=30, hint="一句话备忘，勾掉/删除")


def strip_trigger(text: str) -> str:
    """把「记一下 / 备忘一下 / 记住」这类开头弄掉（只弄开头）。"""
    return _MEMO_TRIGGER.sub("", text or "", count=1).strip()


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    def load_rows() -> list[dict]:
        store = ctx.memos()
        rows = []
        for index, item in enumerate(store.load(), start=1):
            rows.append({
                "index": index,               # 1 起：JsonStore 的改/删都按这个序号
                "id": item.get("id"),
                "content": item.get("content") or "",
                "done": bool(item.get("done")),
                "created_at": item.get("created_at") or "",
            })
        return rows

    @app.get("/api/memos")
    def api_list():
        rows = load_rows()
        return {
            "items": rows,
            "open": sum(1 for r in rows if not r["done"]),
            "done": sum(1 for r in rows if r["done"]),
        }

    @app.post("/api/memos")
    def api_add(payload: dict = Body(default={})):
        from ... import event_text  # noqa: PLC0415

        raw = str((payload or {}).get("text") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="内容不能为空")
        # 「记一下买牛奶」→「买牛奶」（跟语音同一条清洗规则，见 strip_trigger 说明）
        content = (event_text.clean_memo_content(strip_trigger(raw)) or raw).strip()
        store = ctx.memos()
        with store.locked():
            saved = store.append({"content": content, "done": False})
        return {"ok": True, "item": saved, "content": content}

    @app.patch("/api/memos/{index}")
    def api_update(index: int, payload: dict = Body(default={})):
        store = ctx.memos()
        body = dict(payload or {})
        if "done" in body:
            updated = store.update(index, done=bool(body["done"]))
        elif "content" in body:
            text = str(body["content"]).strip()
            if not text:
                raise HTTPException(status_code=400, detail="内容不能为空")
            updated = store.update(index, content=text)
        else:
            raise HTTPException(status_code=400, detail="只能改 done 或 content")
        if updated is None:
            raise HTTPException(status_code=404, detail=f"没有第 {index} 条备忘")
        return {"ok": True, "item": updated}

    @app.delete("/api/memos/{index}")
    def api_delete(index: int):
        removed = ctx.memos().remove_at(index)
        if removed is None:
            raise HTTPException(status_code=404, detail=f"没有第 {index} 条备忘")
        return {"ok": True, "removed": removed}

    @app.post("/api/memos/clear-done")
    def api_clear_done():
        store = ctx.memos()
        with store.locked():
            removed = store.remove_where(lambda it: bool(it.get("done")))
        return {"ok": True, "removed": len(removed)}
