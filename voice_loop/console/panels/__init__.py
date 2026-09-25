"""控制台面板清单（★加面板就改这一行★）。

顺序 = 标签页顺序（由 Panel.order 决定）。每个模块要提供：

    PANEL = Panel(id="xxx", title="名字", order=10)
    def register(app, ctx): ...      # 在这里挂自己的 /api/xxx 路由

前端脚本放 ``voice_loop/console/static/panels/<id>.js``，用
``window.Console.register("<id>", {...})`` 注册自己——标签栏是后端动态生成的，
所以两边都加上就出现新标签，不用碰 index.html/app.js。
"""

from __future__ import annotations

ALL = [
    "overview",
    "schedule",
    "memos",
    "logs",
    "voices",
    "chat",
]
