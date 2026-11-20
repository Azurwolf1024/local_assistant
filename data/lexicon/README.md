# 识别词库（词库放在这里）

识别准确率最便宜的那部分提升来自**告诉引擎这台机器上会出现哪些词**：
角色名（白泽 / 凯尔希 / 克莱门莎）、唤醒词、领域术语。词库就放在这个目录。

## 格式（一行一个词）

```
# 注释行：井号开头整行忽略
源石                     # 只有词 → 用 [asr.lexicon] weight（默认 1.5）
罗德岛 2.5               # 词 + 权重（越大越"应该"被识别出来）
泰拉|泰拉大陆,泰拉世界      # 竖线后面是"听错写法/同义写法"，用于唤醒匹配与规范名还原
```

## 怎么被用上（不用你操心，按后端能力自动选）

| 后端能力 | 词库怎么喂进去 | 谁是这样 |
| --- | --- | --- |
| `native` | 生成 `hotwords.txt` 交给引擎（真正的热词加权） | `sherpa` 里的 **transducer / zipformer** |
| `prompt` | 塞进解码提示词 | （当前 whisper 导出不支持，见下） |
| `post` | 事后用拼音近似纠正 | `sensevoice`、`whisper` |

★注意★：`sensevoice` 只能事后纠正，所以「白泽」被听成「白色」这种**同音不同调**的错，
它不一定改得回来（改得太激进会误伤「海尔洗衣机」）。想从源头解决，走
`[asr] provider = "sherpa"` + 一个 zipformer transducer 模型（原生热词）。

`sensevoice` 还有一条引擎侧的路：`[asr.providers.sensevoice] hr_lexicon` / `hr_dict_dir`
（sherpa-onnx 的同音字替换），配了就不用事后纠。

## 自动化

- 人格文件里的 `name` / `wake_words` / `aliases` 会**自动并入**（`from_personas = true`）
- 生成物在 `data/lexicon/_gen/`（`hotwords.txt` / `keywords.txt`），可随时删掉重建
- `*.txt` 在这个目录里被 `.gitignore` 挡着（里面可能有你的个人信息）；
  想公开某一本词典就 `git add -f data/lexicon/xxx.txt`

## 改完怎么量

```powershell
python scripts\eval_asr.py --synth                  # 合成一批素材（唤醒词/人名/领域词）
python scripts\eval_asr.py --set names --set wake   # 看 CER 和唤醒命中率
python scripts\eval_asr.py --matrix                  # 各后端一张对比表
```
