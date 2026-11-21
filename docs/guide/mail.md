# 邮件（Outlook / Microsoft Graph）

一句话：**大肥鱼能替你收信、读信、起草回信，但「发送」这一下必须你点。**

真正实现是一个子系统而不是「一个工具」：要登录、要增量同步、要把一百封邮件分出
轻重、要在右下角弹审批卡、要记审计 —— 所以代码在 [`voice_loop/mail/`](../../voice_loop/mail/)。

| 想知道什么 | 看哪 |
| --- | --- |
| 怎么注册应用、怎么登录、日常生活怎么用 | **本文件**（往下看） |
| 和 Outlook 的接口（Graph 端点、delta、scope、错误码）、6 个工具的签名、
数据表结构、闸门与 token 怎么算、坏了怎么查 | [`voice_loop/mail/README.md`](../../voice_loop/mail/README.md) |

## 能干什么

| 你说 | 会发生什么 |
| --- | --- |
| 「有没有重要的邮件」 | 查本地索引，念出「谁 + 什么事 + 要不要回」 |
| 「学校发了什么通知」 | 关键词检索 → 读正文（自动去掉引用的历史） |
| 「帮我回他，说周五交」 | **只建草稿** → 右下角弹卡片 → 你点「发送」才真发 |
| 「作业3什么时候交」 | 读正文 + **附件清单**；需要就问要不要抽附件里的文字 |
| 「把这个截止日期记进日程」 | **只生成提议** → 卡片变「加入 / 忽略」→ 你点了才写日程 |
| （后台）重要邮件进来 | 右下角弹窗 + 念一句「谁发来一封邮件：……」；同一封只提醒一次 |
| （后台）早上 9 点 / 晚上 8 点 | 攒一份摘要：「有 N 封值得看的邮件：……」 |

不重要的（促销、群发、通知类）**静默入库**：不打扰，但你问起来答得出。

## 第一步：注册一个应用（10 分钟，一次就够）

不需要服务器、不需要密钥，走**设备码**（device code）登录：程序显示一串短码，
你在浏览器里输进去、点同意即可。刷新令牌存在本机，之后自动续期。

1. <https://entra.microsoft.com> → **应用注册** → 新注册
   - 名称：随便（例：`local-ai-mail`）
   - 受支持的账户类型：**取决于你的邮箱**（见下表）
   - 重定向 URI：**不用填**
2. 打开刚建好的应用 → **身份验证** → 拉到底 → **允许公共客户端流** → 选「是」
3. 打开 **清单（Manifest）**，改两个字段：

```json
"signInAudience": "AzureADMyOrg",
"api": { "requestedAccessTokenVersion": 2 },
"isFallbackPublicClient": true
```

4. 复制**应用程序(客户端) ID** 和**目录(租户) ID**，写进 `config.local.toml`：

```toml
[mail]
client_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
tenant    = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"   # 或 "consumers" / "organizations"
```

5. 登录：

```powershell
python main.py mail login
```

屏幕会打印一串短码（例：`ABCD-EFGH`）并给个网址；打开网址、把短码粘进去、同意。

> ★`client_id` 不是密钥★，但它是你的应用标识，所以放 `config.local.toml`（已 gitignore）。

### 账户类型 ↔ 受众 ↔ `tenant` 对照

| 你的邮箱 | 应用里的「受支持的账户类型」 | manifest `signInAudience` | `[mail] tenant` |
| --- | --- | --- | --- |
| outlook.com / hotmail / live | 仅个人 Microsoft 账户 | `PersonalMicrosoftAccount` | `consumers` |
| 公司/学校（Microsoft 365） | 仅此组织目录中的账户 | `AzureADMyOrg` | 目录(租户) ID |
| 两者都想用 | 任何组织目录 + 个人账户 | `AzureADandPersonalMicrosoftAccount` | `organizations` |

对不上会报这些码：

| 错误码 | 意思 | 怎么办 |
| --- | --- | --- |
| `AADSTS700016` | 这个目录里找不到这个应用 | `tenant` 写错了，或受众不匹配 |
| `AADSTS9002346` | 应用只配了个人账户 | `signInAudience` 改成含组织的值 |
| `AADSTS9002332` | 应用只配了组织账户 | 同上，改成含个人的值 |
| `AADSTS7000218` | 没开「允许公共客户端流」 | 第 2 步那里选「是」 |
| `AADSTS65001` / 门户上「需要管理员同意 = 是」 | 租户不允许用户自己同意 | 见下面的「学校/公司租户」 |

### 学校 / 公司租户（重要邮件被挡在这里）

学校账号（例如 `xxx@connect.hkust-gz.edu.cn`）通常要求**管理员同意**才能给应用授权。
如果你的账号不是管理员，门户上「授予管理员同意」是灰的、登录会卡在「需要批准」。
三条路，选一条：

| 方案 | 做什么 | 代价 |
| --- | --- | --- |
| **A. 转发到个人邮箱**（最省事） | 学校邮箱设置「自动转发到我的 outlook.com」→ `tenant` 改 `consumers`、受众改含个人账号 → 用个人账号登录 | 转发可能被学校策略禁用；附件/格式偶有损失 |
| **B. 让 IT 批一次**（本项目实测走的就是这条） | 把管理员同意链接发给 IT：`https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<client_id>&scope=https%3A%2F%2Fgraph.microsoft.com%2FMail.ReadWrite%20https%3A%2F%2Fgraph.microsoft.com%2FMail.Send%20https%3A%2F%2Fgraph.microsoft.com%2FUser.Read` —— 管理员点一次「接受」即可 | 要等审批（HKUST-GZ 实测：发邮件当天就批了） |
| **C. 装经典 Outlook** | 走本机 COM/MAPI，完全不碰 Graph | 需要经典 OUTLOOK.EXE；**新版 `olk.exe` 不支持 COM**，Office 家庭版也不含 Outlook |

> 想更规范：先在 **API 权限**里把 `Mail.ReadWrite`、`Mail.Send` 加进「Microsoft Graph → 委托的权限」，
> 这样管理员在同意页面上看到的是明确清单，而不是链接里临时带的 scope。

## 日常命令

```powershell
python main.py mail status              # 登录状态 + 索引统计 + ★新鲜度★（同步于/最新一封）
python main.py mail doctor              # 一键体检：开关/登录/索引/空行/游标/是否够新
python main.py mail sync                # 立刻同步一次（服务在跑时也会每 3 分钟自动同步）
python main.py mail sync --full         # 丢掉游标、按 7 天窗口重建（索引不对时用这个）
python main.py mail search 选课 --limit 5
python main.py mail search --unread --level high
python main.py mail read <邮件id>
python main.py mail digest              # 摘要（默认只要 medium 以上的）
python main.py mail drafts              # 看待批草稿
python main.py mail approve <草稿id>     # ★批准 = 真发送★
```

服务（`python main.py listen`）里这些是自动的：巡检、分级、提醒、审批卡弹窗。

### 她说「没有新邮件」，可信箱里明明有 —— 先跑 `mail doctor`

模型只能看见**本地索引**（`data/mail/index.sqlite`），索引不是实时的：

| 症状 | 意思 | 怎么办 |
| --- | --- | --- |
| `doctor` 说「空行 N 行」 | 以前同步把一些邮件写成了只有 id 的空行 | `python main.py mail sync --full`（会顺手清掉） |
| `doctor` 说「索引足够新 ×」 | 服务没在跑 / 刚重启还没同步 / 上次同步失败 | 确保服务在跑；或手动 `mail sync` |
| 工具结果里带「★这份索引可能不是最新的★」 | 模型看到的就是这句提示 | 它现在会照实说"我这边可能没跟上"，而不是断言"没有" |
| 摘要说「没有重要邮件，但有 N 封普通邮件」 | 只是没有 `high` 级的 | 让它 `mail_summary only_important=false` 或 `mail_search` |

想看"到底同步到什么时候"：`python main.py mail status` 里的 **最新一封** / **上次同步**
两行，控制台「概览」页的技术配置一览里也有同一组数字。

## 省 token（默认就很抠）

分级是**唯一花钱**的环节（每封 = 一次模型调用），所以三处都收着：

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `first_sync_days` | **7** | 首次只往回挖一周；之后走增量游标，不重复拉历史 |
| `classify_per_sync` | **5** | 每轮巡检最多分级 5 封（多出来的下一轮再说） |
| `classify_max_age_days` | **7** | 超过 7 天的旧邮件**只走规则**，绝不送模型 |

另外：读信、搜索、摘要、附件抽取全是**本地**动作；附件正文只有你问到那封时才下载。

想再省一点：

```powershell
python main.py mail prune --days 14      # 清掉本地索引里 14 天前的（Outlook 里的邮件不动）
```

或者干脆把分级换成本地模型（零外发、零费用）：

```toml
[mail]
classify_model = "ollama:qwen3.5:4b"
```

## 附件

**同步时不拉附件**，只记「这封带附件」；你问到它的时候才按需下载并抽文本。

| 能不能抽正文 | 类型 |
| --- | --- |
| 能 | `.txt .md .csv .json .yaml .log .html`、`.pdf`（要 `pypdf`）、`.docx`（要 `python-docx`） |
| 不能（只给清单） | 图片、`.xlsx`、`.pptx`、压缩包、扫描版 PDF（没有 OCR） |

- 附件名进本地索引：「那份成绩单 PDF」也能搜到。
- 抽出来的文本按 `[mail] attachment_chars`（默认 4000 字）截断；超过
  `max_attachment_mb`（默认 8 MB）的不下载，只留清单并注明「太大」。
- 抽不出来就直说抽不出来 —— 模型不会去猜扫描件里写了什么。

## 过期降权

截止时间**已经过去**的邮件自动降一档（high → medium → low），理由里写明「截止时间已过（11-17）→ 降一档」。
典型场景：周三才看到的「周一课调到下午三点」，不该再弹窗提醒你。

```toml
[mail]
demote_expired = true
```

## 邮件 → 日程（写日程同样要你点头）

邮件里常有「11月24日 23:59 前提交」「12月14日 期末考试期开始」这类信息。流程和发信一个道理：

```
mail_to_event(message_id)  →  只生成一条「待批提议」  →  右下角卡片  →  你点「加入」才写进 data/events.json
```

- 卡片是**同一张**，只是按钮变成「加入 / 忽略」、第一行变成「时间」。
- 模型侧被明确要求：从邮件读到的事**只能用 `mail_to_event`**，不许绕过卡片直接 `add_event`。
- 时间由 `event_text.parse_datetime` 解析，认 `2026-11-24 23:59` / `11月24日` / `明天下午三点`。
- 你点「忽略」→ 记为 `skipped`，什么都不写（不影响原邮件）。

```toml
[mail]
link_events = true      # 允许这个能力
auto_propose = false    # 重要邮件自动生成提议（默认关，免得老弹卡）
```

## 让大肥鱼用上它（工具集怎么配）

邮件工具是 **hidden** 的：只有人格文件里 `api_tools` 点名了 `mail` 的 API 角色才看得到
（本地 4B 角色看不到 —— 它连 11 个工具都选不准）。

```json
// data/personas/bluefish.json
"api_tools": ["mail", "web", "weather"]
```

★**别一次开太多**★：实测工具数到 22 个（`mail` + `web` + `weather` + `files`）时，
`deepseek-chat` 会把「我的邮箱里有什么重要邮件」错选成 `list_memos`；砍到 18 个就稳定选
`mail_summary`。要读工作区文件再加 `"files"`，要写文件/执行命令再加 `"write"` / `"shell"`。

**这 6 个工具分别干什么**（细节和参数说明见
[`voice_loop/mail/README.md`](../../voice_loop/mail/README.md) §3）：

| 工具 | 你的一句话 | 会不会写 |
| --- | --- | --- |
| `mail_search` | 「学校发的通知」「谁给我发过…」 | 只读本地索引 |
| `mail_read` | 「那封邮件说了什么」「念一下截止时间」 | 只读（引用历史已裁掉） |
| `mail_summary` | 「有没有重要邮件 / 有没有要回的」 | 只读 |
| `mail_draft` | 「给 AIAA3102 的老师发封邮件说…」 | **只建草稿**，发不出去 |
| `mail_to_event` | 「把这个截止时间加到日程里」 | **只提议**，你在卡片上点「加入」才写日程 |
| `mail_send` | （正常不该由它调） | 要有审批卡签发的 token 才发得出去 |

试一下（不用麦克风）：

```powershell
python main.py --character bluefish ask "我的邮箱里有什么重要邮件" --no-tts
```

```
[路由 model / 工具 mail_summary({'since_hours': 24}) ]
主人～最近一天就一封要紧的：AIAA3102 的老师在上午 11:15 发了期末项目的评分标准，记得点开看看哦～
```


## 收件人匹配（说人话，不编地址）

Outlook 里写收件人有个「最近联系人」下拉，我们这边没有 —— 所以**说名字就行**，
系统从**你自己的邮件往来**里找那个地址（不发出去、不查在线通讯录）：

| 你说 | 解析成 | 依据 |
| --- | --- | --- |
| 给 **AIAA3102 的老师**发邮件 | `cx@hkust-gz.edu.cn` | 这门课的信是他发的（往来邮件里带课程代码） |
| 给**教务处**发 / **IT 部门** / **就业中心** | `registrar@…` / `itdid@…` / `careercenter@…` | 中文机构词 ↔ 英文地址名（教务处=registrar、就业=career…） |
| 发到**私人邮箱** | `whoever@163.com` | 你在 `config.local.toml` 里写的别名 |
| 直接写地址 | 原样用 | — |

规则（都在 `voice_loop/mail/contacts.py`，自测钉着）：

1. **绝不瞎编**：认不出就返回候选 + 「问用户是哪一个」的提示，**草稿不会建**。
2. **不选机器人**：`noreply@` / `notifications@` / `mailer-daemon` 一律排除
   （真机踩过：Canvas 通知机器人因为「发件人名字是课程名」被选中）。
3. **不选群发名单**：`all-students@` / `*-staff@` 之类不当作个人。
4. **不选你自己**（除非你说「发给我自己」）。
5. 分不清就**问**：第一二名分数太接近时把候选念出来让你选。

```toml
# config.local.toml —— 说法 → 地址
[mail.contacts]
"私人邮箱" = "whoever@163.com"
"导师" = "prof@school.edu"
```

★别名会写进 `mail_draft` 的 `to` 参数说明里★（`已配置：「私人邮箱」= …`），
所以模型看到「私人邮箱」会直接把原话放进 `to`，不会反问你要地址
（踩过：只告诉它「有这个别名」而不给地址，它会一本正经地问"发到哪个邮箱"）。

## 正文由 Agent 补全（礼貌 / 语言 / 落款）

你只要说个大概（「问他能不能延期一天」），模型负责写成能直接发出去的信：
称呼、事由、请求、礼貌收束、**落款**。

- **语气**：「礼貌一点」「正式一点」→ 加敬语、少用省略号和感叹号
- **语言**：「用英文」→ 整封英文（Dear / Best regards，称呼和落款一起换）
- **长度**：「简短点」→ 三行以内
- **落款**：默认用 `[mail] signature`；没配就用你在 Outlook 里的显示名
  （第一次发信时自动读一次，例：`Zixiang WANG`）。想换就说「落款写老王」。

## 发给谁 —— 三道闸门

默认**一封都发不出去**，要让 agent 能发信，得在 `config.toml` 里显式开：

```toml
[mail]
allow_send = true
send_allowlist = ["@hku.hk", "mom@example.com"]   # 支持整地址和 @域名
```

| # | 闸门 | 不过会怎样 |
| --- | --- | --- |
| 1 | `allow_send = true` | 报「发信功能没开」 |
| 2 | 收件人在 `send_allowlist` 里 | 报「收件人 xxx 不在白名单里」 |
| 3 | 状态是「已批准」（你点了按钮） | 报「还没批准」 |
| 4 | `confirm_token` 对得上（绑定正文 hash，5 分钟过期） | 报「确认码对不上」/「正文被改动过」 |

第 4 条的意思是：**你批准的是「这一版」正文**。批准之后草稿被改了一个字，就发不出去。

审批卡长这样（右下角，60 秒不动 = 自动取消，草稿留着下次还能批）：

```
┌ 要发一封邮件 ─────────────────────────────┐
│ 收件人：prof@x.edu.cn                     │
│ 主题：关于周报                             │
│ ─────────────────────────────────────     │
│ 老师您好…（前 300 字）    30 秒后自动取消   │
│              [ 发送 ]  [ 取消 ]            │
└───────────────────────────────────────────┘
```

想改内容就接着说「把这封改成明天发」——模型会重新起草一份新草稿，再弹一次卡。

## 重要度怎么分

规则先算一遍分（可解释），再用模型补一句「一句话概括 + 要不要回 + 截止时间」：

| 加分 | 减分 |
| --- | --- |
| 发件人在 `vip_senders` | 像群发 / 促销（含「退订」等） |
| 命中「会议/截止/面试/选课/缴费…」 | 噪音词（订阅、优惠、活动） |
| 只发给你（不是群发名单） | — |
| 来自 `priority_domains` | — |

`≥ high_score` → 立刻提醒；`≥ medium_score` → 进摘要；其余静默入库。
分级只把「主题 + 发件人 + 前 `classify_chars` 字」发给模型，**整封正文不出本机**。
想零外发：`classify_model = "ollama:qwen3.5:4b"`。

## 数据放在哪

```
data/mail/index.sqlite    索引（本地；含附件清单与抽到的附件正文）
data/mail/token.json     登录令牌（本机；**明文 JSON**，非 Windows 上 chmod 0600）
data/mail/audit.jsonl     发信审计：谁批的、什么时候、正文 hash
data/mail/confirm.key     审批 token 的 HMAC 密钥（32 字节随机，第一次用时生成）
```

整个 `data/mail/` 已在 `.gitignore` 里。删掉它就等于「忘掉所有邮件」，不影响 Outlook 本身
（但会要重新登录一次）。★令牌没有加密★：它和 `config.local.toml` 一个级别 ——
要更强的隔离就把 `data/mail/` 放到加密盘。

## 常见问题

**Q：为什么不能让它自己发？**
发信是不可逆的外部动作。这里把「批准」做成代码路径上的一步（HMAC token，
由用户在弹窗点按钮时签发），而不是提示词里的一句请求 —— 模型没有伪造路径。

**Q：发完提示「结果不确定」怎么办？**
说明 HTTP 断在半路，可能发出去了也可能没。程序**不会自动重试**（避免发两封），
请去 Outlook 的「已发送」里确认。

**Q：服务里没弹审批卡？**
没有图形环境 / Tk 起不来时退化成命令行审批：`python main.py mail drafts` + `mail approve <id>`。
日志里会有 `[mail] 审批卡起不来` 一行。

**Q：一台机器能给两个账号用吗？**
一份 `data/mail/` 只对应一个账号（令牌覆盖）。要多账号就把 `[mail]` 整段换掉、
或改 `[app] data_dir` 跑第二份配置。

**Q：说「没有新邮件」但其实有？**
先 `python main.py mail doctor`。最常见两种：① 服务没在跑（巡检不跑，索引就停在那）；
② 索引里有"空行"（2026-11-21 那次同步事故：增量只带 id，写进去的是空壳）。
两种都用 `python main.py mail sync --full` 修，前者再确保服务在跑。
现在 `mail_search` / `mail_summary` 的结果末尾都会带「索引同步于 …，最新一封 …」，
索引落后时会明说「可能不是最新的」，所以模型不会再一口咬定"没有"。
