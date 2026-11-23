# `voice_loop/memory/` —— 记忆子系统（可以整目录拿走）

> 这一个目录回答三个问题：**记住什么、什么时候整理、怎么忘**。
> 它**不认识** ASR / TTS / 技能 / 管线（`scripts/tests/test_module_boundaries.py` 守着边界）。
> 允许依赖的只有：`store`（落盘）/ `text`（分词）/ `persona`（只读名字）/ `events`（提醒）/
> `harness`（做梦用的离线 agent）。反过来，管线只通过 `MemoryHub` 一个入口跟它打交道。

## 目录地图

| 文件 | 管哪一层 | 关键接口 | 一句话 |
| --- | --- | --- | --- |
| `__init__.py` | 门面 | `MemoryHub` / `Memory` | 一个角色一份 `Memory`，`MemoryHub` 按 id 分发；`recall` / `remember` / `consolidate` / `stats` 都在这 |
| `context.py` | L1 | `ContextLog`：`append` / `render` / `seal` / `write_digest` / `pending_tokens` / `cache_stats` | 「今天聊到哪儿了」。不可变段 + 追加尾，是前缀缓存能命中的前提 |
| `dream.py` | 整理 | `Dreamer.run` / `DreamScheduler` / `undo` | 做梦：把 L1 压成摘要、把该记的提到 L2/L3；**三条触发**见下 |
| `extract.py` | L1→L2 | `read_session` / `episodes_from_session` / `facts_from_text` / `rule_summary` | 从原始对话里抽情节和事实（规则为主，可选本地模型补摘要） |
| `levels.py` | L2/L3 | `merge_episodes` / `compact_episodes` / `prune_episodes` / `promote_to_facts` / `prune_facts` | 自清洁：合并、按半衰期衰减、提升成事实、超出上限才淘汰 |
| `retrieve.py` | 查 | `parse_when` / `rank` / `keywords_of_query` | 双路检索：内容相似度 + 中文时间（「上周三」） |
| `knowledge.py` | L4 | `build_knowledge` / `KnowledgeBase.search` | 共享 / 世界观组 / 角色专属 三层叠加 |
| `model.py` | 数据结构 | `Episode` / `Fact` / `Chunk` / `Hit` | `Fact` 带**双时间**（`valid_at` / `invalid_at`），换新不删旧 |
| `store.py` | 落盘 | `MemoryPaths` / `read_json` / `write_json` / `append_jsonl` / `rewrite_jsonl` | 原子写 + 跨进程文件锁（控制台和语音服务会同时写） |
| `sync.py` | 上云 | `MemorySync` / `sync_memory` / `memory_root` | 镜像到**私有** git 仓库；公开仓库一律拒推 |
| `schedule.py` | 日程 | `SharedSchedule.upcoming` | 记忆只**读**日程（写日程只走技能），避免两边各写一份 |

## 五层记忆

| 层 | 人话 | 存在哪 | 谁维护 |
| --- | --- | --- | --- |
| **L1 活动上下文** | 今天聊到哪儿了 | `data/memory/<角色>/context/`（段 + 摘要 + 状态） | `context.py` |
| **L1' 滑窗日志** | 原始对话（会被清） | `sessions/session-*.jsonl` | 管线写、`prune_raw_sessions` 清 |
| **L2 情节** | 「记得那件事」 | `episodes.jsonl` | `extract.py` / `levels.py` / `dream.py` |
| **L3 事实** | 名字/偏好/习惯（双时间） | `facts.json`（`invalid_at` 保留旧值） | `levels.py` / `dream.py` |
| **L4 知识** | 世界观/资料 | `data/knowledge/**` + 人格卡 `world` | `knowledge.py` |

**为什么要分五层**：不同的话该用不同方式忘。刚说的（L1）随时会被压缩；「上周三见过导师」（L2）
按半衰期慢慢变淡；「你叫什么、喝美式」（L3）**永不衰减**；世界观（L4）根本不该被「淘汰」碰到。

## 一条对话的旅程

```
麦克风 → ASR → 管线
                 │
                 ├─ sessions/session-*.jsonl        （L1' 原始日志，给排查和归档用）
                 └─ context/segments/seg-*.jsonl    （L1 上下文：uid 去重，只追加）
                          │
     提示词 ←────────────────┤   render()：事实(L3) → 往日摘要 → 最近原文（尾预算内）
                          │
              （深夜且够长 / 积得太长 / 你说「整理一下」）
                          ↓
                     Dreamer.run()
                    ╱            ╲
             rule 版              agent 版（云端角色）
        封段+规则抽取+规则摘要    读对话 → 提结构化提议 → 五道校验 → 执行
                    ╲            ╱
             digests/<date>.json（摘要，进明天的提示词）
             episodes.jsonl / facts.json（长期记忆）
             dreams/<date>.json（前镜像，`--undo` 用）
```

## L1：为什么「封存后一个字节都不改」

云端的前缀缓存（DeepSeek 的上下文缓存）按**前缀字节**匹配，命中部分只要 1/10 价。
所以：**旧段不动、新内容只往后追加**，白天每一轮请求的前缀都一模一样 → 一直命中。
提示词的排列顺序就是照这个要求排的：

```
事实（L3，一天最多变一次） → 往日摘要（做梦产出，一天追加一次） → 最近原文（只追加）
```

细节：

- **`uid` 去重**：`turn_uid(ts, role, text, session)`，同一轮重复 `append` 不会进两遍
  （管线重试、上下文重建都会撞上）。
- **段的封存条件**：`segment_tokens`（1200）或 `segment_turns`（30）任一到了就封。太小 → 前缀碎片化；
  太大 → miss 一次更贵。
- **尾预算**：`tail_tokens`（2000）/ `local_tail_tokens`（800）—— 按**整轮**砍，绝不切半句。
  本地 4B 窗口小、注意力弱，给它塞 2000 token 的原文反而更糊涂。
- **缓存记账**：`record_usage()` 把真实 `usage.prompt_cache_hit_tokens` 记下来，
  `cache_stats()` 能算命中率 —— 控制台「上下文与整理」卡片显示的就是它。

## 做梦：什么时候才会做（★2026-11-20 改过★）

以前是「每晚 `02:00-05:00` 到点必跑一次」，哪怕当天只说了三句话 —— 起一次 agent 梦要
读一遍上下文、最多 6 轮工具调用、改 24 条记忆，**这是最贵的一笔开销，而收益几乎为零**。
现在**只有三个触发**（外加一个不花钱的补丁）：

| # | 条件 | 跑法 | 说明 |
| --- | --- | --- | --- |
| ① | 深夜窗口 **且** 攒够 `night_min_tokens` | agent | **两个条件同时满足才做**。窗口里随机挑一分钟（`random`），一天最多一次；不够长就只记一句「今天没攒够」 |
| ② | 任意时刻攒够 `hard_tokens` | rule | 保命：不压就会把上下文顶爆。规则版毫秒级、**不调模型** |
| ③ | 模型主动调 `request_dream` | agent | 你说「你整理一下记忆」 |
| ④ | 服务退出时 | rule | `compress_on_close`。不调模型、不花钱，只为防止「23 点关机、永远赶不上 02:00 窗口」→ 那天的对话永远变不成摘要 |
| — | `python main.py dream` | 跟角色 | 手动扳机，排查时用 |

**「空闲 20 分钟就自己醒一次」已经删掉**：它和③功能重复，而且**不请自来** ——
聊完刚起身倒杯水，它就开始烧 token 了。

### 80000 / 240000 是怎么定的

云端 API 的上下文窗口有 1M，这两个数按窗口的 **8% / 24%** 取，给长对话留足空间
（1 token ≈ 1.3~1.5 个汉字，一轮日常对话约 100~300 token）：

- **80000 token ≈ 连续几天的长聊（几百轮）** —— 攒到这个量才值得让模型读一遍、把该记的提炼进 L3。
  一天只说三五句（<1000 token）→ 不触发，一分钱不花。
- **240000 token ≈ 1M 窗口的 24%** —— 到这儿必须压，但只用规则版，不花模型的钱。

换算成体感：**平时正常用不会触发**；内容先按天累积，攒够了才有夜里那一次 agent 梦。

### 两种跑法

| 跑法 | 谁用 | 干什么 |
| --- | --- | --- |
| `rule` | **本地角色默认** | 不调模型：封段 + 规则抽取 `episodes_from_session` + 规则摘要 |
| `agent` | 云端角色（或 `model = "api:…"`） | `read_turns` / `query_memory` / `done` 三个工具 → 提议 → 五道校验 → 落盘 |

`agent` 失败**自动退回** `rule`，绝不出现「梦没做完还把记忆改坏」。

### 五道校验（防「整理」把记忆改坏）

1. `pinned` 的身份事实（名字、称呼）**不许** `forget` / `supersede`；
2. `promote` 的 `evidence_seq` 必须是**对方说过的**那一轮 —— 工具/网页输出不能当证据
   （防 poisoned consolidation）；
3. `(key, value)` 已经记过 → 判 NOOP，不重复加；
4. 一次梦最多改 `max_actions`（24）条；
5. 改之前先存前镜像 `dreams/<date>.json`，`--undo` 能**整批回滚**。

## L2/L3：自清洁在做什么

`Memory.consolidate()`（退出时、做梦后都会跑一次，纯规则、不花钱）：

| 动作 | 规则 | 为什么 |
| --- | --- | --- |
| 合并 `merge_episodes` | 话题重叠足够高 + 时间相近（`MERGE_WINDOW_HOURS`）→ 合成一条 | 「今天调了记忆系统」说八遍不该是八条 |
| 衰减 `salience_eff` | 按半衰期算**有效**重要度 | 旧事自然变淡，但 `pinned` 永不衰减 |
| 提升 `promote_to_facts` | 反复出现、跨会话稳定 → L2 升到 L3 | 「我喝美式」说三次就该是事实，不是一条情节 |
| 淘汰 `prune_*` | 只在**超过上限**时按有效重要度从低到高删（`max_episodes` / `max_facts`） | 上限是保险，不是日常删除 |
| 双时间 | `supersede_fact` 给旧值写 `invalid_at`，不删 | 「我以前喝什么」还答得上来 |

## L4 知识库：三层叠加

`build_knowledge()` 把三层叠起来，越靠后越「专属」：

1. **共享**（`knowledge_paths` 里所有角色都能读的文件/目录）
2. **世界观组**（同一个 IP 的多个角色共享一份，不必拷文件）
3. **角色专属**（人格卡里的 `world` 或该角色自己的目录）

`search()` 按关键词覆盖度打分；`merge()` 用来把世界观组并进角色视图。

## 检索：双路

`recall(query, when=...)` = **内容路**（分词 → 覆盖度 → 新鲜度加成）+ **时间路**（`parse_when`
解析「上周三 / 前天 / 这个月」成区间，落在区间里的加权）。`min_score=0.12` 拦掉噪声命中 ——
「记不起来」比「编一条给你」好。

## 云端同步（`sync.py`）与隐私

- 只同步**每角色一份**的记忆：`episodes.jsonl` / `facts.json` / `sessions.json` / `knowledge/` /
  `context/`；**原始对话 `sessions/*.jsonl` 不上云** —— 它是会过期的工作记忆，也最隐私。
- 推送前会查远端仓库可见性，**public 一律拒推**（`allow_public=True` 才放行）。
- 拉回来是**合并**不是覆盖：episodes 按 `id`、facts 按 `(key, valid_at)`、上下文段**按 `uid`**
  （两台机器各自追加过同一段时，整段覆盖会丢行）。
- `memory_root(settings)` 是路径解析的唯一入口：`[memory] dir` 留空时跟 `[skills] data_dir` 走
  （测试/多开把 data_dir 指到临时目录时，记忆自动隔离 —— 踩过：自测的请求被真实服务捡走）。

## 公开契约

```python
from voice_loop.memory import MemoryHub

hub = MemoryHub(settings, schedule=..., llm_call=..., character_knowledge=...)
mem = hub.for_character("baize")

mem.context.render(facts_text, digest_days=3, tail_tokens=2000)   # 提示词里的三段
mem.context.append([{"ts":…, "role":"user", "text":…}])           # 每轮追加（uid 去重）
mem.recall("导师", when="上周")                                    # 双路检索
mem.remember("记住：周五三点见导师")                                # 记一条
mem.consolidate()                                                 # 自清洁
mem.supersede_fact(key, value)                                    # 换新（旧值标失效）
hub.recall_everywhere("火锅")                                      # 跨角色查（"上次谁说过来着"）
```

做梦：

```python
from voice_loop.memory.dream import Dreamer, DreamScheduler, undo

Dreamer(settings, hub, "baize").run(source="rule")   # 规则版；agent 版由 llm_factory 决定
undo(settings, hub, "baize", "2026-11-20")           # 整批回滚那次梦

sched = DreamScheduler(settings, hub, make_dreamer=make, character=lambda: "baize")
sched.request(reason="她自己想整理一下", source="manual")   # 预留：随机时间唤醒
```

## 配置面

```toml
[memory]              # 开关 / 注入字数 / 归档与自清洁 / 上限 / 知识库路径 / 云端同步
[memory.context]      # L1：段大小、摘要天数、尾预算（本地模型单独一档更小）
[memory.dream]        # 做梦：window + night_min_tokens + hard_tokens + compress_on_close …
```

`[memory.dream]` 逐项：

| 键 | 默认 | 意思 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `window` | `"02:00-05:00"` | 深夜窗口（条件①的一半） |
| `random` | `true` | 窗口里随机挑一分钟，不是准点 |
| `night_min_tokens` | `80000` | **条件①的另一半**：没攒够就跳过（省钱的主要开关） |
| `hard_tokens` | `240000` | 条件②：积到这么多就压（规则版） |
| `compress_on_close` | `true` | 条件④：退出时补一次规则版 |
| `min_turns` | `4` | 少于 4 轮不值得压 |
| `min_interval_minutes` | `30` | 两次 agent 梦之间至少隔多久 |
| `model` | `""` | 空 = 跟角色引擎；`"api:deepseek-chat"` = 整理用更强的模型 |
| `max_rounds` / `max_actions` | `6` / `24` | agent 版的上限 |
| `allow_tool_call` | `true` | 允许模型调 `request_dream`（条件③） |
| `keep_days` / `sync_after` | `60` / `true` | 可撤销记录保留天数 / 梦完推云端 |

## 单独优化它

```powershell
python scripts\tests\test_memory.py            # 分级 / 自清洁 / 检索 / 隔离
python scripts\tests\test_memory_dream.py      # 日上下文 / 做梦 / undo / 三种触发
python scripts\tests\test_memory_mcp.py        # MCP 工具 + 知识库分层
python scripts\tests\test_memory_sync.py       # 上云（临时 bare 仓库，不联网）
python main.py memory --recall "导师" --when 上周
python main.py dream --dry-run                  # 只算不改
python main.py memsync --status                 # 云端有没有没推的
```

排查「为什么昨晚没做梦」：看 `data/memory/_dream_schedule.json` ——
`at` 是今天挑的时间、`done` 说明做过、`short_noted` + `short_detail` 说明**到点了但没攒够**
（`52000/80000 token、3/4 轮` 这种就是它）。

## 四条不变量

1. **段封存后一个字节都不改** —— 这是「白天提示词前缀稳定、云端缓存能命中（省 90% 输入成本）」的唯一实现方式。
2. **压缩 ≠ 删除**：封存的段还在盘上，`recall` 照样查得到。
3. **做梦是「整理」不是「重写」**：五道校验 + `--undo` 整批回滚。
4. **镜像到云端是「本地→云端镜像、云端→本地合并」**，且**公开仓库一律拒推**。
