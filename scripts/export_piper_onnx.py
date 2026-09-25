"""把 Piper 微调的 checkpoint 导成能直接用的 ONNX（并写配套的 .onnx.json）。

## 为什么不用 `python -m piper_train.export_onnx`

vendor 那个脚本调 `torch.onnx.export(..., dynamic_axes=...)`，而本机的 torch 已经
**只剩 dynamo 导出器**（旧 TorchScript 导出没了），dynamo 又不接受 `dynamic_axes`
（会抛 `Constraints violated`）。这是**环境与 vendor 代码的版本差**，不是我们的模型问题。

本项目处理 vendor 的老办法（见 `scripts/train_piper.py`）是：**不改 vendor 源码**，
自己写一个等价的小脚本，把兼容性阶梯写在明面上——以后换 torch 版本，坏在哪里一眼可见。

## 导出的兼容性阶梯

    ① 先试 `dynamo=False`（旧导出器，最省事，参数语义和 vendor 一样）
    ② 不行就 `dynamo=True` + `dynamic_shapes`（新导出器的正确写法）
    → 两条都失败才报错，并把两边的原始错误都打出来（不然只剩一句「导出失败」没法查）

## 用法

    .venv-piper\\Scripts\\python.exe scripts\\export_piper_onnx.py \\
        --checkpoint data/piper/kaltsit/exp/lightning_logs/version_2/checkpoints/epoch=59-step=480.ckpt \\
        --out models/tts/piper/kaltsit-finetune.onnx

会生成 `kaltsit-finetune.onnx` 和 `kaltsit-finetune.onnx.json`（后者从基础语音的配置
复制而来，只改必须改的地方——音素表跟微调前完全一样，因为我们是**在它基础上**微调的）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OPSET = 15                    # 与 vendor 的 export_onnx.py 保持一致（piper 运行时认这个）
# 与 vendor 一致：noise, length, noise_w（推理时会被 piper 用请求里的值覆盖）
DEFAULT_SCALES = (0.667, 1.0, 0.8)
DUMMY_PHONEMES = 50


def build_model(checkpoint: Path):
    """加载 VitsModel 并包成「只有 infer」的导出形态（照 vendor 的做法）。"""
    import torch  # noqa: PLC0415

    from piper_train.vits.lightning import VitsModel  # noqa: PLC0415

    model = VitsModel.load_from_checkpoint(str(checkpoint), dataset=None)
    model_g = model.model_g
    model_g.eval()
    with torch.no_grad():
        model_g.dec.remove_weight_norm()          # 推理不需要权重归一化

    def infer_forward(text, text_lengths, scales, sid=None):
        noise_scale, length_scale, noise_scale_w = scales[0], scales[1], scales[2]
        return model_g.infer(
            text, text_lengths,
            noise_scale=noise_scale,
            length_scale=length_scale,
            noise_scale_w=noise_scale_w,
            sid=sid,
        )[0].unsqueeze(1)

    model_g.forward = infer_forward
    return torch, model_g


def export(torch, model_g, out: Path) -> tuple[bool, str]:
    """导出 ONNX。返回 (成功?, 用了哪条路/错误说明)。"""
    num_symbols = model_g.n_vocab
    num_speakers = model_g.n_speakers
    sequences = torch.randint(0, num_symbols, (1, DUMMY_PHONEMES), dtype=torch.long)
    lengths = torch.LongTensor([sequences.size(1)])
    sid = torch.LongTensor([0]) if num_speakers > 1 else None
    scales = torch.FloatTensor(list(DEFAULT_SCALES))
    args = (sequences, lengths, scales, sid)
    names = dict(input_names=["input", "input_lengths", "scales", "sid"], output_names=["output"])
    legacy_axes = {
        "input": {0: "batch_size", 1: "phonemes"},
        "input_lengths": {0: "batch_size"},
        "output": {0: "batch_size", 1: "time"},
    }
    errors: list[str] = []

    # ① 旧导出器（参数语义与 vendor 一致）
    try:
        with torch.no_grad():
            torch.onnx.export(model_g, args, str(out), opset_version=OPSET,
                              dynamo=False, dynamic_axes=legacy_axes, **names)
        return True, "dynamo=False（旧导出器 + dynamic_axes）"
    except Exception as exc:  # noqa: BLE001 - 换下一条路
        errors.append(f"① dynamo=False: {type(exc).__name__}: {exc}")

    # ② 新导出器：dynamic_shapes 才是它的正确写法（dynamic_axes 会被拒）
    try:
        from torch.export import Dim  # noqa: PLC0415

        batch = Dim("batch_size", min=1, max=8)
        phonemes = Dim("phonemes", min=1, max=2000)
        time_dim = Dim("time", min=1, max=200000)
        dynamic_shapes = (
            {0: batch, 1: phonemes},      # input
            {0: batch},                   # input_lengths
            None,                         # scales（固定 3 个标量）
            {0: batch} if num_speakers > 1 else None,
        )
        with torch.no_grad():
            torch.onnx.export(
                model_g, args, str(out), opset_version=OPSET, dynamo=True,
                dynamic_shapes=dynamic_shapes,
                input_names=names["input_names"], output_names=names["output_names"],
            )
        # output 的动态维由导出器按图推出来；这里只断言文件真的出来了
        _ = time_dim
        return True, "dynamo=True（新导出器 + dynamic_shapes）"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"② dynamo=True: {type(exc).__name__}: {exc}")

    return False, "\n".join(errors)


def write_config(base_json: Path, out_json: Path, sample_rate: int, checkpoint: Path) -> dict:
    """写配套配置：**从基础语音复制**，只改必须改的。

    音素表（phoneme_id_map）保持原样——我们是在这个基础语音上微调的，符号集没变；
    乱改它会让模型「听见」不认识的音素，直接变成乱码音。
    """
    cfg = json.loads(base_json.read_text(encoding="utf-8"))
    cfg.setdefault("audio", {})["sample_rate"] = int(sample_rate)
    cfg["dataset"] = f"fine-tuned from {base_json.stem} on kaltsit lines ({checkpoint.name})"
    cfg["piper_version"] = cfg.get("piper_version", "1.0.0")
    out_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


def verify_with_onnxruntime(onnx_path: Path) -> str:
    """用 onnxruntime 真加载一遍（导出的文件「看起来在」不等于能用）。"""
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except ImportError:
        return "（这个环境没有 onnxruntime，跳过加载自检）"
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    ins = [f"{i.name}:{i.shape}" for i in sess.get_inputs()]
    outs = [f"{o.name}:{o.shape}" for o in sess.get_outputs()]
    return f"onnxruntime 加载成功 · 输入 {ins} · 输出 {outs}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Piper 微调权重 → ONNX（+ 配套 json）")
    ap.add_argument("--checkpoint", required=True, help="训练出的 *.ckpt")
    ap.add_argument("--out", required=True, help="输出 *.onnx（同目录会再生成 *.onnx.json）")
    ap.add_argument("--base-config", default="models/tts/piper/zh_CN-huayan-medium.onnx.json",
                    help="复制哪份配置（音素表来自它，默认 huayan medium）")
    ap.add_argument("--sample-rate", type=int, default=22050,
                    help="采样率，默认 22050（训练数据就是这个，别乱改）")
    ap.add_argument("--no-verify", action="store_true", help="跳过 onnxruntime 加载自检")
    args = ap.parse_args(argv)

    checkpoint = Path(args.checkpoint)
    if not checkpoint.is_absolute():
        checkpoint = (ROOT / checkpoint).resolve()
    if not checkpoint.exists():
        print(f"找不到 checkpoint：{checkpoint}")
        return 2
    out = Path(args.out)
    if not out.is_absolute():
        out = (ROOT / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    base_json = Path(args.base_config)
    if not base_json.is_absolute():
        base_json = (ROOT / base_json).resolve()

    print(f"checkpoint : {checkpoint.name}（{checkpoint.stat().st_size / 1024 / 1024:.0f} MB）")
    print(f"输出       : {out}")
    t0 = time.time()
    torch, model_g = build_model(checkpoint)
    print(f"模型已加载：n_vocab={model_g.n_vocab} n_speakers={model_g.n_speakers}"
          f"（{time.time() - t0:.1f}s）")

    started = time.time()
    ok, how = export(torch, model_g, out)
    if not ok:
        print("导出失败：")
        print(how)
        return 1
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"导出成功（{how}）· {size_mb:.1f} MB · 耗时 {time.time() - started:.1f}s")

    cfg = write_config(base_json, out.with_suffix(out.suffix + ".json"), args.sample_rate, checkpoint)
    print(f"配置已写：{out.with_suffix(out.suffix + '.json').name}"
          f"（音素表沿用 {base_json.stem}，采样率 {cfg['audio']['sample_rate']}）")

    if not args.no_verify:
        print(verify_with_onnxruntime(out))

    print("\n下一步（让角色用上它）：")
    print(f"  1) 把 {out.name} 与 {out.name}.json 放到 models/tts/piper/（已经在的话跳过）")
    print(f"  2) 在 data/personas/<角色>.json 里写 \"voice\": \"{out.stem}\"")
    print("  3) 把 config.toml 的 [tts] backend 改成 \"piper\"（或在角色里单独指）")
    print("  4) 试听：python main.py tts \"你好呀\" -o sessions/x.wav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
