# `scripts/` 怎么找东西

三个层次，按「你会不会照着文档里抄这条命令」分：

| 位置 | 放什么 | 例子 |
| --- | --- | --- |
| `scripts/`（根） | **文档里会让人照抄的命令**、被代码调用/解析的脚本 | `download_models.py` `eval_asr.py` `preflight_heavy.py` `lint_persona.py` `check_deploy.py` `make_release.py` `make_console_exe.py` `import_persona.py` `import_lines.py` `persona_voice.py` `say.py` `clean_junk_data.py` |
| `scripts/tests/` | **离线自测**（`python scripts/tests/xxx.py`，退出码 0 = 通过）+ 跑全套的 `run_all.cmd` | `test_offline.py` `test_asr_registry.py` `test_memory_dream.py` |
| `scripts/tools/` | **一次性 / 实验性**：训练、探针、A/B、数据准备、迁移 | `train_piper.py` `probe_npu.py` `ab_voice.py` `migrate_tts_format.py` |

为什么根目录不全搬家：`setup_flow.py` **真的会执行** `scripts/check_deploy.py`、
还会**解析** `scripts/download_models.py` 取模型组名；文档里几十处写着
`python scripts/download_models.py`。把这些留在根目录，改结构就不会改坏别人的肌肉记忆。

## 常用

```powershell
python scripts\download_models.py                    # 下模型（--only kws / zipvoice / speaker…）
python scripts\eval_asr.py --synth                    # 识别评测：合成素材
python scripts\eval_asr.py --matrix                   # 识别评测：各后端对比
python scripts\lint_persona.py                        # 人格文件体检（发布前必须 0 警告）
python scripts\check_deploy.py                        # 只读体检（setup/doctor 复用它）
python scripts\preflight_heavy.py                     # 跑全套自测前的内存/显存预检
python scripts\tests\test_asr_registry.py             # 单个自测
```

## 跑全套自测

```powershell
scripts\tests\run_all.cmd      # 49 个脚本 + lint_persona，日志落 sessions\testlogs\
```

## 找某个自测

按子系统猜名字就行：`test_asr_*`（识别）、`test_tts_*` / `test_voice_*`（声音）、
`test_memory_*`（记忆）、`test_console*` / `test_update` / `test_setup_flow`（交付）、
`test_wake*` / `test_bargein` / `test_precision`（唤醒与打断）、`test_module_boundaries`（模块边界）。
