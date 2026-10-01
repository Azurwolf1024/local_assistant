"""面板：★角色★（资料卡 + 声线，2026-10-01 合并）。

一个标签页管两件事，因为它们本来就是同一件事的两半：

- **改资料卡**：按一份 SPEC 生成表单（新建 / 修改 / 从文件导入 / 导出），只写
  `data/personas/<id>.json` 与索引；★不依赖唤醒服务★（服务那边几秒内热加载）；
- **声线**：看参考音、切过去、试听、★只用一条语音就能克隆★ —— 这些必须让服务做，
  路由在 `panels/voices.py`，由本模块一并挂上（它就是同一个标签页的另一半）。

为什么要合并：用户要的是「管角色」，不是「先想这件事该去哪个标签页」——
建完资料卡顺手就试听，听着不对就要回去改资料，来回切标签纯属浪费。

★安全边界★（细节都在 `console/persona_card.py` 里，这里只做 HTTP 壳）：
只写 `personas/<id>.json` 与索引两个文件，改前 `.bak`、写完校验 JSON，
默认 dry-run（要显式 `apply` 才落盘），id 必须是安全文件名。
★「修改」不是「覆盖」★：表单只动它自己那几栏，`enabled` / `voice_dir` 这些键原样保留。
"""

from __future__ import annotations

import base64
import json

from ..registry import Panel

PANEL = Panel(
    id="persona",
    title="角色",
    order=45,
    hint="改资料卡、切声线、只用一条语音克隆、试听（切声线/试听需要服务在跑）",
    needs_service=False,
)

MAX_IMPORT_MB = 2.0


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    from .. import persona_card as card  # noqa: PLC0415
    from . import voices as voice_routes   # noqa: PLC0415

    # ★同一个标签页的另一半★：声线路由（切过去/试听/克隆）也挂在这个面板下。
    # 路径仍是 `/api/voices/*` ——「声线」是功能名，跟标签页怎么分无关。
    voice_routes.register(app, ctx)

    def find(cid: str):
        """按 id 找一个角色（找不到就是 None，调用方决定 404 还是报错）。"""
        for got in ctx.characters().all():
            if got.id == cid:
                return got
        return None

    @app.get("/api/persona/spec")
    def api_spec(cid: str = ""):
        """表单规范 + 一份样板 + 现有 id（前端据此画表单、挡重名）。"""
        registry = ctx.characters()
        example_char = find(cid) if cid else None
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
        char = find(cid)
        if char is None:
            raise HTTPException(status_code=404, detail=f"没有角色 {cid!r}")
        return {"id": char.id, "filename": f"{char.id}.json", "json": char.to_dict()}

    @app.get("/api/persona/get")
    def api_get(cid: str = ""):
        """★编辑用★：把一个现有角色整理成**表单字段**（前端填进表单就能改）。

        还带上三层信息，因为「改」比「建」多出的风险都在这三层：

        - `raw`：人格文件里**真正**存了什么（对答案用）；
        - `quiet`：表单不暴露、但会被修改**原样保留**的键（`enabled`/`voice_dir`/`world`…）；
        - `file`：改的是哪个文件（索引里写的那条，不一定在 `personas/` 下）。
        """
        if not cid:
            raise HTTPException(status_code=400, detail="缺少 cid")
        char = find(cid)
        if char is None:
            raise HTTPException(status_code=404, detail=f"没有角色 {cid!r}")
        target = card.file_of(ctx.settings, char.id)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return {
            "id": char.id,
            "name": char.name,
            "fields": card.fields_of(char),
            "raw": char.to_dict(),
            "quiet": {k: raw[k] for k in sorted(card.QUIET_KEYS) if k in raw},
            "file": card.rel(ctx.settings, target),
            "file_exists": target.is_file(),
            "index": card.rel(ctx.settings, card.index_path(ctx.settings)),
            "exists": True,
        }

    @app.post("/api/persona/update")
    def api_update(payload: dict = Body(default={})):
        """改已有角色（`apply=false` 时只报告会改哪几栏）。

        ★跟 create 分开一条路★：「新建」与「修改」的失败方式完全不同 ——
        新建怕重名，修改怕**把文件里别的键弄丢**，所以各自一条路、各自一套校验。
        """
        body = payload or {}
        try:
            report = card.update(ctx.settings, body.get("fields") or {},
                                 dry_run=not bool(body.get("apply")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        report["ok"] = True
        return report
