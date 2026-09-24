"""ZipVoice 模型的**精度选择**：int8 还是 fp32。

为什么单独一个模块：这套判断有四个地方要用——加载引擎的 ``zipvoice_tts``、
切角色前查文件全不全的 ``pipeline``、盘点素材的 ``voice_data``、
离线测试。放在 ``zipvoice_tts`` 里会逼着前两个模块为了一个字符串常量去 import
numpy 和 sherpa 那一堆东西；放这儿则谁都能用，而且**只有一份定义**——
「查文件全不全」和「真正加载哪个文件」用的是同一个函数，不会各说各话。

两份可以共存：训练脚本 ``scripts/finetune_zipvoice.py --precision both``
会把 int8 和 fp32 都装进同一个角色目录，运行时按 config.toml 的
``[tts] clone_precision`` 选一套，方便直接 A/B 听。

    python scripts/ab_clone_model.py      # 精度 × 步数 的客观指标 + wav 试听
"""

from __future__ import annotations

from pathlib import Path

# 精度 →（角色模型目录里的两个 onnx 文件名）
PRECISION_FILES: dict[str, tuple[str, str]] = {
    "int8": ("encoder.int8.onnx", "decoder.int8.onnx"),
    "fp32": ("encoder.onnx", "decoder.onnx"),
}

# 默认精度：int8 是**实测过、当前在用**的那一套（125 MB / 快），
# fp32 是 2026-09 为了查「沙沙声」加的候选（600 MB / 慢一倍），还没被耳朵确认。
DEFAULT_PRECISION = "int8"


def want_precision(settings) -> str:
    """配置里想用哪个精度（认不出来的值当 int8）。"""
    raw = str(getattr(getattr(settings, "tts", None), "clone_precision", "") or "").strip().lower()
    return raw if raw in PRECISION_FILES else DEFAULT_PRECISION


def model_names(directory: Path, precision: str) -> tuple[str, str]:
    """这个目录里实际能用的两个 onnx 文件名。

    优先级：**要的精度 → 目录里另一份 → 都没有就返回要的那份名字**
    （后一种情况让调用方按「缺文件」报错，报的也正是用户配置的那个精度）。

    为什么要退：精度是**音质取舍**而不是功能开关——把 ``clone_precision``
    写成 fp32 而那个角色只装了 int8，或者反过来，都应该继续出声
    （只是音质/速度不是最理想），绝不能哑。
    """
    order = [precision, *(p for p in PRECISION_FILES if p != precision)]
    for name in order:
        enc, dec = PRECISION_FILES[name]
        if (directory / enc).is_file() and (directory / dec).is_file():
            return enc, dec
    return PRECISION_FILES.get(precision, PRECISION_FILES[DEFAULT_PRECISION])


def has_any(directory: Path) -> bool:
    """这个目录里有没有任何一套能用的 onnx。"""
    return any(
        all((directory / name).is_file() for name in names)
        for names in PRECISION_FILES.values()
    )
