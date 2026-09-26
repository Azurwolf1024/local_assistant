# 知识库（L4）：把「世界观 / 资料 / 自己的笔记」放进这个目录

放在这里的 `.md` / `.txt` / `.json` 会被切成片段，供助手在回答时检索
（`data/memory/` 里存的是**它自己的经历和事实**，这里放的是**你给它的背景知识**）。

## 两种摆法（决定谁能看到）

| 放哪里 | 谁能检索到 | 用途 |
| --- | --- | --- |
| `data/knowledge/某个文件.md`（**顶层**） | **所有角色** | 通用常识、你的项目背景 |
| `data/knowledge/<角色id>/任何文件.md` | **只有那个角色** | 各自 IP 的世界观 |

三个角色来自三个世界观，就建三个同名目录：

```
data/knowledge/
    通用说明.md            ← 大家都看
    baize/世界观.md        ← 只有「白泽」看
    kaltsit/罗德岛.md      ← 只有「凯尔希」看
    amiya/泰拉.md          ← 只有「阿米娅」看
```

★子目录名必须等于角色 id★（就是 `data/characters.json` 索引里那个 `id`）。
共享库只收**顶层文件**，所以专属世界观不会跑到别人嘴里。

## 一个主题一个文件

- **一个主题一个文件**，用 Markdown 标题分节：标题是检索时最强的信号，
  留空一行分隔段落；一节 900 字以内最合适（切段规则见 `voice_loop/memory/knowledge.py`）。
- 文件里随便写：设定、人名表、术语表、项目背景、常见问答都行。
- 想临时试一段，也可以直接写在 `config.toml` 的 `[memory] world = "…"`，不用建文件。

## 人格文件里的几个字段（可选，见 persona.py 模块说明）

```json
{
  "id": "amiya", "name": "阿米娅",
  "knowledge": ["data/knowledge/amiya/泰拉.md"],   // 显式路径（可以指到仓库外）
  "knowledge_title": "泰拉大陆",                    // 检索/统计里显示的标题
  "world": "阿米娅是罗德岛的领袖……",                 // 直接把世界观写在这
  "knowledge_shared": false                        // ★连顶层那份也不看★（全隔离）
}
```

`knowledge_shared` 默认 `true`（共享 + 自己的都看）；设 `false` 就是「只看自己那份」。

## 检查有没有被读到

```powershell
python main.py memory --who baize --knowledge 白泽   # 只搜「白泽」这名下的知识库
python main.py memory --who baize                    # 看 knowledge 那行：local / local:baize 各多少段
```

想看某次回答到底用了哪几段，跑助手时看日志里的 `[记忆]` 行，或
`python main.py memory --recall "关键词"`（知识库片段会标 `[资料]`）。

## 模型自己也会查

记忆挂了 MCP 工具（`recall` / `remember`，`config.toml` 的 `[[mcp.servers]]` 里配置）：
用户提到过去的事时，模型会先自己查一遍再回答，而不是靠提示词硬塞。
