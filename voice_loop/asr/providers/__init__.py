"""识别后端集合。

★加一个后端 = 在这里加一个文件★：模块顶层只写一条 :data:`PROVIDER` 声明，
引擎和三方库都在 ``create()`` 里才 import —— 所以注册表「扫一遍」不会把
sherpa-onnx / openvino / torch 拉进内存（唤醒服务待唤醒时要省内存）。

见 `voice_loop/asr/registry.py` 的 ``discover()``。
"""
