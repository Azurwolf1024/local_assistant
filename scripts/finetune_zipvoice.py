"""在 Windows 上跑 ZipVoice 官方微调（把 Linux 配方翻译成 Windows 能跑的版本）。

官方配方是 `egs/zipvoice/run_finetune.sh`（bash，Linux、多卡）。本机情况不同：

| 官方默认 | 本机 | 为什么 |
|---|---|---|
| `--world-size 4` + `--use-fp16 1` | `--world-size 1` + `--use-fp16 0` | 没有 CUDA，CPU 上 fp16 又慢又不稳 |
| `--num-iters 10000 --max-duration 500` | `--iters 300 --max-duration 60` | 只有 4.8 分钟数据，配官方参数是上万轮，纯过拟合 |
| `--num-workers 8` | `--num-workers 0` | Windows 上 spawn 子进程 + 小数据集，没必要 |
| `--drop-last 1` | `--drop-last 0` | 数据太少，丢掉最后一个 batch 就是丢掉一堆样本 |
| `--min-len 1.0` | `--min-len 0.5` | 我们有 0.87s 的短句，别被过滤掉 |

其余环境事实（都是实测踩出来的，见 README 同名小节）：
- 训练用独立 venv `.venv-zipvoice`（torch 2.11 + torchaudio 2.11 + k2(torch2.11) + lilcom）；
- **必须 `PYTHONUTF8=1`**：官方脚本读 TSV 没写 encoding，Windows 默认 GBK 会直接崩；
- 权重走 hf-mirror 下载到 `.zipvoice-src/download/zipvoice/`。

    python scripts/finetune_zipvoice.py                 # 只做环境检查
    python scripts/finetune_zipvoice.py --stage 6 --stop-stage 6 --iters 100   # 冒烟测速
    python scripts/finetune_zipvoice.py --stage 2 --stop-stage 8 --iters 300   # 全流程
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = ROOT / ".venv-zipvoice"
DEFAULT_REPO = ROOT / ".zipvoice-src"
DEFAULT_WEIGHTS = DEFAULT_REPO / "download" / "zipvoice"
# 预训练权重在 huggingface.co 上（本机连不上）→ 一律走镜像
HF_MIRROR = "https://hf-mirror.com"
HF_REPO = "k2-fsa/ZipVoice"
# 权重文件（model.pt 468 MB，必须多连接下，单连接实测只有 ~1.5 MB/分钟）
WEIGHT_FILES = ("model.pt", "tokens.txt", "model.json")
DOWNLOAD_CONNECTIONS = 8


def venv_python(venv: Path) -> Path:
    exe = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
    if os.name == "nt":
        exe = exe.with_suffix(".exe")
    return exe


def child_env(repo: Path, threads: int = 0) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    # ★关键★ 官方脚本读 TSV 没指定 encoding，不开 UTF-8 模式 Windows 上必崩
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # ★关键★ CPU 训练默认只跑一个核（实测 20 秒里只涨 19.4 CPU 秒 ≈ 单核 97%），
    # 这台机器有 14 线程，不告诉 torch 就是白等十几倍。
    if threads > 0:
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            env[key] = str(threads)
        env["ZIPVOICE_NUM_THREADS"] = str(threads)
    return env


def run(cmd: list[str], repo: Path, label: str, threads: int = 0) -> float:
    """跑一条命令，打印命令本身和耗时；失败就抛。"""
    print(f"\n$ {' '.join(cmd)}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(repo), env=child_env(repo, threads=threads))
    cost = time.perf_counter() - t0
    print(f"[{label}] 用时 {cost:.1f}s，退出码 {proc.returncode}")
    if proc.returncode != 0:
        raise SystemExit(f"{label} 失败（退出码 {proc.returncode}）")
    return cost


def which_python() -> Path:
    """跑 ZipVoice 的那些模块要用哪个解释器（venv 优先）。"""
    exe = venv_python(DEFAULT_VENV)
    return exe if exe.exists() else Path(sys.executable)


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / 1e6:.1f} MB"


def _head_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - 固定域名
        return int(resp.headers.get("Content-Length") or 0)


def _fetch_range(url: str, start: int, end: int, dest: Path) -> None:
    """下一段 Range 到临时文件（失败抛出，由调用方重试）。"""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=600) as resp, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out, length=1 << 20)


def download_file(url: str, dest: Path, connections: int = DOWNLOAD_CONNECTIONS) -> bool:
    """多连接分段下载 + 逐段校验 + 总大小校验。

    为什么不用单连接：实测 hf-mirror 单连接拉 468 MB 只有 ~1.5 MB/分钟（要 5 小时），
    8 连接能到 36 MB/分钟（约 13 分钟）。
    ★别用「起一堆后台任务再等」的写法★：上次 Wait-Job 提前返回，把没下完的分片拼进去了，
    产出 390 MB 的坏文件。这里每段都校验字节数，不对就不拼。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = _head_size(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  取不到文件大小（{exc}）：{url}")
        return False
    if size <= 0:
        print(f"  文件大小为 0：{url}")
        return False

    parts = dest.parent / "parts"
    parts.mkdir(exist_ok=True)
    chunk = -(-size // connections)
    expected: dict[int, int] = {}
    for index in range(connections):
        start = index * chunk
        end = min(start + chunk - 1, size - 1)
        expected[index] = end - start + 1

    todo = [i for i in range(connections)]
    for attempt in range(1, 4):
        if not todo:
            break
        print(f"  第 {attempt} 轮：{len(todo)} 段待下（共 {human_size(size)}）")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as pool:
            futures = {}
            for index in todo:
                start = index * chunk
                end = min(start + chunk - 1, size - 1)
                part = parts / f"{dest.name}.part{index}"
                futures[index] = pool.submit(_fetch_range, url, start, end, part)
            failed = []
            for index, future in futures.items():
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"    段 {index} 失败：{type(exc).__name__} {exc}")
                    failed.append(index)
        todo = []
        for index in range(connections):
            part = parts / f"{dest.name}.part{index}"
            got = part.stat().st_size if part.exists() else 0
            if got != expected[index]:
                todo.append(index)
        if not todo:
            break
    if todo:
        print(f"  ★还有 {len(todo)} 段没下完：{todo}（先不拼装）")
        return False

    with dest.open("wb") as out:
        for index in range(connections):
            part = parts / f"{dest.name}.part{index}"
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, length=1 << 20)
    final = dest.stat().st_size
    if final != size:
        print(f"  ★拼装后大小不对：{final} != {size}，分片先留着")
        return False
    for part in parts.glob(f"{dest.name}.part*"):
        part.unlink()
    print(f"  下好了：{dest.name}  {human_size(final)}")
    return True


def ensure_weights(weights: Path, variant: str) -> int:
    """权重不在就自动从镜像下载（幂等：大小对就跳过）。"""
    missing = [name for name in WEIGHT_FILES if not (weights / name).exists()]
    if not missing:
        return 0
    print(f"\n权重缺失：{'、'.join(missing)} → 从 {HF_MIRROR} 下载（{variant}）")
    base = f"{HF_MIRROR}/{HF_REPO}/resolve/main/{variant}"
    failed = []
    for name in missing:
        url = f"{base}/{name}"
        if name == "model.pt":
            ok = download_file(url, weights / name)
        else:
            weights.mkdir(parents=True, exist_ok=True)
            try:
                urllib.request.urlretrieve(url, weights / name)  # noqa: S310
                ok = True
            except Exception as exc:  # noqa: BLE001
                print(f"  下载 {name} 失败：{exc}")
                ok = False
        if not ok:
            failed.append(name)
    if failed:
        print(f"  ★没下全：{failed}；也可以手工从 {base}/ 拉下来放到 {weights}")
        return 1
    print("  权重就绪")
    return 0


def patch_repo() -> None:
    """给官方代码打两个必要的补丁（幂等，改前存 `.orig` 备份）。

    ★补丁 1：硬编码单线程★
    `zipvoice/bin/train_zipvoice.py` 的 `__main__` 里直接写了
    `torch.set_num_threads(1)` / `set_num_interop_threads(1)`（k2/icefall 为多卡 DDP 防
    线程过订的写法），于是 `OMP_NUM_THREADS` 完全无效：实测 14 核机器上训练只跑 1 核
    （20 秒里 CPU 时间只涨 19.4 秒），改完 9.32 核。

    ★补丁 2：onnx 导出器代际不匹配★
    `zipvoice/bin/onnx_export.py` 调 `torch.onnx.export(..., dynamic_axes=...)`，
    而 torch ≥2.9 默认走 dynamo 导出器，会报
    "Failed to convert 'dynamic_axes' to 'dynamic_shapes'"；加 `dynamo=False`
    走老的 TorchScript 路径即可（它本来就是 `torch.jit.trace` 后再导出的）。
    """
    # --- 补丁 1：多线程 ---
    for name in ("train_zipvoice.py", "train_zipvoice_distill.py"):
        target = DEFAULT_REPO / "zipvoice" / "bin" / name
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        if "ZIPVOICE_NUM_THREADS" in text:
            print(f"  {name} 已是多线程版，跳过")
            continue
        patched = text.replace(
            "torch.set_num_threads(1)",
            'torch.set_num_threads(int(os.environ.get("ZIPVOICE_NUM_THREADS", "1")))',
        ).replace(
            "torch.set_num_interop_threads(1)",
            'torch.set_num_interop_threads(int(os.environ.get("ZIPVOICE_NUM_THREADS", "1")))',
        )
        if patched == text:
            print(f"  {name} 里没找到单线程那两行（版本变了？），未改动")
            continue
        backup = target.with_suffix(".py.orig")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        target.write_text(patched, encoding="utf-8")
        print(f"  已给 {name} 打上多线程补丁（原文件存为 {backup.name}）")

    # --- 补丁 2：ONNX 导出走 TorchScript ---
    target = DEFAULT_REPO / "zipvoice" / "bin" / "onnx_export.py"
    if target.exists():
        text = target.read_text(encoding="utf-8")
        if "dynamo=False" in text:
            print("  onnx_export.py 已是 TorchScript 导出，跳过")
        elif "verbose=False,\n        opset_version=opset_version," in text:
            # ★只匹配 torch.onnx.export(...) 内部那两行★：包装函数的调用点也传
            # `opset_version=opset_version,`，只按那一行替换会把 dynamo 传给包装函数，
            # 报 "export_text_encoder() got an unexpected keyword argument 'dynamo'"（踩过）。
            patched = text.replace(
                "verbose=False,\n        opset_version=opset_version,",
                "verbose=False,\n        dynamo=False,\n        opset_version=opset_version,",
            )
            backup = target.with_suffix(".py.orig")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            target.write_text(patched, encoding="utf-8")
            print(f"  已给 onnx_export.py 打上 dynamo=False 补丁（原文件存为 {backup.name}）")
        else:
            print("  onnx_export.py 里没找到 torch.onnx.export 的 kwargs（版本变了？），未改动")


def stage_env(args) -> int:
    """0 步：环境清点。缺什么就明说，别等到训练中途才炸。"""
    print("== 环境清点 ==")
    ok = True
    py = which_python()
    print(f"  Python：{py}")

    report = {
        "训练仓库": (DEFAULT_REPO / "zipvoice" / "bin" / "train_zipvoice.py", True),
        "权重 model.pt": (args.weights / "model.pt", True),
        "权重 tokens.txt": (args.weights / "tokens.txt", True),
        "权重 model.json": (args.weights / "model.json", True),
        # ★TSV 是第 1 步自己生成的，缺了不算错★：否则「新角色一条龙」（--stage 1）
        # 会在预检就被拦住（踩过：阿米娅第一次跑 EXIT=1，什么都没干）
        "训练 TSV": (args.data_dir / "custom_train.tsv", False),
        "验证 TSV": (args.data_dir / "custom_dev.tsv", False),
        # manifest / fbank 是第 2~4 步自己会生成的，缺了不算错（否则第一次跑必失败）
        "train manifest": (args.fbank_dir / f"{args.prefix}_cuts_train.jsonl.gz", False),
        "dev manifest": (args.fbank_dir / f"{args.prefix}_cuts_dev.jsonl.gz", False),
    }
    for what, (path, mandatory) in report.items():
        exists = path.exists()
        if not exists and mandatory:
            ok = False
        size = human_size(path.stat().st_size) if exists else ("缺失（下面会生成）" if not mandatory else "缺失")
        print(f"  {'OK  ' if exists else ('缺  ' if mandatory else '待生')} {what:<16} {path}  ({size})")

    try:
        proc = subprocess.run(
            [str(py), "-c", "import torch, torchaudio, k2, lhotse, piper_phonemize, lilcom; print(torch.__version__, torchaudio.__version__)"],
            env=child_env(DEFAULT_REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            print(f"  依赖：torch/torchaudio = {proc.stdout.strip()}")
        else:
            ok = False
            print(f"  依赖导入失败：{(proc.stderr or '').strip()[-300:]}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  依赖导入异常：{exc}")

    print("\n结论：" + ("环境齐了，可以开跑。" if ok else "还有东西没准备好（见上面标『缺』的行）。"))
    return 0 if ok else 1


def stage_dataset(args) -> int:
    run(
        [
            sys.executable,
            str(ROOT / "scripts" / "prepare_tts_dataset.py"),
            "--dir",
            str(args.audio_dir),
            "--out",
            str(args.data_dir.relative_to(ROOT)).replace("\\", "/"),
            "--apply",
            "--force",
            "--path-prefix",
            str(ROOT).replace("\\", "/") + "/",
        ],
        ROOT,
        "数据集",
    )
    return 0


def stage_manifests(args) -> int:
    py = str(which_python())
    manifests = DEFAULT_REPO / "data" / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    for subset in ("train", "dev"):
        run(
            [
                py, "-m", "zipvoice.bin.prepare_dataset",
                "--tsv-path", (args.data_dir / f"custom_{subset}.tsv").as_posix(),
                "--prefix", args.prefix,
                "--subset", f"raw_{subset}",
                "--num-jobs", str(args.jobs),
                "--output-dir", "data/manifests",
            ],
            DEFAULT_REPO,
            f"prepare_dataset {subset}",
        )
    return 0


def stage_tokens(args) -> int:
    py = str(which_python())
    for subset in ("train", "dev"):
        # dev 只有几条，官方默认 --num-jobs 20 会因为「切不出 20 份」直接报错
        run(
            [
                py, "-m", "zipvoice.bin.prepare_tokens",
                "--input-file", f"data/manifests/{args.prefix}_cuts_raw_{subset}.jsonl.gz",
                "--output-file", f"data/manifests/{args.prefix}_cuts_{subset}.jsonl.gz",
                "--tokenizer", args.tokenizer,
                "--num-jobs", "1",
            ],
            DEFAULT_REPO,
            f"prepare_tokens {subset}",
        )
    return 0


def count_cuts(path: Path) -> int:
    """数一个 cuts jsonl.gz 里有几条（一行一条）。"""
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def ensure_dev_cuts(args, minimum: int = 10) -> None:
    """让验证集至少有 minimum 条。

    ★这是个硬壁垒★：`zipvoice/dataset/datamodule.py` 里验证集的
    `DynamicBucketingSampler(cuts_valid, max_duration=..., shuffle=False)` **没传 num_buckets**，
    于是用 lhotse 默认的 10 → 要求验证集 ≥ 10 条，否则直接 AssertionError
    （实测 3 条时报"The number of buckets (10) must be smaller than or equal to the number of cuts (3)"）。

    4.8 分钟的数据再切 10 条出来做验证太亏，所以这里的做法是：**把训练集的 fbank manifest
    复制一份当验证集**（特征文件是按内容哈希存的，改个名就能用，不用重算）。
    代价是 loss 曲线不再反映泛化性 —— 反正这么小的数据也看不出什么泛化，
    真要留真验证集就自己去改 prepare_tts_dataset.py 的 --dev-count，并接受训练数据变少。
    """
    dev = args.fbank_dir / f"{args.prefix}_cuts_dev.jsonl.gz"
    train = args.fbank_dir / f"{args.prefix}_cuts_train.jsonl.gz"
    marker = args.fbank_dir / f"{args.prefix}_dev_is_train_copy"
    have = count_cuts(dev)
    limit = max(minimum, int(args.dev_cuts or minimum))

    def take_from_train(count: int) -> int:
        with gzip.open(train, "rt", encoding="utf-8") as src, gzip.open(
            dev, "wt", encoding="utf-8"
        ) as out:
            for index, line in enumerate(src):
                if index >= count:
                    break
                out.write(line)
        marker.write_text(
            f"这份验证集是从 {train.name} 取的前 {count} 条（由 finetune_zipvoice.py 生成）\n",
            encoding="utf-8",
        )
        return count_cuts(dev)

    if marker.exists():
        # 是我们自己造的（=训练集前 N 条），就按 limit 对齐
        if have != limit:
            now = take_from_train(limit)
            print(f"  验证集是我们造的副本：从 {have} 条调整到 {now} 条（省验证时间）")
        else:
            print(f"  验证集 {have} 条（自造副本，符合设定），保持原样")
        return
    if have >= minimum:
        print(f"  验证集 {have} 条（≥{minimum}），保持原样")
        return
    if not train.exists():
        print(f"  验证集只有 {have} 条，但训练 manifest 也不在（{train}）")
        return
    now = take_from_train(limit)
    print(
        f"  ★验证集只有 {have} 条，而 lhotse 的验证 sampler 硬要求 ≥{minimum} 条★\n"
        f"    已从训练集取前 {now} 条做验证集（记了标记文件，下次可调）：训练数据一条不浪费，\n"
        f"    验证集只留 {limit} 条是为了省时间（实测验证每条都要跑一次前向）；\n"
        f"    代价是验证 loss 不再反映泛化性（这么小的数据看不出泛化，以试听为准）。"
    )


def stage_fbank(args) -> int:
    py = str(which_python())
    for subset in ("train", "dev"):
        run(
            [
                py, "-m", "zipvoice.bin.compute_fbank",
                "--source-dir", "data/manifests",
                "--dest-dir", "data/fbank",
                "--dataset", args.prefix,
                "--subset", subset,
                "--num-jobs", "2",
            ],
            DEFAULT_REPO,
            f"compute_fbank {subset}",
        )
    ensure_dev_cuts(args)
    return 0


def stage_train(args) -> int:
    py = str(which_python())
    cmd = [
        py, "-m", "zipvoice.bin.train_zipvoice",
        "--world-size", "1",
        "--use-fp16", "0",                     # CPU 上没有 CUDA，fp16 只会更慢
        "--finetune", "1",
        "--base-lr", str(args.lr),
        "--num-iters", str(args.iters),
        "--save-every-n", str(max(20, args.iters // 5)),
        "--average-period", str(max(20, args.iters // 3)),
        # ★checkpoint 每个 1.87 GB（含优化器状态）★，默认 keep-last-k=30 能吃掉 50+ GB
        "--keep-last-k", str(args.keep_last_k),
        "--max-duration", str(args.max_duration),
        "--max-len", str(args.max_len),
        "--min-len", str(args.min_len),
        "--num-workers", str(args.num_workers),
        "--num-buckets", str(args.num_buckets),
        "--drop-last", "0",
        "--model-config", (args.weights / "model.json").as_posix(),
        "--checkpoint", (args.weights / "model.pt").as_posix(),
        "--tokenizer", args.tokenizer,
        "--token-file", (args.weights / "tokens.txt").as_posix(),
        "--dataset", "custom",
        "--train-manifest", f"data/fbank/{args.prefix}_cuts_train.jsonl.gz",
        "--dev-manifest", f"data/fbank/{args.prefix}_cuts_dev.jsonl.gz",
        "--exp-dir", args.exp_dir,
    ]
    cost = run(cmd, DEFAULT_REPO, "训练", threads=args.threads)
    cleanup_checkpoints(DEFAULT_REPO / args.exp_dir, keep_epochs=args.keep_epochs)
    if args.iters > 0:
        per_iter = cost / args.iters
        print(
            f"\n★ 速度：{args.iters} iter 用了 {cost / 60:.1f} 分钟 → "
            f"{60 / per_iter:.1f} iter/分钟（{per_iter:.1f} 秒/iter）"
        )
        print(f"   外推：1000 iter ≈ {1000 * per_iter / 3600:.1f} 小时；"
              f"3000 iter ≈ {3000 * per_iter / 3600:.1f} 小时")
    return 0


def cleanup_checkpoints(exp_dir: Path, keep_epochs: int = 2) -> None:
    """删掉旧的 ``epoch-N.pt``。

    ★每个 1.87 GB（含优化器状态）★：epoch 文件不受 ``--keep-last-k`` 管，
    100 iter 能攒出 20 多个、吃掉 40 GB。真正要留的是 ``checkpoint-*.pt``
    （平均模型要用）与 best-*，epoch 文件留最近几个就够。
    """
    epochs = sorted(exp_dir.glob("epoch-*.pt"), key=lambda p: int(p.stem.split("-")[-1]))
    if len(epochs) <= keep_epochs:
        return
    freed = 0
    for path in epochs[:-keep_epochs]:
        freed += path.stat().st_size
        path.unlink(missing_ok=True)
    print(f"  清理了 {len(epochs) - keep_epochs} 个旧 epoch checkpoint（释放 {human_size(freed)}）")


def stage_average(args) -> int:
    py = str(which_python())
    ckpts = sorted(
        (DEFAULT_REPO / args.exp_dir).glob("checkpoint-*.pt"),
        key=lambda p: int(p.stem.split("-")[-1]),
    )
    if not ckpts:
        print(f"没找到 checkpoint（{DEFAULT_REPO / args.exp_dir}）")
        return 1
    last = int(ckpts[-1].stem.split("-")[-1])
    # `--avg K` 会把最后 K 个分步 checkpoint 平均起来；只有一个时只能用 1
    # （训练崩在中途、或 save-every-n 太大时就会出现这种情况）
    avg = 2 if len(ckpts) >= 2 else 1
    run(
        [
            py, "-m", "zipvoice.bin.generate_averaged_model",
            "--iter", str(last), "--avg", str(avg),
            "--model-name", "zipvoice", "--exp-dir", args.exp_dir,
        ],
        DEFAULT_REPO,
        f"平均 checkpoint（avg={avg}）",
    )
    return 0


def pick_checkpoint(args) -> str:
    """挑一个 checkpoint 去导出：优先平均值，其次最新的分步 checkpoint。

    `load_checkpoint` 能吃带优化器状态的训练 checkpoint，所以平均那步失败/没跑
    也不影响导出（实测训练崩在 epoch 10 时，直接拿 checkpoint-60.pt 照样能导）。
    """
    exp = DEFAULT_REPO / args.exp_dir
    if args.ckpt:
        return args.ckpt
    averaged = sorted(exp.glob("iter-*-avg-*.pt"))
    if averaged:
        return averaged[-1].name
    stepped = sorted(exp.glob("checkpoint-*.pt"), key=lambda p: int(p.stem.split("-")[-1]))
    if stepped:
        return stepped[-1].name
    for name in ("best-valid-loss.pt", "best-train-loss.pt"):
        if (exp / name).exists():
            return name
    return ""


def stage_export(args) -> int:
    """导出 ONNX（sherpa-onnx 用的就是这种导出结果）。"""
    py = str(which_python())
    ckpt = pick_checkpoint(args)
    if not ckpt:
        print(f"{DEFAULT_REPO / args.exp_dir} 里找不到可导出的 checkpoint")
        return 1
    print(f"用这个 checkpoint 导出：{ckpt}")
    run(
        [
            py, "-m", "zipvoice.bin.onnx_export",
            "--model-name", "zipvoice",
            "--model-dir", args.exp_dir,
            "--checkpoint-name", ckpt,
            "--onnx-model-dir", args.onnx_dir,
        ],
        DEFAULT_REPO,
        "导出 ONNX",
    )
    return 0


# 精度 →（导出目录里的文件名, 角色目录里的文件名）
# ★两边文件名不一样，别搞混★：
#   官方 `onnx_export` 产出 → `text_encoder.onnx` / `text_encoder_int8.onnx`
#                             `fm_decoder.onnx`  / `fm_decoder_int8.onnx`
#   sherpa-onnx 要的是       → `encoder.onnx` / `encoder.int8.onnx`
#                             `decoder.onnx` / `decoder.int8.onnx`
PRECISION_FILES = {
    "int8": (
        ("**/text_encoder_int8.onnx", "encoder.int8.onnx"),
        ("**/fm_decoder_int8.onnx", "decoder.int8.onnx"),
    ),
    "fp32": (
        ("**/text_encoder.onnx", "encoder.onnx"),
        ("**/fm_decoder.onnx", "decoder.onnx"),
    ),
}


def stage_install(args) -> int:
    """把导出的 ONNX 装成一个**完整的角色模型目录**。

    ``--precision`` 决定装哪一份（导出那一步**两份都会生成**，见
    `.zipvoice-src/zipvoice/bin/onnx_export.py` 末尾）：
      - ``int8``（默认）：125 MB，快；出厂模型就是这个精度。
      - ``fp32``：600 MB（4.8 倍），慢一些，但省掉了动态量化的精度损失。
      - ``both``：两份都装，运行时有得选（见 config.toml 的 ``clone_precision``）。

    另外还要 `tokens.txt`、`lexicon.txt`、`espeak-ng-data/`（文本前端；声码器是共用的），
    这几样从出厂目录（``--base-model``）拷，让角色目录**自包含**。

    ★不要再把出厂目录的 encoder/decoder 当模板拷进来★：那是**出厂音色**，
    万一微调的文件没装成功，运行时会静默用出厂声线发声（听上去「音色突然变了」）。

    换之前会比对 token 表：实测本项目微调前后 ``tokens.txt`` 是**逐字节相同**的
    （训练脚本用的是权重自带那份），所以 lexicon / espeak-ng-data 不用动；
    真遇到不一致会大声警告。
    """
    src = DEFAULT_REPO / args.onnx_dir
    base = Path(args.base_model)
    dst = (
        Path(args.install_dir)
        if args.install_dir
        else ROOT / "models" / "tts" / "zipvoice" / "personas" / args.prefix
    )

    wanted = ["int8", "fp32"] if args.precision == "both" else [args.precision]
    exported: dict[str, Path] = {}
    for precision in wanted:
        for pattern, target in PRECISION_FILES[precision]:
            found = sorted(src.glob(pattern))
            if found:
                exported[target] = found[0]
            else:
                print(f"⚠ {src} 里没找到 {pattern}（{precision}），这一份跳过")
    if not exported:
        print(f"{src} 里没找到导出的 onnx，先跑导出那一步（--stage 7）")
        return 1
    if not base.is_dir():
        print(f"出厂模型目录不在：{base}（用 --base-model 指定）")
        return 1

    dst.mkdir(parents=True, exist_ok=True)
    print(f"出厂模板：{base}")
    print(f"安装到  ：{dst}")
    print(f"精度    ：{'、'.join(wanted)} → {', '.join(sorted(exported))}")
    for name in ("tokens.txt", "lexicon.txt"):
        source = base / name
        if not source.exists():
            continue
        target = dst / name
        if target.exists() and name in exported:
            continue  # 马上要覆盖，先不拷
        shutil.copy2(source, target)
        print(f"  拷入 {name}（{human_size(target.stat().st_size)}）")
    espeak_src = base / "espeak-ng-data"
    if espeak_src.is_dir() and not (dst / "espeak-ng-data").is_dir():
        shutil.copytree(espeak_src, dst / "espeak-ng-data")
        print(f"  拷入 espeak-ng-data/（{len(list((dst / 'espeak-ng-data').iterdir()))} 项）")

    for target_name in sorted(exported):
        path = exported[target_name]
        out = dst / target_name
        if out.exists() and not args.force:
            backup = out.with_suffix(out.suffix + ".bak")
            shutil.copy2(out, backup)
            print(f"  备份旧模型 → {backup.name}")
        shutil.copy2(path, out)
        print(f"  √ 装入 {target_name}（{human_size(out.stat().st_size)}，来自 {path.name}）")

    new_tokens = DEFAULT_REPO / args.exp_dir / "tokens.txt"
    old_tokens = base / "tokens.txt"
    if new_tokens.exists() and old_tokens.exists():
        same = hashlib.sha256(new_tokens.read_bytes()).digest() == hashlib.sha256(
            old_tokens.read_bytes()
        ).digest()
        if same:
            print("  token 表与出厂模型一致 → lexicon.txt / espeak-ng-data 不用动")
        else:
            shutil.copy2(new_tokens, dst / "tokens.txt")
            print(
                "  ★token 表与出厂模型不一致★：已换成新的 tokens.txt，"
                "但 lexicon.txt（按 token 字符串映射）可能也要重建，"
                "先跑 --verify 听一句再说。"
            )

    for name in ("encoder.onnx", "decoder.onnx", "encoder.int8.onnx", "decoder.int8.onnx"):
        f = dst / name
        if f.exists():
            print(f"  目录里现在有 {name}（{human_size(f.stat().st_size)}）")

    print("\n校验（用助手自己的解释器加载这份模型试一句）：")
    print(f"  python scripts/persona_voice.py --persona {args.prefix} --verify")
    print("  想换精度就改 config.toml 里的 tts.clone_precision（int8 / fp32）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows 上跑 ZipVoice 微调")
    ap.add_argument("--stage", type=int, default=0, help="从第几步开始（0 = 只检查环境）")
    ap.add_argument("--stop-stage", type=int, default=0, help="跑到第几步为止")
    ap.add_argument("--prefix", default="", help="数据集前缀（manifest 文件名 + 默认安装目录名）；留空则用音频目录名")
    ap.add_argument("--audio-dir", type=Path, default=ROOT / "data" / "personas" / "kaltsit")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data" / "finetune" / "kaltsit")
    ap.add_argument("--fbank-dir", type=Path, default=DEFAULT_REPO / "data" / "fbank")
    ap.add_argument("--weights", type=Path, default=None, help="预训练权重目录（默认按 --model-variant 推）")
    ap.add_argument(
        "--model-variant",
        default="zipvoice",
        help="用哪个预训练模型：zipvoice（基础）/ zipvoice_distill（蒸馏，推理步数更少、CPU 上更快）",
    )
    ap.add_argument("--exp-dir", default="exp/kalsit")
    ap.add_argument("--onnx-dir", default="exp/kalsit_onnx")
    ap.add_argument(
        "--install-dir",
        default="",
        help="装到哪（默认 models/tts/zipvoice/personas/<prefix>）",
    )
    ap.add_argument(
        "--base-model",
        default=str(ROOT / "models" / "tts" / "zipvoice" / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"),
        help="出厂模型目录（只从它拷 tokens/lexicon/espeak-ng-data 当模板）",
    )
    ap.add_argument(
        "--precision",
        choices=["int8", "fp32", "both"],
        default="int8",
        help="装哪种精度的 onnx：int8（125 MB，默认）/ fp32（600 MB）/ both",
    )
    ap.add_argument(
        "--keep-epochs",
        type=int,
        default=2,
        help="训练后保留几个 epoch checkpoint（每个 1.87 GB，旧的会删）",
    )
    ap.add_argument("--tokenizer", default="emilia", choices=["emilia", "libritts", "espeak", "simple"])
    ap.add_argument("--iters", type=int, default=300, help="训练迭代数（4.8 分钟数据别超过几百）")
    ap.add_argument("--lr", type=float, default=1e-4, help="官方微调用 1e-4")
    ap.add_argument("--max-duration", type=int, default=60, help="一个 batch 的总时长（秒）")
    ap.add_argument("--max-len", type=float, default=30.0)
    ap.add_argument("--min-len", type=float, default=0.5)
    ap.add_argument("--num-workers", type=int, default=0)
    ap.add_argument("--threads", type=int, default=0, help="CPU 训练线程数（0 = 用满所有逻辑核）")
    ap.add_argument("--dev-cuts", type=int, default=10, help="验证集留几条（lhotse 硬要求 ≥10）")
    ap.add_argument("--keep-last-k", type=int, default=3, help="保留几个分步 checkpoint（含优化器状态，每个 1.87 GB）")
    ap.add_argument("--ckpt", default="", help="导出时指定 checkpoint 文件名（默认自动挑）")
    # lhotse 默认 30 个桶，但我们的训练集只有 27 条 → 会直接 AssertionError
    ap.add_argument("--num-buckets", type=int, default=8, help="lhotse 分桶数，必须 ≤ 训练样本数")
    ap.add_argument("--jobs", type=int, default=2, help="数据准备并行数（小数据集别开大）")
    ap.add_argument("--force", action="store_true", help="装模型时覆盖已有备份")
    arglist = ap.parse_args()
    args = arglist
    if args.weights is None:
        args.weights = DEFAULT_REPO / "download" / args.model_variant
    # ★前缀别手写★：它同时决定 manifest 名和默认安装目录，写错就会出现
    # 「模型装到 personas/kalsit、角色 id 却是 kaltsit」这类不一致（踩过一次）。
    # 默认直接取音频目录名，再跟人格文件里的 id 对一遍。
    if not args.prefix:
        args.prefix = Path(args.audio_dir).name
        print(f"prefix 未指定 → 用音频目录名：{args.prefix}")
    persona_json = Path(args.audio_dir).parent / f"{args.prefix}.json"
    if persona_json.exists():
        try:
            data = json.loads(persona_json.read_text(encoding="utf-8"))
            cid = str(data.get("id") or "").strip()
            if cid and cid != args.prefix:
                print(
                    f"  ⚠ 人格文件 {persona_json.name} 里的 id 是 {cid!r}，与 prefix "
                    f"{args.prefix!r} 不一致：\n"
                    f"    模型会装到 personas/{args.prefix}，而 persona 的 voice_model 按 id 找，"
                    f"会找不到。建议加 --prefix {cid}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  （读人格文件失败，跳过一致性检查：{exc}）")
    if args.threads <= 0:
        args.threads = os.cpu_count() or 1
    print(f"线程数：{args.threads}　预训练：{args.model_variant}（{args.weights}）")
    patch_repo()

    stages = {
        1: ("数据集（TSV + 24k wav）", stage_dataset),
        2: ("lhotse manifest", stage_manifests),
        3: ("token 化", stage_tokens),
        4: ("fbank 特征", stage_fbank),
        5: ("训练", stage_train),
        6: ("平均 checkpoint", stage_average),
        7: ("导出 ONNX", stage_export),
        8: ("装成角色模型目录", stage_install),
    }

    if ensure_weights(args.weights, args.model_variant) != 0:
        return 1
    if stage_env(args) != 0:
        return 1
    if args.stage == 0 and args.stop_stage == 0:
        print("\n（--stage 0 只是检查环境；要开跑就指定 --stage 1 --stop-stage N）")
        return 0

    for number in range(args.stage, args.stop_stage + 1):
        if number == 0:
            continue
        if number not in stages:
            raise SystemExit(f"没有第 {number} 步（只有 1~8）")
        label, fn = stages[number]
        print(f"\n{'=' * 64}\n第 {number} 步：{label}\n{'=' * 64}")
        if fn(args) != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
