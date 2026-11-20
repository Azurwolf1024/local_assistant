# `voice_loop/mail/` —— 邮件子系统（可单独拿走）

> 这一个目录就是「怎么管邮件」。它**不认识** TTS / 记忆 / 技能 / 流水线：
> 弹窗、朗读、审批卡都是**注入**进来的（`toast=` / `speak=` / `on_draft=`），
> 所以整包能离线自测，也能单独搬到别的宿主里用。

## 一句话设计

**读**可以自动（同步 → 分级 → 提醒），**写**必须过人（草稿 → 弹窗点「发送」→ HMAC 令牌校验）。
模型能查、能读、能起草；**发不出去** —— 这条不是靠提示词，是靠代码闸门。

## 分层

```
Graph（HTTP + delta 查询）        graph.py       ← 唯一发请求的地方
        ↓
索引（SQLite）                    store.py       ← 检索 / 未读 / 未分级 / 未提醒
        ↓
同步游标                          delta.py       ← 首次拉窗口 + 增量 + 410 自动重来
        ↓
重要度分级                        triage.py      ← 规则分 + 模型分（只发主题+前 N 字）
        ↓
发信闸门                          outbox.py      ← 四道闸门 + HMAC token + 审计 jsonl
        ↓
提醒 / 审批                       notify.py / approve.py / watcher.py
        ↓
门面                              service.py     ← 外面只该用这一个
        ↑
MCP 工具（5 个）                  ../mcp/servers/mail.py
```

| 文件 | 管什么 | 什么时候要改它 |
| --- | --- | --- |
| `auth.py` | device code 登录、token 缓存 `data/mail/token.json`、自动续期 | 换认证方式（客户端证书 / broker） |
| `graph.py` | Microsoft Graph 封装：`/me/messages`、delta、草稿、发送；429 按 `Retry-After` 退避 | 换数据源（IMAP / 本机 Outlook COM） |
| `store.py` | SQLite 索引（messages / drafts / meta），中文多词 AND 检索 | 加字段、加索引 |
| `delta.py` | `Syncer`：首次按时间窗口拉 → 取 `delta` 游标接力 → 之后只问变化 | 改同步策略 |
| `triage.py` | 规则（VIP / 关键词 / 群发降权）+ 模型 JSON 合并 + **过期降权**，按 model 缓存 | 调整「什么叫重要」 |
| `outbox.py` | ★发信闸门★：白名单、状态机、HMAC token、幂等、审计 | 加闸门（几乎只加不减） |
| `attachments.py` | 附件：类型识别 / 清单描述 / **按需抽正文**（pdf/docx/文本） | 加 OCR 或新格式 |
| `link.py` | 邮件 → 日程：`EventProposer`（提议）+ `EventQueue`（待批队列，同卡片协议） | 改"什么样的事值得记" |
| `notify.py` | 重要立刻弹窗+念一句、同封只提醒一次、早/晚报摘要 | 改提醒节奏 |
| `watcher.py` | 服务里的巡检线程（一个 `Event` + 守护线程，`tick()` 可离线调） | 改周期 / 加动作 |
| `approve.py` | `ApprovalQueue`（纯逻辑）+ `ApprovalCard`（右下角 Tk 卡片） | 换审批界面（控制台 / 手机） |
| `adapters.py` | **唯一知道外面世界**的文件：把 `[api]` / Ollama 接成分类器 | 换分类模型 |
| `service.py` | `MailService` 门面（status/login/sync/search/read/summary/draft/send…） | 加能力 |

## 公开契约

```python
from voice_loop.mail import MailService

svc = MailService(settings, log=logger, llm=classifier)   # classifier: (system, user) -> str
svc.ready()                                  # (能不能干活, 为什么不能)
svc.sync()                                   # 拉一次（增量）
svc.search("选课", limit=10, unread_only=True)
svc.read(message_id)                         # {'ok', 'subject', 'body', ...}
svc.summary(since_hours=24)
svc.draft(to="a@b.com", subject="…", body="…")   # ★只建草稿★
svc.approve(draft_id) / svc.send(draft_id, token)
svc.close()
```

宿主只需要注入三样东西（都没有也能跑，只是不会弹窗/不朗读）：

```python
svc.on_draft = approval_card.submit          # 建完草稿 → 弹审批卡
notifier = Notifier(cfg, svc.store, toast=toast, speak=speak)
watcher  = MailWatcher(svc, notifier, interval=180)
```

## 发信的四道闸门（`outbox.Outbox.send`）

| # | 闸门 | 默认 |
| --- | --- | --- |
| 1 | `[mail] allow_send` 必须为 true | **false** |
| 2 | 收件人都在 `[mail] send_allowlist` 里 | **空表 = 谁都发不出去** |
| 3 | 草稿状态必须是 `approved`（点过按钮） | — |
| 4 | `confirm_token` = HMAC(草稿 id + 正文 hash + 过期时间) | 5 分钟过期 |

第 4 条专治「你批准了 A 版本，agent 偷偷改成 B 版本再发」：正文一变 hash 就对不上。
token 只在用户点「发送」的那一刻由本进程签发，模型拿不到、也伪造不了。

写日程同理：`mail_to_event` 只落一条 `proposals` 记录（`pending`），**卡片上点「加入」才
调用注入的 `add_event`**（`EventQueue.decide(id, "send")`）；点「忽略」记 `skipped`，什么都不写。

状态机：`pending → approved → sent`；`failed`（明确失败，可用同一 token 重试）；
`unknown`（网络断在半路，★不自动重试★，由人确认「已发送」里到底有没有）。

## 配置面（`[mail]`）

```toml
enabled / client_id / tenant / folder / first_sync_days / page_size / max_pages
sync_minutes / max_body_chars
classify_chars / classify_model / classify_per_sync / high_score / medium_score
vip_senders / priority_domains
notify_high / digest_hours
allow_send / send_allowlist / approval_ttl / timeout
```

`client_id` 放 `config.local.toml`（gitignored）。

## 自测

```powershell
python scripts\tests\test_mail.py
```

全部打桩（假 Graph / 假 token / 假模型），不联网、不登录、不弹窗，覆盖：
token 续期与失效、翻页与 410、只外发「主题+前 N 字」、四道闸门、幂等、`unknown`
不自动重试、审批队列「点取消就不发」、提醒去重、MCP 工具形状。

## 数据与隐私

```
data/mail/index.sqlite     索引（主题/正文摘要/分级）—— 本地文件
data/mail/token.json       访问令牌 + 刷新令牌（Windows DPAPI 可用时加密）
data/mail/audit.jsonl      发信审计（谁批的、什么时候、发的什么 hash）
data/mail/secret.bin       HMAC 密钥（审批 token 用）
```

- 分级只把「主题 + 发件人 + 前 `classify_chars` 字」发给模型；整封正文不出本机。
- 想完全不外发：`classify_model = "ollama:qwen3.5:4b"`。
- 日志里不写正文（见 `service._brief`）。
