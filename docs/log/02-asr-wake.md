# 识别与唤醒（ASR / 唤醒词）

> 从 [`docs/ENGINEERING_LOG.md`](../ENGINEERING_LOG.md) 拆出：§53、§63。
> 索引见 [`docs/log/README.md`](README.md)；按主题成册，**节号保持原样**，
> 所以老地方写的「见 §36」仍然指得到。

---
## 53. 唤醒词不再靠手攒别名：拼音近音层（2026-10-01）

用户要求：唤醒词的模糊匹配要「给个唤醒词就能用，不用手工加近似词」，
并提到天猫精灵那类助手「忽略辅音比韵母」。

### 53.1 病根：字符级相似度天生够不到阈值

唤醒词一直用**字符级** `SequenceMatcher`（阈值 0.75）匹配。ASR 把「能天使」听成「能天史」时：

    2/3 = 0.667 < 0.75 → 认不出来

于是只能手工加别名 —— 能天使的资料卡里 5 条，凯尔希那边 **58 条**（都是用户耳听攒的）。
换个角色就得重新攒一遍。

### 53.2 业界怎么做（查过的）

| 路线 | 做法 | 代表 |
| --- | --- | --- |
| 声学侧（真 KWS） | 专用小模型**从音频**直接检关键词；关键词以「声母 + 韵母」token 序列给出（`n ǐ h ǎo j ūn g ē @你好军哥`），每个关键词单独调 `:boosting` 与 `#threshold` | sherpa-onnx open-vocabulary KWS（encoder int8 4.4 MB；chunk-8 延迟 160 ms / chunk-16 320 ms）、Porcupine、openWakeWord |
| 文本侧 | ASR 出字后归一到拼音/音素再比；同音词替换同属这条 | sherpa-onnx 的 pinyin homophone replacer；商用助手对自定义唤醒词普遍如此 |

用户的「忽略辅音比韵母」是文本侧的**粗糙版** —— 真做法是**音素级**匹配，不是真丢辅音
（丢了就会把「开尔文」也认成「凯尔希」，实测这条撞过）。

### 53.3 我们的做法：不另写一套，而是把已有的实测核心抽出来共用

`voice_loop/names.py` 里早就有一个人名用的拼音音近匹配器（凯尔希 58 条 + 阿米娅 7 条
真人耳听数据调出来的，配 22 条负样本 0 误判）。所以这一步**不是新写一个音近匹配器**，
而是把它抽成 `voice_loop/phonetic.py`，人名与唤醒词共用，只是**宽严不同**：

- 人名：只允许**第一个**音节不同（实测听错都错在首字）；
- 唤醒词：允许**恰好一个**音节不同（听错会落在任何位置 —— 「能天**史**」）。

新增/固化的规则（都是实测换来的，README 第 14 节有表）：

1. 相似度三档：全同 1.0 / 同声母 0.7 / 同韵母 0.7 / 否则 0；
2. 「同韵母」要求韵母 ≥2 字母 —— 否则「阿米**巴**」会被认成「阿米**娅**」；
3. ★同类混淆组★：声母 z/zh、c/ch、s/sh、n/l，韵母 an/ang、en/eng、in/ing…
   —— 这条是**量出来才补上**的：第一版只有「同声母/同韵母」，
   结果 17 条真实听错里漏了「白**哲**」（bai-zhe vs bai-ze，平翘舌），补上才 17/17；
4. **只在句首附近**认（允许前面一个语气词）—— 与人名同一套实测结论，
   放开到全句立刻出事（「海尔洗衣机」的「海尔洗」与「凯尔希」拼音全同）；
5. 拼音撞车的常用词黑名单（海尔洗、开尔文）；
6. 单音节唤醒词不做近音（一个音节里「恰好一个不同」等于全都不同）。

开关：`data/wakewords.json` 里的 `"phonetic": true`（**默认开**），关掉就回到只要别名的老行为。
匹配顺序变成：精确/别名 → **拼音近音** → 字符模糊（老的 0.75 那层保留）。
`best_ratio()` 也把拼音分数算进去 —— 不然会出现「相似度 ≥ 阈值却没命中」的自相矛盾提示。

### 53.4 测了什么

`scripts/tests/test_phonetic.py`（7 节，纯离线）：

- ★**把别名表拿掉**，17 条真实听错全中★（能天史/能天师/宁天使/能田使、阿宁、
  开尔信/胎儿戏/泰尔西/开尔西/凯儿戏/海尔西/太儿西、百泽/白则/白择/白哲/柏泽）
  —— 这正是用户的验收标准；
- 9 条日常负样本 **0 误触发**（海尔洗衣机 / 开尔文是谁 / 米拉…/胎儿心率 / 宁泽涛 / 阿米巴…）；
- 位置：句首认、句中句末不认（把字符层关掉做确定性验证）；
- 开关：关掉后「能天史」不再命中、相似度提示也不虚报；单音节词不做近音；
- ★只有一份实现★：`names.syllable_similarity is phonetic.similarity`、黑名单与声母表都是同一对象。

回归：`test_names` / `test_offline` 全 0；`test_wake_apply` 一开头报的是 **UnicodeEncodeError**
（`↔` 在 GBK 控制台编不出来）—— 那是**我手动跑脚本时没设 `PYTHONIOENCODING`** 的假失败，
总测脚本里是设了的（memory 里早记着这条，这次又踩了一遍）。

### 53.5 没做的 / 可选的下一步

- **没上真 KWS 模型**。它确实更省（3~4 MB 替掉 228 MB 的 SenseVoice）且更低延迟（160 ms），
  但要多一个模型 + 关键词文件要在运行时生成，而且只对设定过的词生效；
  我们待唤醒时本来就在跑 ASR，所以文本侧这一层是零成本的。
- 没做**声调**比对（现在丢掉声调）。听错主要是声母/韵母层面的，声调加上去收益小、噪声大。
- 没做「一句话里找唤醒词」（仍限句首附近）—— 有实测支撑的保守选择，不是忘了。
















---


## 63. ASR 解耦：把「怎么听」从引擎名字里拆出来（2026-11-20）

### 63.1 为什么光"换引擎"没用

这次的目标不是"支持更多识别模型"，而是三件具体的事：**提升准确率**、**提升唤醒速度**、
**让词库真的能起作用**。先看清楚它们各自卡在哪：

| 目标 | 原来卡在哪 | 现在靠什么 |
| --- | --- | --- |
| 准确率 | 一点词库都没有：人名/术语全靠 ASR 自由解码，再靠 `names.py` **事后猜**；两个引擎只能整句二选一 | `Lexicon` 一等公民 + 按后端能力自动选"喂法"（native / prompt / post） |
| 唤醒速度 | 唤醒也必须**跑完整识别**：VAD 等静音 → SenseVoice → 再拿文本匹配关键词 | 可选的两阶段唤醒（流式 KWS 先筛，命中才跑完整识别） |
| 词库匹配 | 只有拼音近似纠错，引擎拿不到任何提示 | 同上：transducer 有 `hotwords_file`，SenseVoice 有 `hr_lexicon`，都没有就事后纠 |
| 架构自由 | `pipeline` 直接调 `set_whisper_enabled / whisper_loaded / prefer="sensevoice"` —— 四个 API 全带引擎名 | 只剩 `Hearing`：`set_heavy_enabled / heavy_loaded / prefer="fast"`，**按能力**不按名字 |

### 63.2 旧结构的四个病灶

| 病灶 | 具体长相 | 位置 |
| --- | --- | --- |
| 按名字分支 | `_engine(name)` 里 `if name == "sensevoice" / "whisper"`；`_wanted_engines()` 又是一份 if | `asr/router.py` |
| 策略和引擎名焊死 | `strategy ∈ {sensevoice, whisper, hybrid}` 本身就是引擎名；hybrid 硬编码"先 SV 再 WH" | 同上 |
| `prefer=` 传引擎名 | TTS 回听校验写死 `prefer="sensevoice"` | `pipeline._wire_text_guard` |
| 重引擎写死 Whisper | `whisper_enabled / load_whisper / whisper_loaded` 四件套 + `VoiceLoop(lazy_whisper=)` | `router` / `pipeline` |

顺带发现一个**静默 bug**：`tts/zipvoice_tts.py` 直连 `SenseVoiceEngine` 并读
`getattr(asr, "sensevoice_threads", 4)` —— 这个字段**根本不存在**，所以线程数永远是 4。
现在它走 `Hearing.transcribe(prefer="fast")`，TTS 也不再反向依赖 ASR 的内部实现。

### 63.3 新结构：spec / registry / providers / session

```python
AsrSpec(provider, language, model, verify, verify_when, fallback, params, note)
ProviderSpec(id, label, capabilities, hotwords, fields, defaults, ready, create, missing)
Hearing: transcribe(prefer=) / set_heavy_enabled / load_heavy / warmup / close
```

| 能力 | 意思 | 谁有 |
| --- | --- | --- |
| `local` | 在本机算 | 三个内置后端 |
| `cloud` | 会把音频发上网（本版本没有实现者，闸门先留着） | — |
| `heavy` | 走两级加载（待唤醒绝不常驻） | whisper |
| `fast` | 能当"合成回听"这类内部校验的快路径 | sensevoice |
| `hotwords` | 热词怎么喂：`native / prompt / post / none` | transducer / (暂缺) / sensevoice、whisper |

★策略也是数据★：`verify` + `verify_when`（`empty` / `long:6.0` / `score<0.45` / `always`）。
老 `strategy` 在 `registry.LEGACY_STRATEGY` 里映射成同一份 spec，所以
`config.toml` 和 `main.py --strategy` 都不用改。

### 63.4 词库：一份数据，四种用法

`asr/lexicon.py` 把「名字 / 唤醒词 / 领域词」合成一张表，然后**按后端能力选喂法**：

- `native` → 生成 `data/lexicon/_gen/hotwords.txt`（`词 :权重`）交给 transducer
- `prompt` → 生成一句 `initial_prompt`（Whisper 这条路当前导出不支持，见坑 3）
- `post` → 句首名字走 `names.NameCorrector`（严规则），领域词走"同长度 + 只差一个音节 +
  相似度 ≥ fix_min_score"
- 唤醒匹配 → 同一份词（名字 + aliases）

★保守是刻意的★：把人名纠正放开到全句会立刻出事（实测「海尔洗衣机」→「凯尔希」、
「米亚的论文」被改坏）。自测里专门钉了三条负样本，`fix` 一条都不许动。

### 63.5 五个坑（都是真跑才暴露的）

1. **Whisper 在短音上会整句幻觉**：把「阿米娅」听成 `MING PAO CANADA MING PAO TORONTO`，
   而且质量分（0.5）比"空结果"（0.0）**高**，于是被混合策略选中 —— CER 直接 9.0。
   修法：主引擎"没听出字"（只有一个「。」也算没听出）而复核结果"不像话"（中文场景下
   一个汉字都没有）时，**按没听清处理**，宁可空着也不编。
   实测同一批素材 CER 1.499 → 0.499、RTF 0.669 → 0.052。
2. **`prefer="fast"` 不能只在"想要的那两个"里找**：一开始只在主/复核引擎里挑，
   结果配上"慢主引擎 + 快兜底"时永远选不到快的。改成从**降级链**里挑第一个 `fast` 且真能起来的。
3. **Whisper 的提示词偏置在这台机器上不存在**：transformers 5.5 的 `generate()` 没有
   `prompt/prompt_ids`，传 `prompt=` 只会被当未知参数丢掉（还刷一条警告）。所以
   whisper 的 `hotwords` 标成 `post`，字段留着但注明"当前不生效"。
4. **`dataclasses.replace()` 在测试壳子上会炸**：`registry.effective()` 原来直接
   `replace(settings, ...)`，自测用 `SimpleNamespace` 当配置就 TypeError。改成
   `_with()`：真是 dataclass 就 replace，否则 `copy.copy` + 赋值。
5. **`test_wake_cycle` 的等待条件写错了**（老问题，这次才暴露）：它等"日志里出现
   `[待唤醒] 已释放`"，但前面阶段已经打过同一句，于是可能抢在应答打印之前抓快照。
   改成等"这次"的计数**超过**之前的值。

### 63.6 两级加载的契约没变（只是换了名字）

`test_wake_cycle.py` 钉的四条语义一字未改：关闭**不阻塞**（< 0.3 s）、
加载中关不掉要返回 `False`（交给加载线程收尾）、加载线程结束后**自己卸掉**、
待唤醒时转写**只跑常驻引擎**。为了兼容老代码，`set_whisper_enabled / load_whisper /
whisper_loaded` 都还在，内部转发到通用实现。

### 63.7 验证

| 验的东西 | 怎么验 | 结果 |
| --- | --- | --- |
| 注册表 / 能力 / 别名 | `scripts/tests/test_asr_registry.py` [1] | 3 个后端，`sherpa` 原生热词，无 cloud |
| `[asr]` → spec（含老 strategy） | 同上 [2] | hybrid 三条条件在；`whisper_min_duration` 跟着走 |
| 配置副本不改全局 | 同上 [3] | 全局一个字段都没动 |
| 降级链 | 同上 [4] | 自己 → fallback → 快引擎，无重复 |
| 策略（空/长/低分/不复核/prefer） | 同上 [5] | 全过；★复核只在条件命中时才构造引擎★ |
| 两级加载 + 竞态 | 同上 [6] + `test_wake_cycle.py` [1] | 全过 |
| 词库四种用法 + 误伤为 0 | 同上 [7] | 「白则」→「白泽」；三条负样本原样不动 |
| KWS 默认关 / 缺模型退回 | 同上 [8] | `ready=False` 且原因说清；检测失败当没命中 |
| 评测指标 | 同上 [9] | CER / 归一（「３点」→「3点」）/ 唤醒 / 误唤醒 |
| 真机转写 | `python main.py asr models/asr/sensevoice-small/zh.wav` | 5.59 s 音频 0.09 s 出结果（RTF 0.02），走了新门面 |
| 真机评测 | `python scripts/eval_asr.py --synth --set wake` | 8 条唤醒词：CER 0.499 / 命中 25% / 误唤醒 0% / RTF 0.052 |
| 回归 | `test_wake_cycle` / `test_offline` / `test_persona` / `test_tts_registry` / `test_agent_tools` / `test_route` / `test_mcp` / `test_accel` / `test_wake` | 全 EXIT=0 |

### 63.8 这次**没有**做的事（说清楚，免得以为是 bug）

- **没有云端 ASR**：`CAP_CLOUD` 只是闸门占位（`[asr] allow_cloud = false`）。
  本机使用场景里"把声音传出去"需要单独讨论，不该顺手做掉。
- **KWS 默认关**：它改变的是唤路路径，必须先量过命中率/误唤醒。模型 30 MB，
  `download_models.py --only kws` 一条命令就有。
- **流式识别（边说边出字）**：`CAP_PARTIAL` 占位，暂不实现。
- **唤醒命中率还只有 25%**（合成素材）：这是**下一步的工作清单**，不是这次的目标 ——
  评测台就是为了让「加 aliases / 调 fix_min_score / 换 transducer」这三个选择有数可依。
