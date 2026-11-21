# `voice_loop/mail/` —— 邮件子系统

> 这一个目录就是「怎么管邮件」。它**不认识** TTS / 记忆 / 技能 / 流水线：
> 弹窗、朗读、审批卡都是**注入**进来的（`toast=` / `speak=` / `on_draft=`），
> 所以整包能离线自测，也能单独搬到别的宿主里用。
>
> 面向使用者的说明在 [`docs/guide/mail.md`](../../docs/guide/mail.md)（怎么注册应用、怎么登录、
> 怎么让它发信）；**这一份讲实现**：和 Outlook 的接口是什么、给 agent 开了哪些工具、
> 数据长什么样、闸门装在哪、坏了怎么查。

## 0. 一句话设计

**读可以自动**（同步 → 分级 → 提醒），**写必须过人**（草稿 → 弹窗点「发送」→ HMAC 令牌校验）。

模型能查、能读、能起草；**发不出去** —— 这条不是靠提示词，是靠代码闸门
（`outbox.Outbox.send` 的四道门）。同理「把邮件里的事记进日程」也只是**提议**，
用户在卡片上点「加入」才真的写。

三个视角各看一层：

| 视角 | 关心什么 | 看哪 |
| --- | --- | --- |
| 用户 | 怎么登录、怎么让它发信、哪些开关能关掉外发 | `docs/guide/mail.md` |
| Agent（模型） | 我有哪些工具、参数怎么写、能不能发信 | §3（工具表）+ `../mcp/servers/mail.py` 的 description |
| 改代码的人 | Graph 调了什么、数据存哪、闸门在哪、坏了怎么查 | 本文件 |

## 1. 目录地图

```
Graph（HTTP + delta 查询）        graph.py        ← 唯一发网络请求的地方
        ↓
索引（SQLite）                    store.py        ← 检索 / 未读 / 未分级 / 未提醒
        ↓
同步游标                          delta.py        ← 首次拉窗口 + 增量 + 410 自动重来
        ↓
重要度分级                        triage.py       ← 规则分 + 模型分（只发主题+前 N 字）
        ↓
发信闸门                          outbox.py       ← 六态状态机 + HMAC token + 审计 jsonl
        ↓
提醒 / 审批                       notify.py / approve.py / watcher.py
        ↓
门面                              service.py      ← 外面只该用这一个
        ↑
MCP 工具（6 个）                  ../mcp/servers/mail.py
```

| 文件 | 管什么 | 关键接口 | 什么时候要改它 |
| --- | --- | --- | --- |
| `auth.py` | device code 登录、token 缓存、自动续期 | `Auth.start_device_login` / `wait_for_token` / `headers` / `status` | 换认证方式（客户端证书 / broker / 多账号） |
| `graph.py` | Microsoft Graph 封装：读信、delta、草稿、发送、附件；429 按 `Retry-After` 退避 | `Graph.call` / `delta` / `list_recent` / `message` / `create_draft` / `send_draft` | 换数据源（IMAP / 本机 Outlook COM） |
| `store.py` | SQLite 索引（4 张表）+ 中文多词 AND 检索 + `prune` | `Store.search` / `upsert_messages` / `drafts` / `proposals` | 加字段、加索引、改检索 |
| `delta.py` | `Syncer`：首次按时间窗口拉 → 取 `deltaLink` 接力 → 之后只问变化；410 自动重来 | `Syncer.sync` / `bootstrap` | 改同步策略 |
| `triage.py` | 规则（关键词 / VIP / 群发降权）+ 模型 JSON 合并 + **过期降权** | `Triage.score` / `is_stale` / `classify` | 调整「什么叫重要」 |
| `outbox.py` | ★发信闸门★：白名单、六态状态机、HMAC token、幂等、审计 | `Outbox.create` / `approve` / `send` / `reject` | 加闸门（几乎只加不减） |
| `attachments.py` | 附件：类型识别 / 清单描述 / **按需抽正文** | `list_of` / `extract_text` / `kind_of` | 加 OCR 或新格式 |
| `link.py` | 邮件 → 日程：`EventProposer`（提议）+ `EventQueue`（待批队列） | `propose` / `decide` / `pending` | 改「什么样的事值得记」 |
| `notify.py` | 重要立刻弹窗+念一句、同封只提醒一次、早/晚报摘要 | `Notifier.high` / `digest` | 改提醒节奏 |
| `watcher.py` | 服务里的巡检线程（一个 `Event` + 守护线程，`tick()` 可离线调） | `MailWatcher.start` / `tick` | 改周期 / 加动作 |
| `approve.py` | `ApprovalQueue`（纯逻辑）+ `ApprovalCard`（右下角 Tk 卡片） | `submit` / `decide` | 换审批界面（控制台 / 手机） |
| `adapters.py` | **唯一知道外面世界**的文件：把 `[api]` / Ollama 接成分类器 | `api_classifier` / `ollama_classifier` | 换分类模型 |
| `service.py` | `MailService` 门面 | `sync` / `search` / `read` / `draft` / `approve` / `send` | 加能力 |

---

## 2. 和 Outlook 的接口

### 2.1 为什么是 Microsoft Graph

| 方案 | 为什么不用 |
| --- | --- |
| **IMAP/SMTP** | 学校/公司账号多半关了基本认证；拿不到 `conversationId`、附件 `$value`、`webLink`；分类只能靠本地猜 |
| **本机 Outlook COM** | 只有 Windows + 装了桌面 Outlook 才行；pythoncom 线程模型和我们的后台线程打架；exe 里还得多带一套 COM |
| **Exchange Web Services（EWS）** | 微软已宣布 2026 年起陆续停用；Graph 才是官方路线 |
| **Microsoft Graph** ★ | 一条 REST + OAuth2 device code 就能覆盖读/写/附件/增量；个人号（`consumers`）、学校号（`organizations`）同一套代码 |

代价是「要注册一个 Entra 应用、要管理员批准某些租户」—— 那部分写在
[`docs/guide/mail.md`](../../docs/guide/mail.md) 的前两节。

### 2.2 认证：device code 流程

对**桌面程序**来说 device code 是官方推荐的做法：不需要 client secret、
不需要公网回调地址、不需要监听本地端口（这三点在「把 exe 发给别人」时全是麻烦）。

```
① 我们  ──POST /{tenant}/oauth2/v2.0/devicecode──▶  微软
        ◀── {user_code: "ABCD-EFGH", device_code, verification_uri, interval, expires_in}

② 用户  浏览器打开 https://microsoft.com/devicelogin，输入 ABCD-EFGH，登录 + 同意权限

③ 我们  ──POST /{tenant}/oauth2/v2.0/token (grant_type=…device_code)──▶ 微软
        ◀── 还没好：authorization_pending（按 interval 秒轮询）
        ◀── 好了：access_token + refresh_token + expires_in

④ token 存 data/mail/token.json；之后每个请求带 Authorization: Bearer …

⑤ 过期前（提前 120 秒）用 refresh_token 换新的；换不动 → AuthError(needs_login=True)
```

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `LOGIN_HOST` | `https://login.microsoftonline.com` | 主权云（如 21Vianet）要改这里 |
| `TENANT` 默认 | `consumers` | 见下面那张表 |
| `SCOPES` | `offline_access` `User.Read` `Mail.ReadWrite` `Mail.Send` | ★`offline_access` 才有 refresh token★ |

**账号类型 ↔ `tenant` 对照**（配错是第一次登录最常见的原因）：

| 你的账号 | `tenant` |
| --- | --- |
| 个人（`@outlook.com` / `@hotmail.com` / `@live.com`） | `consumers` |
| 学校 / 公司（`@xxx.edu.cn`、`@company.com`） | `organizations`，或直接填租户 ID（GUID） |
| 两种都要用 | `common`（微软会按账号路由；但**授权过的应用范围是分开的**） |

**常见 AAD 错误码**（`auth.py` 会把它们翻成人话）：

| 错误 | 意思 | 怎么办 |
| --- | --- | --- |
| `AADSTS700016` | 这个租户里找不到你的 client_id | 应用注册在别的账号下，或者 `tenant` 写错 |
| `AADSTS65001` / `AADSTS9002346` | 用户/管理员**还没同意**这套权限 | 让管理员跑一次 `adminconsent?scope=…` |
| `AADSTS9002332` | 这个账号类型不支持这个应用（常见于 MSA + 单租户应用） | 把应用改成多租户，或 `requestedAccessTokenVersion: 2` |
| `AADSTS7000218` | 请求体里该有 `client_assertion` | 应用清单里 `isFallbackPublicClient` 没开 |
| 401 + `InvalidAuthenticationToken` | token 过期/被吊销 | 重新登录（`python main.py mail login`） |

> token 文件是**明文 JSON**（`data/mail/token.json`），非 Windows 上 `chmod 0600`。
> ★没做 DPAPI 加密★：它和 `config.local.toml` 一个级别 —— 都在你的用户目录里、
> 都进了 `.gitignore`、都会被同账号的其他程序读到。要更强的隔离就把整个 `data/mail/` 放到加密盘。

### 2.3 用到的每一个 Graph 端点

| 用途 | 方法 + 路径 | 参数 | 返回里我们用到的 |
| --- | --- | --- | --- |
| 我是谁 | `GET /me` | `$select=displayName,mail,userPrincipalName` | 显示名（落款）、地址（排除自己发的）、UPN |
| 文件夹 id | `GET /me/mailFolders/{name}` | `$select=id,displayName`（`inbox` 这种 well-known 名） | `id` |
| **增量拉信** | `GET /me/messages/delta` 或 `GET /me/mailFolders/{id}/messages/delta` | `$select=<LIST_SELECT>` `$top=page_size`，翻页用上一页的 `@odata.nextLink` | `value[]`、`@odata.nextLink`、`@odata.deltaLink` |
| **拿「此刻」游标** | 同上，但**必须同样用 `$select=<LIST_SELECT>`**，从头翻到最后一页 | `$top=100`，最多 `max_pages=30` 页 | 最后一页的 `@odata.deltaLink` |
| 首次按窗口拉 | `GET /me/messages`（或指定文件夹） | `$select=<LIST_SELECT>` `$top` `$filter=receivedDateTime ge <ISO>` `$orderby=receivedDateTime desc` | `value[]`、`@odata.nextLink` |
| 读一封正文 | `GET /me/messages/{id}` | `$select=<BODY_SELECT>` | `body.content`、`uniqueBody.content`、`webLink` |
| 附件清单 | `GET /me/messages/{id}/attachments` | `$select=id,name,contentType,size,isInline` | 名字/类型/大小/是不是内嵌图 |
| 附件内容 | `GET /me/messages/{id}/attachments/{att}/$value` | — | 二进制（8 MB 以上不下载） |
| 回复草稿 | `POST /me/messages/{id}/createReply` | `{}` | 新草稿的 `id`（保留会话串） |
| **建草稿** | `POST /me/messages` | `{subject, body:{contentType:"HTML"}, toRecipients[], ccRecipients[], …}` | 草稿 `id` |
| 改草稿 | `PATCH /me/messages/{draft_id}` | 同上（局部） | 改后的对象 |
| **发送** | `POST /me/messages/{draft_id}/send` | `{}` | 204（无 body） |
| 删信 | `DELETE /me/messages/{id}` | — | 204（`mail prune` 用不到，删云端是显式命令） |

两套 `$select`（**只取要用的字段**，否则一封带图邮件能几 MB）：

```python
LIST_SELECT = "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,
               receivedDateTime,isRead,hasAttachments,importance,bodyPreview,webLink,flag"
BODY_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,uniqueBody,webLink"
```

★`uniqueBody` 是关键★：它只包含**这封新写的部分**，把「Re: Re: Re:」下面几百行引用历史切掉。
邮箱里一次来回能到十几 KB，不裁的话读 5 封就顶掉一整轮上下文。

### 2.4 增量同步（delta）

```
第一次（或 --full）：
  bootstrap → 先 store.drop_broken()（清掉历史空行）
            → list_recent(days=first_sync_days) → upsert 进 SQLite
            → delta_cursor_from_now()  ← ★从头翻到最后一页，取 @odata.deltaLink★
            → 写进 meta['delta_cursor']

之后每次：
  delta(cursor) → 只返回「新到的 / 改过的 / 删掉的」
                → 正常条目 upsert；@removed 的删本地行
                → 缺字段的（稀疏更新）逐条补 message_row()；补不到就跳过、**游标不前进**
                → 全吃下来了才把新的 @odata.deltaLink 存起来
  HTTP 410 / 400 resyncRequired → 丢掉游标，重新 bootstrap 一次
  「纯 id 且补不了」→ 判形状坏了（见下），丢游标、同一轮重建
```

**★★为什么不能用 `$select=id` 铸游标（2026-11-21 真机事故）★★**

delta 令牌**把查询形状烘在令牌里**。用 `$select=id` 铸出来的游标，之后每一页增量
只带 `id`（实测返回项只有 `@odata.etag / @odata.type / id`）：

| 现象 | 后果 |
| --- | --- |
| `normalize_message()` 拿到空对象 | 新邮件被写成「只有 id 的空行」 |
| 搜索按 `received_at` 排序/过滤 | 这些行永远搜不到 → Agent 说「没有新邮件」 |
| 日志 | 一路显示「同步完成：新增 2」——看着完全正常 |
| 老邮件被"更新"时同样是空载荷 | `upsert` 会把已有的**主题/发件人抹空** |

所以现在：**铸游标必须用同一份 `LIST_SELECT`**（代价只是首次多翻几页）；
另外 `delta.py` 里还有三道兜底 —— 因为**形状对了也不代表每条都有全字段**：

| Graph 行为（实测） | 我们的处理 |
| --- | --- |
| **改过**的邮件只回**变化字段**（把一封标未读后，增量里只有 `id` / `isRead` / `parentFolderId`） | 没有 `receivedDateTime` 的条目一律**逐条补**（`GET /me/messages/{id}?$select=<LIST_SELECT>`，一页最多 `BACKFILL_PER_ROUND=60` 封），补完再 upsert |
| 补不到（网络/权限） | **跳过、不写空行，并且游标不前进** —— 推进了那几条就永远丢了（Graph 不会重发） |
| 纯 `id` 载荷 + 这个 Graph 连按 id 补都做不到（老版本铸的窄游标） | 判 `ShapeBroken`：丢游标、同一轮按时间窗口重建（`repaired: True`） |
| 删除通知（`id` + `@removed`） | **删本地行**，绝不能当新邮件 upsert（那又是一行空的） |

日志会分别报：`新增 X、更新 Y、删除 Z（增量 N 页），补字段 A、跳过 B` ——
★`补字段` 出现说明 Graph 回了稀疏更新（正常），`跳过` 出现说明有条目这一轮没吃下（游标会停在原地）★。

**★为什么不用 `$deltatoken=latest`★**：那是旧版 Outlook REST 的写法，Graph 的
`/messages/delta` 不认 —— 实机上它返回的对象里**根本没有** `@odata.deltaLink`，
于是游标永远空、每 3 分钟全量重拉一遍（踩过：一次巡检把 276 封重新索引）。
现在只能老老实实翻页，跟着 `@odata.nextLink` 翻到最后一页；
`max_pages` 用完还没到底就放弃（返回空游标，下次再来）—— 至少不会把自己卡死。

**水位先写还是后写**：现在是**吃下了才写**。以前是"先写水位再处理"，好处是崩了不重复，
坏处是崩了/补不到就**永久丢信**；现在的做法是「有没吃下的条目就把游标停在原地，下一轮重来」，
重复处理几条（upsert 幂等）比丢信划算。

### 2.4b 索引新鲜度：模型凭什么敢说「没有新邮件」

模型只能看见本地索引。索引落后时它必须说「我这边可能不是最新的」——
这条写进了两处代码，不再靠提示词自觉：

| 位置 | 行为 |
| --- | --- |
| `MailService.freshness()` | `last_sync_at`（存在 `meta` 里，重启也知道）、`newest_at`（索引里最新一封）、`age`、`stale`（> max(15 分钟, 3×`sync_minutes`) 或上次同步失败） |
| `MailService.stamp()` | 一句话：「（索引同步于 21:08，最新一封 11-21 20:50）」；`stale` 时追加「★这份索引可能不是最新的，先别断言「没有新邮件」★」 |
| `mail_search` / `mail_summary` | 结果末尾带上面那句（`servers/mail.py`）；`INSTRUCTIONS` 里明说「这句不是邮件内容，别念；它说可能不是最新时不许断言没有新邮件」 |
| `mail_summary` 措辞 | `only_important=True` 且没有重要邮件时，补一句「但有 N 封普通邮件；最新一封是 …」——★没有重要邮件 ≠ 没有邮件★ |
| `main.py mail doctor` | 一次体检：开关 / 登录 / 索引 / 空行 / 游标 / 是否够新 |

### 2.5 限流与错误

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `429` | 限流 | 按响应头 `Retry-After`（默认 5s）退避后重试一次 |
| `410` | delta 游标过期 | 抛 `DeltaExpired` → `Syncer` 自动重新 bootstrap |
| `401` | token 失效 | 刷新一次 token 再试；还是 401 → `AuthError(needs_login=True)` |
| `403` | 权限不够 | 说明缺 `Mail.ReadWrite` / `Mail.Send`，或管理员没同意 → 提示重新走 adminconsent |
| 其它 4xx/5xx | — | `GraphError(status, reason)`，`reason` 是 Graph 给的 `error.message` 原文 |

### 2.6 发信路径：为什么是「建草稿 → 用户点发送 → send」

Graph 有更直接的 `POST /me/sendMail`（一次请求发出去）。我们**故意不用**：
`sendMail` 发完什么都没有，草稿箱里不留痕、发错了没法看原文。
用 `POST /me/messages` 建草稿的好处：

1. 用户能在**真实邮箱**里看到它（Outlook 草稿箱），和我们卡片里那份是同一封；
2. 我们能在 `PATCH` 阶段改主题/正文/收件人（用户说「再礼貌点」）；
3. `POST …/send` 是最后一步，失败可重试（草稿还在）；
4. 审计能对上「发出去的那一封」就是用户看过的那个 `body_hash`。

---

## 3. 给 Agent 的 6 个工具

注册在 [`../mcp/servers/mail.py`](../mcp/servers/mail.py)，属于 `mail` 工具预设
（`voice_loop/tool_presets.py`）。**工具 description 写得很啰嗦**：模型只能看见它们
（服务器的 `instructions` 我们自己的宿主**不转发**，详见工程日志 §71）。

| # | 工具 | 参数 | 读/写 | 闸门 | 用户会怎么说 |
| --- | --- | --- | --- | --- | --- |
| 1 | `mail_search` | `query` `limit(1-30)` `unread_only` `level(high/medium/low)` `sender` `since_hours` | 读 | — | 「学校发的通知」「谁给我发过…」「找那封关于选课的」 |
| 2 | `mail_read` | `id` `max_chars(200-20000，默认 6000)` | 读 | — | 「那封邮件说了什么」「念一下截止时间」 |
| 3 | `mail_summary` | `since_hours(默认 24)` `only_important(默认 true)` | 读 | — | 「有没有重要邮件」「有没有要回的」 |
| 4 | `mail_draft` | `to` `subject` `body` `cc` `reply_to` | **写草稿** | 只建草稿；发不出去 | 「给 AIAA3102 的老师发封邮件说…」 |
| 5 | `mail_to_event` | `id` | **写提议** | 只落 `proposals`（pending） | 「把这个截止时间加到日程里」 |
| 6 | `mail_send` | `draft_id` `confirm_token` | **写（真发送）** | 四道闸门；token 只有审批卡能签发 | 一般**不该**由模型调 —— 用户点卡片才是正常人路 |

有几处 description 里塞了「实现事实」，因为它们直接影响模型怎么写参数：

- `mail_search.query`：空 = 最近的邮件；搜的是**本地索引**（主题/发件人/正文全文），
  所以「上个月的那封」也能搜到，不需要先同步。
- `mail_draft.to`：**可以说人话**（「AIAA3102 的老师」「教务处」「私人邮箱」「itdid」），
  系统从往来邮件里匹配地址；分不清会返回候选，description 明确要求
  「★这时要用 `ask_user` 问用户，绝对不要自己编地址★」。别名和已配置的地址
  （例如「私人邮箱」= 某个地址）**写在参数说明里**，模型才看得见。
- `mail_draft.body`：要求模型把口语补全成**能直接发出去的信**（称呼 / 事由 / 请求 /
  收束 / 落款），并处理「礼貌一点」「用英文」「简短点」这类要求；落款默认取
  Graph 的 `displayName`（`service.signature()`）。
- `mail_send.confirm_token`：说明里直接写「这是结构限制，不要尝试绕过」。

**为什么只有 6 个**：工具越多模型选错率越高（实测 22 个工具时，`deepseek-chat`
把「看看邮件」错选成 `list_memos`）。所以邮件这一摊只开这 6 个，
草稿审批、附件下载、删除、标已读这些**刻意不做成工具** —— 它们要么该由用户点，
要么该由代码自动做。

---

## 4. 数据流与状态机

```
每 sync_minutes（默认 3 分钟）/ 手动 `mail sync`
        │
        ├─ delta 拉变化 ─▶ SQLite（messages）
        │
        ├─ 分级（每轮最多 classify_per_sync 封；> classify_max_age_days 的只走规则）
        │        └─ high → 立刻弹窗 + 念一句（notify.high，同封只提醒一次）
        │
        └─ 早/晚 digest_hours 两个时间点出摘要

用户说「给 X 发封邮件说…」
        │
        ├─ agent 调 mail_draft(to="X", subject, body)
        │        ├─ 收件人匹配（contacts.py）
        │        ├─ 建草稿记录（state=pending，同时 POST /me/messages 建 Graph 草稿）
        │        └─ 弹审批卡（approve.ApprovalCard）
        │
     用户点「发送」（8 秒内点第二次）──▶ outbox.approve → 签发 HMAC token（5 分钟）
        │
        └─ outbox.send(draft_id, token)
                 ├─ 四道闸门全过 → POST /me/messages/{draft}/send
                 ├─ 成功 → state=sent，写 audit.jsonl（含 body_hash）
                 └─ 网络断在半路 → state=unknown（★不自动重试★，要人去看草稿箱）
```

| 状态 | 谁能进 | 意味着 |
| --- | --- | --- |
| `pending` | `Outbox.create` | 草稿已生成，等你看 |
| `approved` | `Outbox.approve`（用户点按钮） | 已签发一次性 token，`expires_at` 到了就作废 |
| `sent` | `send` 成功 | 真发出去了；再调只回同一结果，不会发第二封 |
| `failed` | `send` 拿到明确的 Graph 错误 | 可以**用同一个 token** 重试（正文没变，hash 对得上） |
| `unknown` | `send` 时网络断在半路 | 不知道发没发出去 → 要人去草稿箱/已发送里看一眼 |
| `rejected` | `Outbox.reject`（用户点取消） | 永不发送；Graph 那边的草稿也删掉 |

`proposals`（邮件→日程）只有两态：`pending` → 点「加入」写日程并记 `event_id`，
或点「忽略」记 `skipped`。**没有**「模型自己调一下就写进日程」的路径。

---

## 5. 重要度分级

分数 = **规则分** + **模型分**（两次都只作为「加分项」，最后按阈值分档）：

| 档 | 阈值（`high_score` / `medium_score`，默认 60 / 30） | 表现 |
| --- | --- | --- |
| `high` | ≥ 60 | 立刻弹窗 + 念一句 |
| `medium` | ≥ 30 | 进每天两次的摘要 |
| `low` | < 30 | 静默入库，只可检索 |

**规则分**（`triage.KEYWORDS`，顺序有意义——先命中先算类别）：

| 匹配（正则） | 加分 | 类别 |
| --- | --- | --- |
| `截止|deadline|ddl|due date|最后期限|务必|过期` | +25 | deadline |
| `面试|interview|录用|offer|录取|admission|笔试|二面` | +30 | interview |
| `合同|contract|发票|invoice|报销|reimburse|账单|缴费|退款` | +20 | money |
| `退课|选课|成绩|grade|答辩|论文|thesis|导师|supervisor|课程|考试` | +15 | school |
| `会议|meeting|日程|schedule|邀请|invit|calendar|组会|例会` | +12 | meeting |
| `紧急|urgent|asap|尽快|马上|今天之内|明天之前` | +12 | deadline |

再叠加这些（`triage.rules`，顺序照代码）：

| 信号 | 加减分 |
| --- | --- |
| 发件人在 `vip_senders`（地址或域名） | +40 |
| 来自 `priority_domains`（学校/公司域名） | +15 |
| 只发给你（`to` 里有我，`cc` 里没有） | +10 |
| `importance = high`（发件人标了高重要性） | +20 |
| 带附件 | +5 |
| 主题像回复/转发（`Re:` `回复:` `Fwd:` …） | +8 |
| 你给它加过旗标 | +10 |
| 像群发/促销（`unsubscribe|退订|促销|秒杀|no-reply…`） | −30，类别标成 `newsletter` |

最后夹在 `0..100`。

**模型只看到这些**（`classify_chars`，默认 400 字）：

```
发件人：张三 <zhang@x.edu.cn>
主题：…
正文开头：…（前 400 字）
```

整封正文**不出本机**（附件正文也不给分类模型）。模型返回：

```json
{"important": 0-100, "category": "deadline|interview|money|school|meeting|personal|
 notification|newsletter|promotion|other", "needs_reply": true/false,
 "deadline": "YYYY-MM-DD 或空串", "one_line": "不超过 20 字的中文摘要"}
```

**怎么跟规则分合并**（★不是简单相加★）：

- 默认 `score = max(规则分, 模型分)` —— 模型只能**抬高**，不能把「截止今天」压下去；
- 但模型说「这是群发/通知/促销」**且**规则分 < 50 时 → `score = min(模型分, 25)`
  （治「模型把一封推销信评成 90」）；
- `category` 以模型为准（除非它说 `other`）；`needs_reply` / `deadline` / `one_line` 只由模型给。

**过期降权**（`demote_expired`，默认开）：`deadline` 已经过去的通知降一档 ——
「11 月 17 日的课调到 11 月 20 日」这种信，17 号之后就不该再弹窗。

**省 token 的三个闸**：`classify_per_sync`（每轮最多几封）、
`classify_max_age_days`（老邮件只走规则）、`classify_model`（想完全不出网就填
`ollama:qwen3.5:4b`）。

---

## 6. 发信的四道闸门

`outbox.Outbox.send()` 依次检查（**任何一条不过就发不出去**）：

| # | 闸门 | 默认 | 失败时说 |
| --- | --- | --- | --- |
| 1 | `[mail] allow_send == true` | **false** | 「发信没开（`[mail] allow_send`）」 |
| 2 | 所有收件人都在 `[mail] send_allowlist` 里 | **空表 = 谁都发不出去** | 「这个地址不在白名单里」 |
| 3 | 草稿 `state == approved` | — | 「这封还没批准」 |
| 4 | `confirm_token` 对得上 | 5 分钟过期 | 「token 过期/不对，请重新点发送」 |

### confirm_token 是怎么算的

```
payload = f"{draft_id}|{body_hash(to, cc, subject, body)}|{int(expires_at)}"
token   = HMAC-SHA256(secret, payload)[:40]
secret  = data/mail/confirm.key（第一次用时随机生成 32 字节，不进 git）
```

校验时用 `hmac.compare_digest`，并**重新算一遍正文 hash**：

- 你批准了 A 版本、agent 偷偷改成 B 版本 → hash 变了 → token 对不上 → 拒绝。
- token 由**用户点「发送」的那一刻**、在**本进程内**签发（`approve.py` 的卡片回调），
  模型拿不到、也伪造不了（它连 `secret` 都读不到）。
- `expires_at` 过了就作废（默认 300 秒，`[mail] approval_ttl`）。

审计写在 `data/mail/audit.jsonl`，一行一个事件：
`created` / `approved` / `rejected` / `sent`（含 `body_hash` 与 Graph 返回的 id）/ `send_failed`。

---

## 7. 附件

两件事分开：**清单**跟同步一起入库（只要名字和大小），**正文按需抽**（你说「看看附件」才下载）。

| 扩展名 / 类型 | 清单 | 抽正文 | 用什么 |
| --- | --- | --- | --- |
| `.txt .md .csv .json .yaml`（及 `text/*`） | ✓ | ✓ | 直接解码（UTF-8 优先，失败按 GBK） |
| `.pdf` | ✓ | ✓ | `pypdf` / `PyPDF2` |
| `.docx` | ✓ | ✓ | `python-docx` |
| `.xlsx .pptx` 图片 压缩包 | ✓ | ✗（只给清单） | — |

- 超过 `[mail] max_attachment_mb`（默认 8）只记清单、不下载。
- 抽出来的正文截到 `[mail] attachment_chars`（默认 4000 字）再给模型。
- 为什么要清单：**「上周那份 PDF 说了什么」靠附件名才搜得到**
  （`store` 里有 `idx_att_name` 索引）。

---

## 8. 邮件 → 日程

`link.EventProposer` 拿一封邮件（主题 + 正文）问模型：

```json
{"has_event": true/false, "title": "不超过 20 字", "when": "YYYY-MM-DD HH:MM 或空串",
 "place": "", "reason": "为什么觉得这是件要做的事"}
```

- `title` 要求写成「动作 + 对象」的中文短语（「提交 AIAA3102 作业3」「报名羽毛球赛」）；
- 模型不可用/超时 → 退回**规则版**（用 `triage` 抽出来的 `deadline` + 主题拼一条）；
- 提议落进 `proposals` 表（`state=pending`），弹的**是同一套审批卡**（「忽略 / 加入」）；
- 点「加入」→ `EventQueue.decide(id, "send")` → 调注入的 `add_event` → 记 `event_id`；
- 点「忽略」→ `skipped`，日程一个字都不动。

> ★为什么只提议★：模型读一封营销邮件也能编出「11 月 30 日截止」，直接写进日程就是
> 往你的时间里塞噪声。而且日程里有闹钟 —— 写错了是会在半夜响的。

---

## 9. 收件人匹配（说人话 → 地址）

`contacts.py`：一条 SQL 把「我和谁来往过」捞出来（`json_each` 拆 `to_json`/`cc_json`，
老 sqlite 回退 Python），然后打分：

| 信号 | 加分 |
| --- | --- |
| `[mail.contacts]` 里配的别名（「私人邮箱」） | +120 |
| 地址完全一致 | +100 |
| 名字/地址包含关键词 | +70 |
| 命中课程代码（`AIAA3102`，从主题里学） | +45 |
| 命中机构词（教务处 / registry / itdid …，中英映射在 `_ORG_SYNONYMS`） | 地址里 +55 / 只在主题 +30 |
| 模糊匹配（去掉元音、缩写对齐） | 0~40 |
| 往来次数 | 0~15 |

- 门槛：`MIN_SCORE=45`；第一名和第二名差 < `MIN_GAP=10` → **不猜，返回候选**，让模型去问用户
  （这条是实测调出来的：`itdid@…` 57 分 / `gzit@…` 44 分时差 13 分，一开始一直反问用户）。
- 排除：`noreply` / `do-not-reply` / 群发列表 / **你自己**（-999），免得把信发给自己。
- 配置在 `config.local.toml`（别名可能含私人信息）：

```toml
[mail.contacts]
"私人邮箱" = "someone@example.com"
"教务处"   = "registry@…"
```

---

## 10. 存储（`data/mail/index.sqlite`）

| 表 | 一行是什么 | 关键字段 |
| --- | --- | --- |
| `messages` | 一封邮件 | `id`(=Graph message id) `conversation_id` `internet_id` `subject` `sender` `sender_name` `to_json` `cc_json` `received_at` `is_read` `has_attachments` `importance` `preview` `web_link` `flagged` `body_text` `body_cached_at` `score` `level` `category` `needs_reply` `deadline` `one_line` `triaged_at` `triaged_model` `notified_at` |
| `attachments` | 一个附件 | `message_id`+`att_id`（主键）、`name` `content_type` `size` `is_inline` `text` `cached_at` |
| `drafts` | 一封待发/已发草稿 | `id` `to_json` `cc_json` `subject` `body` `reply_to` `graph_draft_id` `state` `body_hash` `approved_at` `approved_by` `expires_at` `sent_at` `error` |
| `proposals` | 一条日程提议 | `id` `message_id` `title` `when_text` `when_iso` `place` `source` `reason` `state` `event_id` |
| `meta` | 键值 | `delta_cursor`（增量游标）、`last_sync_at` / `last_sync_ok`（同步水位，重启也知道）、`owner_address` / `owner_name`（我是谁，用来排掉自己发的信） |

**检索**（`Store.search`）：中文按「多词 AND」——把 query 拆成 token，
每个 token 都要在 `subject||sender||sender_name||preview||body_text` 里出现
（`LIKE %tok%`），再按 `received_at DESC` 排。不建 FTS5：中文分词要么装 jieba、
要么用 trigram，而「几个关键词都要出现」这个语义对中文反而更稳。

**清理**：`python main.py mail prune` 删掉 `[mail] prune_days`（默认 30 天）之前的行，
只删**本地索引**，云端邮件一封不动。

---

## 11. 省 token 的六个开关（都是默认值）

| 开关 | 默认 | 省在哪 |
| --- | --- | --- |
| `first_sync_days` | 7 | 第一次只往回挖一周（不是 30 天）→ 少索引几百封 |
| `classify_per_sync` | 5 | 每轮最多分级 5 封（每封一次模型调用） |
| `classify_max_age_days` | 7 | 超过 7 天的旧邮件**只走规则**，不送模型 |
| `classify_chars` | 400 | 分类只看前 400 字 |
| `max_body_chars` | 6000 | 读正文的硬上限（`mail_read` 的默认值） |
| `prune_days` | 30 | 本地索引只留 30 天 |

另外 `uniqueBody` 裁掉引用历史、`$select` 裁字段、摘要走一句话清单而不是原文 ——
这些都是「看不见但很值钱」的省法。

---

## 12. 失败模式与排障

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| `ready()` 说没登录 | `token.json` 不存在/没有 refresh token | `python main.py mail login` |
| 登录后立刻又失效 | 应用没勾 `offline_access`，或管理员撤了同意 | 重新走 adminconsent |
| 同步一直是 0 封 | `folder` 写错（如 `收件箱` 不是 `inbox`），或租户策略拦了 | `mail status` 看 `graph_scope`；换成 `inbox` |
| ★**明明有新邮件，Agent 却说「没有新邮件」**★ | 索引落后（没同步）/ 空行（只有 id）/ 摘要只看 `high` | `python main.py mail doctor` 一眼看出来；`mail sync --full` 修 |
| 每 3 分钟全量重拉 | 游标空（见 §2.4 的 `$deltatoken` 坑） | `mail sync --full` 重建一次游标 |
| 索引里出现「空行」（主题/时间全空） | 增量载荷只有 id（游标形状不对，见 §2.4 事故） | 新代码会自动判坏并重建；也可以 `mail sync --full` 立刻清掉 |
| 分级全是 `low` | 模型不可用，只走了规则 | 看日志里 `[mail] 分类` 那行；换 `classify_model` |
| 草稿建了、点发送说「不在白名单」 | 收件人不在 `send_allowlist` | 加白名单，或临时把 `allow_send` 关掉测试 |
| 点发送说「token 过期」 | 超过 `approval_ttl`（默认 5 分钟） | 重新点一次「发送」 |
| 发出去两封 | 用了 `unknown` 状态那封再点一次 | 先去「已发送」确认；`unknown` 就是要人肉确认 |
| `410` 一直重来 | 游标过期且没重建 | 同上，`mail sync --full` |

一条命令先看全局：**`python main.py mail doctor`**（开关 / 登录 / 索引 / 空行 / 游标 / 是否够新）。

日志里**不写正文**（`service._brief` 只写主题/发件人/分级/一行摘要）。
同步那行现在是 `[mail] 同步完成：新增 X、更新 Y、删除 Z（增量 N 页）`；
出现 `补字段` / `跳过` 就说明增量里有条目字段不全（形状问题，见 §2.4）。

---

## 13. 已知限制（都是有意为之，不是"还没做"）

| 限制 | 为什么 | 绕法 |
| --- | --- | --- |
| 改了 `high_score` / `vip_senders` / `KEYWORDS` 之后，**已经入库的邮件不会自动重算** | `classify()` 命中 `triaged_at` 就直接用缓存，只有新邮件才会跑规则 | 想立刻生效：删掉 `data/mail/index.sqlite` 再 `mail sync --full`（代价是重拉一遍） |
| 一份 `data/mail/` 只对应**一个**邮箱账号 | 令牌是覆盖式的，多账号会让 `owner_address` 打架 | 换 `[app] data_dir` 跑第二份配置 |
| `unknown` 状态**不会自动重试** | 网络断在半路时不知道发没发出去，自动重试 = 可能发两封 | 去 Outlook「已发送」确认，再决定要不要重发 |
| 附件只抽 `.pdf` / `.docx` / 文本类 | `xlsx` / `pptx` / 图片要 OCR 或表格解析，收益不值当 | 只给清单（名字+大小），需要时人工打开 |
| 检索是「多词 AND 的 `LIKE`」，不是语义搜索 | 中文分词/FTS5 的复杂度换不来明显好处（§10） | 换成你记得的词；或先 `mail_read` 再看正文 |
| 分级结果只在**同步时**更新一次 | 避免每轮巡检重复烧 token | 手动 `mail digest` 只看结果，不改分 |
| 同步是**轮询**（`sync_minutes=3`），没有 webhook | 本机没有公网回调地址 | 想要更实时：调小 `sync_minutes`（下限 20 秒），或问的时候先手动 sync |

## 14. 自测覆盖（`scripts/tests/test_mail.py`）

全部打桩（假 Graph / 假 token / 假模型 / 假时钟），**不联网、不登录、不弹窗**：

1. delta 翻页与游标（含 410 重来、水位先写）
2. `$select` 裁剪与 `uniqueBody` 选取
3. 收件人匹配打分（含「分不清就反问」）
4. 分级：规则关键词 / VIP / 群发降权 / 过期降权 / 模型分夹紧
5. 发信四道闸门（逐条单测 + 组合）
6. `confirm_token`：正文改了就不认、过期不认、重放不认
7. 幂等：`sent` 再发不重复；`unknown` 不自动重试
8. 审批队列：点取消 → `rejected`；点两次发送（二次确认）
9. 提醒去重（`notified_at`）、摘要只给重要的
10. 邮件→日程：提议落库、点加入才写、点忽略写 `skipped`
11. **§19 增量形状**（2026-11-21 事故的回归测试）：只有 id 的载荷 → 判坏并重建、
    个别缺字段 → 逐条补齐、补不到 → 跳过不写空行、`@removed` → 删本地行、历史空行被清掉
12. **§20 游标形状**：铸游标时 URL 必须带完整 `LIST_SELECT`（不许 `$select=id`）
13. **§21 新鲜度与摘要措辞**：没同步过 → `stale`；同步后 → 带「索引同步于/最新一封」；
    「没有重要邮件」时必须补一句「有 N 封普通邮件」
11. MCP 工具形状（6 个工具的参数名/必填项，防止改签名把模型带偏）

---

## 15. 隐私边界

```
data/mail/index.sqlite     索引（主题 / 正文摘要 / 分级）—— 本地文件，不进 git
data/mail/token.json       access_token + refresh_token（明文 JSON；非 Windows 上 0600）
data/mail/confirm.key      HMAC 密钥（审批 token 用，32 字节随机）
data/mail/audit.jsonl      发信审计（谁批的、什么时候、发的什么 hash）
```

- **分类**只把「主题 + 发件人 + 前 `classify_chars` 字」发给模型；整封正文不出本机。
- **完全不外发**：`classify_model = "ollama:qwen3.5:4b"`（本地模型，慢一点）。
- **附件正文**只有在你要看时才下载，且截断后才给模型。
- 仓库是 **public**：任何真实地址/姓名都不要写进 `config.toml` 或文档，
  一律放 `config.local.toml`（gitignored）。

---

## 16. 扩展点

| 想做什么 | 改哪 |
| --- | --- |
| 换数据源（IMAP / 本机 Outlook） | 写一个和 `graph.Graph` 同样接口的类（`delta` / `message` / `create_draft` / `send_draft` / `attachments`），`service.py` 里换注入 |
| 换分类模型 | `[mail] classify_model`，或换 `adapters.py` 里的分类器（签名 `(system, user) -> str`） |
| 加一个 agent 工具 | `../mcp/servers/mail.py` + `tool_presets.py` 的 `mail` 预设；**description 写清楚「会不会写」** |
| 换审批界面 | 实现一个 `submit(draft) -> None` + 回调 `decide(id, "send"/"skip")` 的对象，替掉 `approve.ApprovalCard` |
| 调整「什么算重要」 | `triage.KEYWORDS` / `[mail]` 的阈值与 VIP 列表；改完跑 `test_mail.py` |
| 加 OCR / 新附件格式 | `attachments.extract_text` 里加一条分支（记得处理异常文本，别让坏文件崩整轮） |
