# 邮件（Outlook / Microsoft Graph）

一句话：**大肥鱼能替你收信、读信、起草回信，但「发送」这一下必须你点。**

真正实现是一个子系统而不是「一个工具」：要登录、要增量同步、要把一百封邮件分出
轻重、要在右下角弹审批卡、要记审计 —— 所以代码在 [`voice_loop/mail/`](../../voice_loop/mail/)，
对外只有 5 个 MCP 工具（`mail_search` / `mail_read` / `mail_summary` / `mail_draft` / `mail_send`）。

## 能干什么

| 你说 | 会发生什么 |
| --- | --- |
| 「有没有重要的邮件」 | 查本地索引，念出「谁 + 什么事 + 要不要回」 |
| 「学校发了什么通知」 | 关键词检索 → 读正文（自动去掉引用的历史） |
| 「帮我回他，说周五交」 | **只建草稿** → 右下角弹卡片 → 你点「发送」才真发 |
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
python main.py mail status              # 登录状态 + 索引统计
python main.py mail sync                # 立刻同步一次（服务在跑时也会每 3 分钟自动同步）
python main.py mail search 选课 --limit 5
python main.py mail search --unread --level high
python main.py mail read <邮件id>
python main.py mail digest              # 摘要（默认只要 medium 以上的）
python main.py mail drafts              # 看待批草稿
python main.py mail approve <草稿id>     # ★批准 = 真发送★
```

服务（`python main.py listen`）里这些是自动的：巡检、分级、提醒、审批卡弹窗。

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

试一下（不用麦克风）：

```powershell
python main.py --character bluefish ask "我的邮箱里有什么重要邮件" --no-tts
```

```
[路由 model / 工具 mail_summary({'since_hours': 24}) ]
主人～最近一天就一封要紧的：AIAA3102 的老师在上午 11:15 发了期末项目的评分标准，记得点开看看哦～
```


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
data/mail/index.sqlite    索引（本地）
data/mail/token.json     登录令牌（本机；Windows 上尽量加密存放）
data/mail/audit.jsonl     发信审计：谁批的、什么时候、正文 hash
data/mail/secret.bin     审批 token 的 HMAC 密钥
```

整个 `data/mail/` 已在 `.gitignore` 里。删掉它就等于「忘掉所有邮件」，不影响 Outlook 本身。

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
