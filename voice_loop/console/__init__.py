"""可视化控制台（本地网页 UI）。

## 它是什么

`python main.py ui` 会起一个**独立进程**，只监听本机回环地址，用浏览器打开。
它把项目现有的能力集中到几个面板里：服务状态、日程/闹钟、备忘、日志、角色声线、文字对话。

## 为什么是「独立进程 + 网页」

    - 进程隔离：界面崩了不影响语音服务；改控制台代码不用重启语音服务
    - 网页是唯一能同时做到「表格好编辑」「画周视图」「实时跟随日志」又**不引第三方 UI 库**的形态
    - Tk 那条路走不通：本项目规定**一个进程只能有一个 Tk 解释器**，而且只能由创建它的线程碰
      （见 voice_loop/ui.py 顶部的说明），字幕/提醒小窗已经占着它了

## 怎么加一个新功能（★这是本包存在的意义★）

加一个面板只要两步，**不用改别人的代码**：

    1. voice_loop/console/panels/你的面板.py
           PANEL = Panel(id="xxx", title="标签名", order=40)
           def register(app: FastAPI, ctx: ConsoleContext) -> None:
               @app.get("/api/xxx")
               def get_xxx(): return {...}
    2. voice_loop/console/static/panels/xxx.js
           控制台.register("xxx", ({ api, el }) => { ... 渲染 + 交互 ... })

然后在 `panels/__init__.py` 的 `ALL` 里加上模块名即可——前端标签页是从 `/api/panels`
动态生成的，所以加完刷新浏览器就出现新标签，不用碰 HTML。

## 安全边界（写清楚，免得以后被当成「内部工具」随便暴露）

    - 默认只绑定 127.0.0.1；要给别人看必须显式 `--host 0.0.0.0`，README 第 10 节讲隐私
    - 不做鉴权：**它是本机工具**，谁能访问端口谁就能改你的日程
    - 不碰 `sessions/listen.pid` / `sessions/listen.stop` / 麦克风 / 扬声器：
      启停服务一律交给 `main.py stop|listen`（见 voice_loop/service_ctl.py）
"""

from __future__ import annotations

from .app import ConsoleContext, create_app, serve

__all__ = ["ConsoleContext", "create_app", "serve"]
