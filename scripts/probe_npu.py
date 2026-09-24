"""NPU（Intel(R) AI Boost）到底能不能用上：分阶段探测，每段跑在自己的子进程里。

资源监视器里能看到 NPU，不代表这个项目用得上它。这个脚本把话说清楚：

    段名              试什么                                    耗时
    props             只查设备属性（名字/驱动/架构）            秒级
    vad               Silero VAD（小、静态）→ CPU 对比 NPU      ~7s
    whisper_enc       Whisper encoder（动态形状 [?,128,3000]）  ~10s
    whisper_static    同一份 encoder 固定成 [1,128,3000] 再试   ★NPU 编译要 ~5 分钟★
    whisper_gpu       同一份固定形状：CPU vs 核显（对照 NPU）

★每段都是独立子进程★：NPU 编译失败是**硬崩**（不是抛异常），在同一进程里试会把探测脚本
自己带走，退出码就什么都看不到了。崩溃会如实报成 `0xC0000005`。

2026-09-24 本机实测（Core Ultra 5 225H + Arc 130T + AI Boost，OpenVINO 2026.4）：

| 模型 | CPU | Arc 核显 | NPU |
|---|---|---|---|
| Silero VAD（512 采样点，热态） | **0.4 ms** | — | 3.8 ms（慢 ~9 倍） |
| Whisper encoder（静态 30s） | 3.4 s | **0.168 s** | 1.33 s（慢 7.9 倍） |
| 同上，编译耗时 | 2.1 s | 6.2 s | **294 s** |
| Whisper encoder（动态形状） | √ | √ | ✗ 编译失败（Level0 `ZE_RESULT_ERROR_INVALID_ARGUMENT`） |

结论：**硬件是真的、也能跑，但这个项目用不上**——它擅长的那个大模型（Whisper encoder）
已经被 Arc 核显占了，而且核显快它 8 倍；小模型（VAD）它比 CPU 还慢 6 倍。
再加上 sherpa-onnx（ASR/TTS）与 Ollama 都没有 Intel NPU 后端，
真正能碰 NPU 的只有 whisper 那条 OpenVINO 路径。详见 docs/ENGINEERING_LOG.md 第 16.5 节。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VAD = ROOT / "models" / "vad" / "silero_vad.onnx"
WHISPER_ENC = (
    ROOT / "models" / "asr" / "whisper-large-v3-turbo-int8-ov" / "openvino_encoder_model.xml"
)

# Windows 的访问违例退出码（0xC0000005），子进程硬崩时就是这个
CRASH_CODES = {
    3221225477: "0xC0000005 访问违例",
    3221225725: "0xC00000FD 栈溢出",
    3221225477 | 0: "0xC0000005 访问违例",
}


def _static_feeds(model):
    """给每个输入造一个全 0 数组（动态维先当 1）。"""
    import numpy as np

    feeds = {}
    for inp in model.inputs:
        ps = inp.partial_shape
        # ★`ps.rank` 返回的是 Dimension 而不是 int★（拿它进 range() 会报
        # "Dimension object cannot be interpreted as an integer"）→ 用 len(ps)
        dims = [ps[i].get_length() if ps[i].is_static else 1 for i in range(len(ps))]
        feeds[inp.get_any_name()] = np.zeros(dims, dtype=np.float32)
    return feeds


def _props() -> int:
    import openvino as ov

    core = ov.Core()
    print(f"OpenVINO {ov.__version__}")
    print(f"可用设备：{list(core.available_devices)}")
    for name in ("FULL_DEVICE_NAME", "DEVICE_ID", "DEVICE_ARCHITECTURE", "NPU_DRIVER_VERSION",
                 "DEVICE_TYPE", "DEVICE_CAPABILITIES"):
        try:
            print(f"  NPU.{name} = {core.get_property('NPU', name)}")
        except Exception as exc:  # noqa: BLE001 - 版本间属性名不一样，查不到就算了
            print(f"  NPU.{name} → 不支持（{type(exc).__name__}）")
    return 0


def _bench(model_path: Path, devices: tuple[str, ...], reshape: list[int] | None = None,
           repeats: int = 1) -> int:
    import time as _t

    import openvino as ov

    core = ov.Core()
    model = core.read_model(str(model_path))
    print(f"模型：{model_path.name}（{model_path.stat().st_size / 1024:.0f} KB）")
    print(f"输入：{[(i.get_any_name(), str(i.partial_shape)) for i in model.inputs]}")
    if reshape:
        model.reshape({0: reshape})
        print(f"reshape 后：{[(i.get_any_name(), str(i.partial_shape)) for i in model.inputs]}")
    feeds = _static_feeds(model)
    values: dict[str, float] = {}
    for device in devices:
        try:
            t0 = _t.perf_counter()
            compiled = core.compile_model(model, device)
            compile_s = _t.perf_counter() - t0
            compiled(feeds)  # 先热一次（避开首次分配）
            t1 = _t.perf_counter()
            for _ in range(repeats):
                res = compiled(feeds)
            run_ms = (_t.perf_counter() - t1) * 1000 / repeats
            first = list(res.values())[0]
            values[device] = float(abs(first).mean()) if hasattr(first, "mean") else 0.0
            print(f"  {device}: 编译 {compile_s:.2f}s，推理 {run_ms:.1f}ms，平均幅值 {values[device]:.6f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {device}: 失败 —— {type(exc).__name__}: {str(exc)[:300]}")
    if len(values) == 2:
        a, b = values.values()
        print(f"  两者平均幅值差 {abs(a - b):.6f}（接近 0 = 数值一致，说明是同一套算法）")
    return 0


def _vad() -> int:
    return _bench(VAD, ("CPU", "NPU"))


def _whisper_enc() -> int:
    code = _bench(WHISPER_ENC, ("CPU", "NPU"))
    print("  ★这一份是动态形状 [?,128,3000]★：NPU 通常要求静态 shape，"
          "所以「日常那种长度不固定」的用法在 NPU 上根本过不了编译。")
    return code


def _whisper_static() -> int:
    print("  （NPU 编译这一步实测要 5 分钟左右，别以为它挂了）")
    code = _bench(WHISPER_ENC, ("CPU", "GPU", "NPU"), reshape=[1, 128, 3000])
    print("  注意：就算 encoder 能编译，后面还有 decoder / decoder_with_past（带 KV cache），"
          "它们也要各自过一遍——NPU 的路上通常死在那里。")
    return code


STAGES = {
    "props": _props,
    "vad": _vad,
    "whisper_enc": _whisper_enc,
    "whisper_static": _whisper_static,
}


def run_stage(name: str) -> int:
    """在子进程里跑某一段，返回退出码（硬崩也能如实报出来）。"""
    print(f"\n{'=' * 70}\n[{name}] 子进程启动\n{'=' * 70}")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--stage", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
        )
    except subprocess.TimeoutExpired:
        print(f"[{name}] 超时（30 分钟）—— 当成不可用")
        return 1
    cost = time.perf_counter() - t0
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print("stderr:", proc.stderr.strip()[-600:])
    code = proc.returncode
    if code in CRASH_CODES:
        print(f"[{name}] ✗ 进程硬崩：{CRASH_CODES[code]}（{cost:.1f}s）→ 不能用")
    elif code != 0:
        print(f"[{name}] ✗ 退出码 {code}（{cost:.1f}s）")
    else:
        print(f"[{name}] √ 正常结束（{cost:.1f}s）")
    return code


def main() -> int:
    if "--stage" in sys.argv:
        return STAGES[sys.argv[sys.argv.index("--stage") + 1]]()
    names = [a for a in sys.argv[1:] if not a.startswith("-")] or list(STAGES)
    bad = 0
    for name in names:
        if name not in STAGES:
            print(f"没有这一段：{name}（可选 {list(STAGES)}）")
            bad += 1
        elif run_stage(name) != 0:
            bad += 1
    print(f"\n{'=' * 70}")
    print("结论怎么看：")
    print("  · props 只证明「驱动在、OpenVINO 认得它」，不代表能用。")
    print("  · vad 成功但比 CPU 慢 = NPU 对小模型不划算（固定开销吃掉了收益）。")
    print("  · whisper_enc 失败 = 动态形状过不了 NPU 编译，日常用法直接用不了。")
    print("  · whisper_static 成功但比核显慢很多 = 就算为它改造导出也不值得。")
    print("  ★更硬的一条：sherpa-onnx（ASR/TTS）用自带的 onnxruntime（只有 CPU/Azure EP，"
          "换不了 EP），Ollama 没有 Intel NPU 后端 → 真能碰 NPU 的只有 whisper 那条路。★")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
