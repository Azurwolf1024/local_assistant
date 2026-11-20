# 工程日志（已拆分）

> ★这个文件不再是正文★：3900 行的单文件按主题拆成了 **7 册**，节号保持原样。

- 索引与节号对照表：[`docs/log/README.md`](log/README.md)
- 快速定位某节：`Select-String -Path docs\log\*.md -Pattern '^## 36\.'`

| 分册 | 讲什么 |
| --- | --- |
| [log/01-tts-quality.md](log/01-tts-quality.md) | 声音：音质、沙沙声、音区守卫、克隆与训练、TTS 解耦（§6~§61） |
| [log/02-asr-wake.md](log/02-asr-wake.md) | 识别与唤醒（§53、§63） |
| [log/03-memory.md](log/03-memory.md) | 记忆：四级 → 日上下文 → 做梦、知识库、上云 |
| [log/04-console-ui.md](log/04-console-ui.md) | 控制台与 UI |
| [log/05-persona-llm.md](log/05-persona-llm.md) | 人格与人设 |
| [log/06-data-events.md](log/06-data-events.md) | 事件统一与事件链 |
| [log/07-ops-release.md](log/07-ops-release.md) | 运维、清理、隐私事故、发布 |

原文件的历史版本在 git 里：`git log --follow -p docs/ENGINEERING_LOG.md`
