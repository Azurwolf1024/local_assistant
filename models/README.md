# models/

这个目录里全是模型权重，**不进 git**（最大的 `whisper-large-v3-turbo-int8-ov` 一个就 930 MB）。
克隆下来是空的，按需要执行下面任意一条把它填满：

```powershell
python scripts/download_models.py                    # 全部：SenseVoiceSmall + Silero VAD + Piper 中文女声
python scripts/download_models.py --only asr         # 只要 ASR
python scripts/download_models.py --only tts         # 只要 TTS
```

目录约定（改 `config.toml` 里的路径就要跟着改）：

| 路径 | 用途 | 约 |
| --- | --- | --- |
| `asr/sensevoice-small/` | 常驻的快速 ASR（`model.int8.onnx` + `tokens.txt`） | 250 MB |
| `asr/whisper-large-v3-turbo-int8-ov/` | 按需加载的精确 ASR（OpenVINO int8 导出） | 930 MB |
| `vad/silero_vad.onnx` | 断句 | 2 MB |
| `tts/piper/zh_CN-huayan-medium.onnx` | 中文女声 | 63 MB |

Whisper 的 OpenVINO 版需要用 `optimum-cli` 自己导出，命令见根目录 `README.md` 的「安装」一节。
