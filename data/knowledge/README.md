# 知识库（L4）：把「世界观 / 资料 / 自己的笔记」放进这个目录

放在这里的 `.md` / `.txt` / `.json` 会被切成片段，供助手在回答时检索
（`data/memory/` 里存的是**它自己的经历和事实**，这里放的是**你给它的背景知识**）。

## 怎么组织

- **一个主题一个文件**，用 Markdown 标题分节：标题是检索时最强的信号，
  留空一行分隔段落；一节 900 字以内最合适（切段规则见 `voice_loop/memory/knowledge.py`）。
- 文件里随便写：设定、人名表、术语表、项目背景、常见问答都行。
- 想临时试一段，也可以直接写在 `config.toml` 的 `[memory] world = "…"`，不用建文件。

## 举例

```markdown
# 世界设定

白泽是通晓万物之名的瑞兽，如今住在本地机器里……

# 人物

- 阁下：机器的主人。
```

## 检查有没有被读到

```powershell
python main.py memory --knowledge 白泽      # 只搜知识库
python main.py memory                        # 看 knowledge 那行的段数
```

想看某次回答到底用了哪几段，跑助手时看日志里的 `[记忆]` 行，或
`python main.py memory --recall "关键词"`（知识库片段会标 `[chunk]`）。
