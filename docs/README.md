# 文档（已按主题拆分）

从 2026-11-20 起，两个大文件按主题拆开：**用户手册**进 `guide/`，**工程日志**进 `log/`。
章节号 / 节号都保持原样，所以代码注释与老文档里写的
「见 README 第 14 节」「见工程日志第 36 节」仍然指得到。

## 用户手册（怎么装、怎么用）

| 文件 | 讲什么 |
| --- | --- |
| [guide/install.md](guide/install.md) | 安装（一键 / 手动）、自检、升级与备份、运行环境、GPU 加速、实测性能 |
| [guide/usage.md](guide/usage.md) | 日常使用、两个可配置的 json、调音（断句/停顿/语速）、可视化控制台 |
| [guide/persona.md](guide/persona.md) | 角色设定：多角色、声线、唤醒词、素材导入、只用 API 的角色 |
| [guide/memory.md](guide/memory.md) | 记忆：四级分层、会自己整理、怎么查旧事、上云 |
| [guide/asr.md](guide/asr.md) | 语音识别：怎么听、词库、评测台 |
| [guide/mail.md](guide/mail.md) | 邮件：看/读/摘要/起草，发送要人工审批（Outlook Graph） |
| [guide/mcp.md](guide/mcp.md) | MCP：自己搭的工具协议层 |
| [guide/faq.md](guide/faq.md) | 常见问题 |
| [guide/privacy.md](guide/privacy.md) | 隐私说明与协议 |

## 工程日志（为什么这么设计、实测、踩坑）

入口：[log/README.md](log/README.md)（分册索引 + 节号对照表）

| 分册 | 讲什么 |
| --- | --- |
| [log/01-tts-quality.md](log/01-tts-quality.md) | 声音：音质、沙沙声、音区守卫、克隆与训练、TTS 解耦 |
| [log/02-asr-wake.md](log/02-asr-wake.md) | 识别与唤醒 |
| [log/03-memory.md](log/03-memory.md) | 记忆：四级 → 日上下文 → 做梦、知识库、上云 |
| [log/04-console-ui.md](log/04-console-ui.md) | 控制台与 UI |
| [log/05-persona-llm.md](log/05-persona-llm.md) | 人格与人设 |
| [log/06-data-events.md](log/06-data-events.md) | 事件统一与事件链 |
| [log/07-ops-release.md](log/07-ops-release.md) | 运维、清理、隐私事故、发布 |

## 子系统文档（可单独拿走、单独优化）

| 子系统 | 目录 | 文档 |
| --- | --- | --- |
| 语音识别 | `voice_loop/asr/` | [asr/README.md](../voice_loop/asr/README.md) |
| 语音合成 | `voice_loop/tts/` | [tts/README.md](../voice_loop/tts/README.md) |
| 记忆 | `voice_loop/memory/` | [memory/README.md](../voice_loop/memory/README.md) |
| 邮件 | `voice_loop/mail/` | [mail/README.md](../voice_loop/mail/README.md) |
| MCP 工具层 + harness | `voice_loop/mcp/`、`voice_loop/harness.py` | [mcp/README.md](../voice_loop/mcp/README.md) |

这几个包的边界由自测盯着（AST 扫 import，docstring 提到不算违规）：

```powershell
python scripts\tests\test_module_boundaries.py
```

## 其它

- `docs/ENGINEERING_LOG.md`：现在是**指针**，内容已拆进 `log/`（保留它是为了老链接不 404）。
- 词库格式与用法：`data/lexicon/README.md`。
- 脚本怎么找：[scripts/README.md](../scripts/README.md)（三层分工：根 = 文档里照抄的命令 /
  `scripts/tests/` 离线自测 / `scripts/tools/` 探针·训练·迁移）。
