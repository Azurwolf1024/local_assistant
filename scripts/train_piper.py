"""Piper/VITS 微调启动器（绕开厂商入口与新版 Lightning 的不兼容）。

为什么需要它：``python -m piper_train`` 用的是 ``Trainer.add_argparse_args``
（Lightning 1.x API，2.x 已删除），而这个环境里只有 lightning 2.6。
另外它的 ``--resume_from_checkpoint`` 走的是 Lightning 的「恢复整段训练」：
会把**别人训练时的超参和 dataloader** 一起恢复回来（路径在我们机器上不存在），
对「拿别人的 ckpt 微调」是错的做法。

这个启动器改成显式做两件事：
1. 用**我们自己的**数据集配置造 ``VitsModel``（采样率/符号表都从 preprocess 的 config.json 读）；
2. **只把权重**从底模 ckpt 里搬过来（``load_state_dict(strict=False)``），
   优化器状态不要（微调本来就要新的学习率）——
   ★并且会断言「生成器的编码器权重真的匹配上了」★，避免「以为在微调、其实从零训」。

用法：

    python scripts/train_piper.py --dataset-dir data/piper/kaltsit/training ^
        --checkpoint models/tts/piper/_train/zh_CN-huayan-medium.ckpt ^
        --out data/piper/kaltsit/exp --epochs 200 --batch-size 8 --lr 2e-4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
PIPER_SRC = ROOT / ".piper-src" / "src" / "python"
if str(PIPER_SRC) not in sys.path:
    sys.path.insert(0, str(PIPER_SRC))


def load_weights(model, ckpt_path: Path) -> tuple[int, int]:
    """把底模 ckpt 里的**权重**搬进 model（不恢复优化器/超参）。"""
    # ★这个 ckpt 是在 Linux 上存的，pickle 里带着 PosixPath★：直接 load 会报
    #   "cannot instantiate 'PosixPath' on your system"。把 PosixPath 顶成 WindowsPath
    #   就能解开（里面那些路径是别人的超参，我们不用；真要用的只有 state_dict）。
    import pathlib

    if pathlib.PosixPath is not pathlib.WindowsPath:  # pragma: no branch
        pathlib.PosixPath = pathlib.WindowsPath  # type: ignore[assignment,misc]
    blob = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    state = blob.get("state_dict") if isinstance(blob, dict) else None
    if not isinstance(state, dict):
        raise SystemExit(f"{ckpt_path.name} 里没有 state_dict（不是 Lightning 的 ckpt？）")
    # lightning 的 key 可能带 'model_g.' 前缀，也可能不带；两种都试
    stripped = {k[len("model."):] if k.startswith("model.") else k: v for k, v in state.items()}
    missing, unexpected = model.load_state_dict(stripped, strict=False)
    gen_keys = [k for k in stripped if k.startswith("model_g.")]
    print(f"底模权重：共 {len(stripped)} 个张量，其中生成器 {len(gen_keys)} 个；"
          f"未匹配（missing）{len(missing)} 个，多余（unexpected）{len(unexpected)} 个")
    if missing[:3]:
        print("  missing 例：", missing[:3])
    if unexpected[:3]:
        print("  unexpected 例：", unexpected[:3])
    # ★断言：编码器/解码器的核心权重必须在位★
    emb = model.model_g.enc_p.emb.weight.detach()
    if float(emb.abs().max()) == 0.0:
        raise SystemExit("生成器 embedding 还是全 0 = 权重没搬进来，别开了")
    if missing and len(missing) > len(stripped) * 0.2:
        raise SystemExit(f"太多权重没匹配上（{len(missing)}/{len(stripped)}），结构可能不一致")
    return len(missing), len(unexpected)


def main() -> int:
    ap = argparse.ArgumentParser(description="Piper 微调（显式版，绕开厂商入口）")
    ap.add_argument("--dataset-dir", required=True, help="preprocess 的输出目录（含 config.json/dataset.jsonl）")
    ap.add_argument("--checkpoint", default="", help="底模 ckpt（留空 = 从零训，别这么干）")
    ap.add_argument("--out", required=True, help="训练产物目录（default_root_dir）")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--checkpoint-epochs", type=int, default=25, help="每多少 epoch 存一次")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=2e-4, help="学习率（微调可以用 1e-4 更保守）")
    ap.add_argument("--num-workers", type=int, default=0, help="Windows 上用 0（spawn 有额外开销）")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--threads", type=int, default=0, help="torch 线程数（0 = 不设）")
    args = ap.parse_args()

    import json

    try:  # 这个环境里装的是 pytorch_lightning（没有 lightning 元包）
        from lightning.pytorch import Trainer
        from lightning.pytorch.callbacks import ModelCheckpoint
    except ImportError:  # pragma: no cover
        from pytorch_lightning import Trainer
        from pytorch_lightning.callbacks import ModelCheckpoint

    from piper_train.vits.lightning import VitsModel

    dataset_dir = Path(args.dataset_dir)
    config_path = dataset_dir / "config.json"
    dataset_path = dataset_dir / "dataset.jsonl"
    if not config_path.is_file() or not dataset_path.is_file():
        raise SystemExit(f"{dataset_dir} 里缺 config.json / dataset.jsonl（先跑 preprocess）")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ★这些超参跟厂商 train.sh 一致；quality 默认 medium（底模就是 medium）★
    model = VitsModel(
        num_symbols=int(config["num_symbols"]),
        num_speakers=int(config["num_speakers"]),
        sample_rate=int(config["audio"]["sample_rate"]),
        dataset=[str(dataset_path)],
        learning_rate=args.lr,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        validation_split=0.0,
        num_test_examples=0,
        seed=args.seed,
    )
    if args.checkpoint:
        ckpt = Path(args.checkpoint)
        if not ckpt.is_file():
            raise SystemExit(f"底模 ckpt 不存在：{ckpt}")
        print(f"从 {ckpt.name}（{ckpt.stat().st_size / 1e6:.0f} MB）搬权重…")
        t0 = time.perf_counter()
        load_weights(model, ckpt)
        print(f"  用时 {time.perf_counter() - t0:.1f}s")
    else:
        print("⚠️ 没有给 --checkpoint：从零训。5 分钟数据训不出可用声线，只适合验证流程")

    n_train = len(model._train_dataset) if model._train_dataset is not None else -1
    steps_per_epoch = max(1, (n_train + args.batch_size - 1) // args.batch_size)
    print(f"训练样本 {n_train} 条 → 每 epoch {steps_per_epoch} 步；共 {args.epochs} epoch "
          f"≈ {steps_per_epoch * args.epochs} 步")

    trainer = Trainer(
        accelerator="cpu",
        devices=1,
        precision=32,
        max_epochs=args.epochs,
        default_root_dir=str(out),
        callbacks=[ModelCheckpoint(every_n_epochs=max(1, args.checkpoint_epochs), save_top_k=-1)],
        num_sanity_val_steps=0,
        log_every_n_steps=1,
        enable_progress_bar=False,
    )
    t0 = time.perf_counter()
    trainer.fit(model)
    dt = time.perf_counter() - t0
    print(f"\n训练结束：{dt / 60:.1f} 分钟（{dt / max(1, args.epochs):.1f} 秒/epoch）")
    print(f"权重在 {out}（找 *.ckpt）。下一步导出 ONNX：")
    print(f"  python -m piper_train.export_onnx <ckpt> {out}/voice.onnx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
