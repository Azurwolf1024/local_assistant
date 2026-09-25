"""面板：日志跟随。

界面上的日志有两条来源，这里都给：

    - **历史**：``GET /api/logs/tail`` 直接读文件末尾（打开页面就有内容）
    - **实时**：走控制台的 SSE（``kinds=log``），由 ``console/follow.py`` 推送

★只读★：服务独占写 ``sessions/listen.log``（`main.py listen` 把 stdout 重定向进去）。
控制台只 seek/read，绝不追加——两个进程交错写同一个日志会乱成一团。
"""

from __future__ import annotations

from pathlib import Path

from ..follow import classify, read_tail
from ..registry import Panel

PANEL = Panel(id="logs", title="日志", order=40, hint="实时跟随服务日志，报错高亮", needs_service=True)


def register(app, ctx) -> None:
    @app.get("/api/logs/tail")
    def api_tail(lines: int = 300):
        lines = max(20, min(3000, int(lines)))
        rows = read_tail(ctx.watcher.log_path, lines)
        return {
            "file": str(ctx.watcher.log_path),
            "exists": ctx.watcher.log_path.exists(),
            "lines": [{"line": row, "level": classify(row)} for row in rows],
        }

    @app.get("/api/logs/files")
    def api_files():
        """能给界面看的日志文件（服务日志 + 测试日志目录里最新的几个）。"""
        out = []

        def add(path: Path, label: str) -> None:
            try:
                st = path.stat()
            except OSError:
                return
            out.append({
                "label": label,
                "path": str(path),
                "size": st.st_size,
                "age_seconds": None,
                "mtime": st.st_mtime,
                "primary": label == "服务日志",
            })

        add(ctx.watcher.log_path, "服务日志")
        extra = ctx.watcher.log_path.parent / "sessions"
        for path in (ctx.root / "sessions").glob("*.log"):
            add(path, path.name)
        if extra.exists():
            for path in extra.glob("*.log"):
                add(path, path.name)
        out.sort(key=lambda x: (not x["primary"], -x["mtime"]))
        return {"items": out[:20], "dir": str(ctx.root / "sessions")}
