"""面板：角色资料卡（按格式填 / 选文件导入）。

★不依赖唤醒服务★：这里全部是「读人格文件、写人格文件」，服务那边会自己热加载
（几秒内生效，不用重启）。所以这个面板 `needs_service=False`，
服务器没跑也能建角色 —— 建完再启动服务就行。

★安全边界★（都在 `console/persona_card.py` 里，这里只做 HTTP 壳）：
只写 `data/personas/<id>.json` 与索引两个文件，改前 `.bak`、写完校验 JSON，
默认 dry-run（要显式 `apply` 才落盘），id 必须是安全文件名。
"""

from __future__ import annotations

import base64

from ..registry import Panel

PANEL = Panel(
    id="persona",
    title="角色资料卡",
    order=45,
    hint="按格式新建角色（或导入 JSON / 素材清单 txt）",
    needs_service=False,
)

MAX_IMPORT_MB = 2.0


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    from .. import persona_card as card  # noqa: PLC0415

    @app.get("/api/persona/spec")
    def api_spec(cid: str = ""):
        """表单规范 + 一份样板 + 现有 id（前端据此画表单、挡重名）。"""
        registry = ctx.characters()
        example_char = None
        for got in registry.all():
            if cid and got.id == cid:
                example_char = got
                break
        if example_char is None:
            example_char = registry.get(str(ctx.settings.persona.default or "")) or registry.default()
        return {
            "groups": card.groups(),
            "example": card.example(example_char),
            "ids": card.existing_ids(ctx.settings),
            "personas_dir": card.rel(ctx.settings, card.persona_path(ctx.settings, "x").parent),
            "index": card.rel(ctx.settings, card.index_path(ctx.settings)),
        }

    @app.post("/api/persona/preview")
    def api_preview(payload: dict = Body(default={})):
        """检查一遍并给出「会写成什么」——不落盘。"""
        fields, problems, warnings = card.parse((payload or {}).get("fields") or {})
        target = ""
        if not problems:
            target = card.rel(ctx.settings, card.persona_path(ctx.settings, fields["id"]))
        return {
            "fields": fields, "problems": problems, "warnings": warnings,
            "json": card.preview(ctx.settings, fields) if not problems else {},
            "target": target,
            "exists": bool(target) and (ctx.settings.resolve(target)).exists(),
        }

    @app.post("/api/persona/create")
    def api_create(payload: dict = Body(default={})):
        """落盘：写资料卡 + 往索引追加一行（`apply=false` 时只报告）。"""
        body = payload or {}
        try:
            report = card.create(
                ctx.settings, body.get("fields") or {},
                overwrite=bool(body.get("overwrite")),
                dry_run=not bool(body.get("apply")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        report["ok"] = True
        return report

    @app.post("/api/persona/import")
    def api_import(payload: dict = Body(default={})):
        """读一个文件（base64）→ 返回解析出来的字段，交给前端填进表单（**不落盘**）。"""
        body = payload or {}
        name = str(body.get("filename") or "card.json")
        raw = str(body.get("data") or "")
        if not raw:
            raise HTTPException(status_code=400, detail="没有收到文件内容")
        try:
            blob = base64.b64decode(raw.split(",")[-1], validate=True)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"文件内容不是合法 base64：{exc}") from exc
        if len(blob) > MAX_IMPORT_MB * 1048576:
            raise HTTPException(status_code=400, detail=f"文件超过 {MAX_IMPORT_MB:.0f} MB")
        try:
            text = blob.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = blob.decode("gbk")           # 记事本存过的老文件很常见
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail=f"文件不是 UTF-8 / GBK 文本：{exc}") from exc
        fields, problems, note = card.read_import(name, text)
        return {"filename": name, "fields": fields, "problems": problems, "note": note,
                "bytes": len(blob)}

    @app.get("/api/persona/export")
    def api_export(cid: str = ""):
        """导出一张现有资料卡（可以直接分享 / 存进 git）。"""
        char = None
        for got in ctx.characters().all():
            if got.id == cid:
                char = got
                break
        if char is None:
            raise HTTPException(status_code=404, detail=f"没有角色 {cid!r}")
        return {"id": char.id, "filename": f"{char.id}.json", "json": char.to_dict()}
