# `voice_loop/memory/` —— 记忆子系统（可单独拿走）

> 这一个目录就是「记住什么、忘掉什么」。它**不认识** ASR / TTS / 技能 / 管线
> （`scripts/tests/test_module_boundaries.py` 守着）。允许依赖：`store` / `text` /
> `persona`（只读名字）/ `events`（提醒）/ `harness`（做梦用的离线 agent）。

## 五层记忆

| 层 | 人话 | 存在哪 | 谁在维护 |
| --- | --- | --- | --- |
| **L1 活动上下文** | 今天聊到哪儿了 | `data/memory/<角色>/context/`（段 + 摘要 + 状态） | `context.py` |
| **L1' 滑窗日志** | 原始对话（会被清） | `sessions/session-*.jsonl` | 管线写、`prune_raw_sessions` 清 |
| **L2 情节** | 「记得那件事」 | `episodes.jsonl` | `extract.py` / `levels.py` |
| **L3 事实** | 名字/偏好/习惯（双时间） | `facts.json`（`invalid_at` 保留旧值） | `levels.py` / `dream.py` |
| **L4 知识** | 世界观/资料 | `data/knowledge/**` + 人格卡 `world` | `knowledge.py` |

```
context.py   L1：不可变段 + 追加尾（前缀缓存能命中的前提）、render() 三段布局、缓存记账
dream.py     「做梦」：离线 agent 交提议 → 五道校验 → 执行 → 封段 + 摘要 + 前镜像（可 undo）
extract.py   从对话里抽情节/事实（规则 + 可选本地模型摘要）
levels.py    自清洁：合并 / 衰减 / 提升（L2→L3）/ 淘汰
retrieve.py  双路检索：内容 + 中文时间（「上周三」）
model.py     Episode / Fact（含双时间字段）
store.py     原子写 + 跨进程锁（json / jsonl）
sync.py      镜像到**私有** git 仓库（export / pull / status），公开仓库拒推
knowledge.py L4：共享 / 世界观组 / 角色专属 三层叠加
schedule.py  共享日程接口（记忆只读，写入只走技能）
__init__.py  Memory（一个角色一份）+ MemoryHub（按角色分发）
```

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
```

做梦：

```python
from voice_loop.memory.dream import Dreamer, undo
Dreamer(settings, hub, "baize").run(source="rule")   # 规则版；agent 版由 llm_factory 决定
undo(settings, hub, "baize", "2026-11-20")           # 整批回滚那次梦
```

## 配置面

```toml
[memory]              # 开关 / 注入字数 / 归档与自清洁 / 上限 / 知识库路径 / 云端同步
[memory.context]      # L1：段大小、摘要天数、尾预算（本地模型单独一档更小）
[memory.dream]        # 做梦：触发条件（窗口/到量/空闲/上限）、model、可撤销记录保留
```

## 四条不变量

1. **段封存后一个字节都不改** —— 这是"白天的提示词前缀稳定、云端缓存能命中"的唯一实现方式。
2. **压缩 ≠ 删除**：封存的段还在盘上，`recall` 照样查得到。
3. **做梦是"整理"不是"重写"**：五道校验（pinned 保护 / 证据必须是用户说的 / NOOP /
   条数上限 / 前镜像）+ `--undo` 整批回滚。
4. **镜像到云端是"本地→云端镜像、云端→本地合并"**，且**公开仓库一律拒推**。

## 单独优化它

```powershell
python scripts\tests\test_memory.py            # 分级 / 自清洁 / 检索 / 隔离
python scripts\tests\test_memory_dream.py      # 日上下文 / 做梦 / undo / 五种触发
python scripts\tests\test_memory_mcp.py         # MCP 工具 + 知识库分层
python scripts\tests\test_memory_sync.py        # 上云（临时 bare 仓库，不联网）
python main.py memory --recall "导师" --when 上周
python main.py dream --dry-run                  # 只算不改
python main.py memsync --status                 # 云端有没有没推的
```

## 已知限制

- **L2/L3 的价值判断**：本地角色只走规则版（4B 做"该不该记"会灌噪声）；
  agent 版需要 `[memory.dream] model = "api:…"`。
- `sync.py` 40 KB：导出/合并/闸门都在一起，要再拆就按「导出」「合并」「git 操作」切。
- `__init__.py` 38 KB：`Memory` 与 `MemoryHub` 合在一个文件里，是历史原因（改起来风险高）。
