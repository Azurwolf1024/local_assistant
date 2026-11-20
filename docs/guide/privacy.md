# 隐私说明与协议

> 从 [`README.md`](../../README.md) 拆出：第 10 章、第 11 章。
> 索引见 [`docs/README.md`](../README.md)；章节号保持原样，
> 老地方写的「见第 14 节」仍然指得到（对照表在 README 里）。

---
## 10. 隐私说明

麦克风音频、识别文本、模型推理全部在项目目录本地完成（提醒、课表、备忘
都只是本机 json 文件）。唯一的外部依赖是 `http://127.0.0.1:11434`（本机 Ollama）。
断网可正常使用。

这些文件是**你自己的数据**，都在 `.gitignore` 里（不会被推到仓库）：

| 位置 | 是什么 | 生命周期 |
| --- | --- | --- |
| `sessions/session-*.jsonl` | 每次对话的原始记录（L1） | 归档进记忆库后，**超过 30 天自动清**（`[memory] keep_session_days`） |
| `data/memory/<角色>/` | 记忆（L2 情节 / L3 事实） | 只在本地；删掉就等于失忆（想备份就 `python main.py memsync` 推到你的私有仓库，见第 15 节） |
| `data/vision/` | 截屏 / 摄像头拍到的画面 | 想清就整个删掉，不影响运行 |
| `data/knowledge/` | 你给它的世界观/资料 | 随你放 |

想立刻清原始对话：`python main.py memory --prune --apply`（只删**已归档**过的，没进记忆库的不动）。

---


## 11. 协议

[MIT](../../LICENSE) © 2026 Azurwolf1024

用到的模型各自遵守自己的协议：Whisper 与 SenseVoiceSmall 是 MIT，
Piper 与它自带的中文声线是 MIT，Silero VAD 是 MIT，Qwen3.5 是 Apache-2.0。

---
