"""面板：更新（看版本、检查新版、一键更新）。

为什么值得占一个标签页：这个控制台可能是**唯一**的界面 —— 用 exe 的人不一定有
命令行，更不会自己去 GitHub 翻 release。所以「我这是什么版本、有没有新的、
怎么升」必须在界面上说清楚，而不是写在 README 里等人看。

★三个接口的分工★：

    GET  /api/update/check    只读：当前版本 / 远端版本 / 走 git 还是 zip / 能不能升
    POST /api/update/start    真动手：备份 → 下载 → 校验 → 覆盖（跑在后台线程里）
    GET  /api/update/status   看进度（前端每 1.2 秒轮一次）

★为什么 start 与 status 分开★：下载 + 解压 + pip install 可能好几分钟，
同步接口会让浏览器先超时（用户以为坏了，其实正在装）。所以 start 立刻返回，
进度由 status 一点点吐出来 —— 界面上的日志就是这么来的。
"""

from __future__ import annotations

import time

from ..registry import Panel

PANEL = Panel(
    id="update",
    title="更新",
    order=90,
    hint="看版本、检查新版、一键更新（动手前自动备份 data/ 与 config.toml）",
)


def register(app, ctx) -> None:
    from fastapi import Body, HTTPException  # noqa: PLC0415

    from ... import update as upd  # noqa: PLC0415
    from ...paths import describe  # noqa: PLC0415

    # 一个控制台进程一份（更新是**进程级**的事：不能两个标签页同时升）
    updater = upd.Updater(ctx.root)

    @app.get("/api/update/check")
    def api_check(refresh: bool = False, repo: str = ""):
        """看看有没有新版本（``?refresh=1`` 绕过 10 分钟缓存）。"""
        if refresh:
            upd.forget_cache()
        got = upd.check(ctx.root, repo=repo, fetch_json=upd.fetch)
        return {
            "ok": got.ok,
            "check": got.as_dict(),
            "env": describe(ctx.root),
            "hint": upd.env_hint(),
            "busy": updater.snapshot()["running"],
        }

    @app.post("/api/update/start")
    def api_start(payload: dict = Body(default={})):
        body = dict(payload or {})
        if body.get("dry_run"):
            got = upd.check(ctx.root, use_cache=False)
            plan = upd.plan_update(ctx.root, got.release) if (got.ok and got.release) else []
            return {"ok": True, "started": False, "dry_run": True, "plan": plan,
                    "check": got.as_dict()}
        if body.get("confirm") is not True:
            raise HTTPException(status_code=400,
                                detail="要先确认（请求里带 confirm: true），更新会覆盖程序文件")
        got = upd.check(ctx.root, use_cache=not body.get("refresh"))
        started, message = updater.start(
            check_result=got,
            repo=str(body.get("repo") or ""),
            backup=body.get("backup", True) is not False,
            update_exe=body.get("update_exe", True) is not False,
            install_deps=body.get("install_deps", True) is not False,
        )
        return {"ok": started, "started": started, "message": message,
                "check": got.as_dict(), "status": updater.snapshot()}

    @app.get("/api/update/status")
    def api_status(since: int = 0):
        """进度：``since`` 是客户端已经拿到的行数，避免每次把整段日志重发一遍。"""
        state = updater.snapshot()
        lines = state.get("lines") or []
        if since > 0 and since <= len(lines):
            state = {**state, "lines": lines[since:]}
        state["total_lines"] = len(lines)
        state["now"] = time.time()
        state["backup_latest"] = _latest_backup(ctx.root)
        return state

    @app.post("/api/update/restore")
    def api_restore(payload: dict = Body(default={})):
        """把最近一次备份还原回来（默认**只列会覆盖什么**）。"""
        from ... import setup_flow  # noqa: PLC0415

        body = dict(payload or {})
        bak = body.get("backup") or None
        try:
            changed = setup_flow.restore_user_data(ctx.root, bak,
                                                  dry_run=not body.get("apply"))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "applied": bool(body.get("apply")), "changed": changed,
                "backup": str(bak or (setup_flow.latest_backup(ctx.root) or ""))}


def _latest_backup(root) -> str:  # noqa: ANN001
    try:
        from ... import setup_flow  # noqa: PLC0415

        found = setup_flow.latest_backup(root)
        return found.relative_to(root).as_posix() if found else ""
    except Exception:  # noqa: BLE001 - 只是给界面看一眼，读不到就算了
        return ""
