"""面板注册表：控制台的可扩展点。

一个面板 = 一段后端路由 + 一个前端标签页。注册之后：

    - 前端标签栏由 ``/api/panels`` 动态生成（加面板不用改 HTML）
    - 前端按 id 去 ``/static/panels/<id>.js`` 取这个面板自己的脚本

★为什么要这层间接★：以后接新功能（比如「训练数据集浏览」「MCP 工具开关」）时，
只该新增文件，不该回来改这个文件里的分支。所以这里只存**元数据**，
不认识任何具体面板，也不 import 具体面板——由 ``panels/__init__.py`` 负责导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Panel:
    """一个标签页的元数据（后端与前端共用的契约）。"""

    id: str
    """唯一标识：同时用作路由前缀建议、`/static/panels/<id>.js` 的文件名、前端注册名。"""

    title: str
    """标签上显示的名字（中文，短）。"""

    order: int = 50
    """标签顺序（小的在前）。默认 50，核心面板用 10~40。"""

    hint: str = ""
    """鼠标悬停时的一句话说明（可选）。"""

    needs_service: bool = False
    """是否依赖唤醒服务在运行（前端会据此显示「服务没跑」的提示条）。"""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "order": self.order,
            "hint": self.hint,
            "needs_service": self.needs_service,
        }


class Registry:
    """收集所有面板。`register` 由各面板模块调用，`all()` 给前端出清单。"""

    def __init__(self) -> None:
        self._panels: dict[str, Panel] = {}
        self._seen: dict[str, str] = {}          # id -> 注册者（报错时能指出是谁）

    def add(self, panel: Panel, *, owner: str = "") -> Panel:
        if panel.id in self._panels:
            raise ValueError(
                f"面板 id 重复：{panel.id!r}（已被 {self._seen.get(panel.id)} 注册）"
            )
        self._panels[panel.id] = panel
        self._seen[panel.id] = owner or "?"
        return panel

    def all(self) -> list[Panel]:
        return sorted(self._panels.values(), key=lambda p: (p.order, p.id))

    def ids(self) -> list[str]:
        return [p.id for p in self.all()]

    def get(self, panel_id: str) -> Panel | None:
        return self._panels.get(panel_id)


@dataclass
class Loaded:
    """导入面板模块的结果（给启动日志用，出问题时能一眼看出是谁坏了）。"""

    registered: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
