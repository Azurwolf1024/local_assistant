"""邮件（Outlook / Microsoft Graph）子系统。

## 为什么这是「子系统」而不是一个工具

管理邮件需要**状态**（同步游标、本地索引、已分类结果、待批草稿），
需要**安全边界**（token 只在这里、发信必须人工批准、审计留痕），
还需要**自己的循环**（每几分钟增量同步一次，重要邮件当场提醒）。
这四件事都不是一个 ``send_email`` 函数装得下的 —— 但也不需要拆成独立项目：
下面这些模块只依赖 settings / log / 路径，**整个目录可以搬走**。

## 目录

| 文件 | 干什么 |
|---|---|
| ``auth.py``     | device code 登录 + token 缓存/续期（全项目唯一拿得到 access token 的地方） |
| ``graph.py``    | Graph HTTP 封装：429 退避、410 让上层全量重来、超时 |
| ``store.py``    | 本地 SQLite 索引（中文检索靠它，Graph 的 ``$search`` 对中文很弱） |
| ``delta.py``    | 增量同步：第一次拉最近 N 天，之后只问「有什么变化」 |
| ``triage.py``   | 重要度打分：规则先、模型后、反馈可学习 |
| ``notify.py``   | 通知策略（立刻 / 摘要 / 静默）+ 同一封只提醒一次 |
| ``outbox.py``   | 草稿 → 待批 → 已发 的状态机 + confirm_token + allowlist + 审计 |
| ``approve.py``  | 右下角审批卡（复用 ``voice_loop/ui.py`` 的 Tk 宿主） |
| ``watcher.py``  | 服务里的巡检线程（照 ``ReminderScheduler`` 的样子） |
| ``service.py``  | 门面：把上面几件事拼成 ``MailService`` |

## 不变量（改代码前先读）

1. **发信永不自动**：必须有 ``confirm_token``（审批后签发、绑定正文 hash、5 分钟过期）；
   ``[mail] allow_send`` 默认 false、``send_allowlist`` 默认空 —— 两道硬闸门都在 outbox 里。
2. **正文不出本机**（除分类时的那一小段）：分类只发「主题 + 前 N 字」，N 由配置控制。
3. **日志不打正文**：只打 id、主题前 40 字。
4. **没有 token 就不能动网**：所有网络调用都得先过 :class:`voice_loop.mail.auth.Auth`。

    python main.py mail status      # 看登录状态 / 同步进度
    python main.py mail login       # 设备码登录
"""

from __future__ import annotations

from .service import MailService, mail_root

__all__ = ["MailService", "mail_root"]
