"""带「崩溃自动重启」的训练看门狗（Piper 微调用）。

为什么要它
----------
这台机器上 Piper 的训练会**概率性地硬崩**：退出码 `0xC0000005`（访问违例），
**没有 traceback、没有 Python 异常**，崩在 worker 线程里（gdb 只能看到
`[Thread ... exited with code 3221225477]`）。
已经查清的事（详见 docs/ENGINEERING_LOG.md 第 30 节）：
  * 与学习率无关、与并行无关、与存 checkpoint 无关、与内存无关；
  * 长片段数据集（24 条，帧数/音素id 中位 1.91）跑了几百步没崩；
  * 切短数据集（69 条）**同一份数据**：C 组跑到 26 步没崩、B 组 25 步崩了
    —— 所以是**概率性**的，不是某条样本必崩。
没查清的是确切的触发机制（怀疑 `rand_slice_segments` 抽到的切片位置）。

既然做不到「让它不崩」，就做到「崩了也不心疼」：**崩了就自动从最近的 checkpoint
接着跑**。每崩一次最多损失 `--checkpoint-epochs` 个 epoch。

    D:\\local_AI\\.venv-piper\\Scripts\\python.exe scripts\\train_piper_forever.py ^
        --dataset-dir data\\piper\\kaltsit_split\\training ^
        --checkpoint models\\tts\\piper\\_train\\zh_CN-huayan-medium.ckpt ^
        --out data\\piper\\kaltsit_split\\exp_B ^
        --lr 1e-5 --batch-size 8 --epochs 200 --checkpoint-epochs 25 --threads 4

★注意★：续跑时**只搬权重、不恢复优化器状态**（`train_piper.py --checkpoint` 的语义），
所以每次重启那一小段的学习率曲线会有台阶 —— 对微调（lr 1e-5 量级）可以接受，
但别把这个脚本当成严谨的「断点续训」。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRAINER = ROOT / "scripts" / "train_piper.py"
CRASH_CODES = {
    -1073741819: "0xC0000005 访问违例",
    -1073741818: "0xC0000006 页错误",
    -1073741795: "0xC000001D 非法指令",
}


def newest_checkpoint(out: Path) -> Path | None:
    """`<out>/lightning_logs/version_*/checkpoints/*.ckpt` 里最新的那个（按修改时间）。"""
    logs = out / "lightning_logs"
    if not logs.is_dir():
        return None
    found = [p for p in logs.glob("version_*/checkpoints/*.ckpt") if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def main() -> int:
    ap = argparse.ArgumentParser(description="崩溃自动重启的训练看门狗")
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--checkpoint", required=True, help="底模 ckpt（第一次用）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--checkpoint-epochs", type=int, default=25)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-frames-per-id", type=float, default=0.7)
    ap.add_argument("--max-restarts", type=int, default=30)
    ap.add_argument("--log", default="", help="训练输出写到哪（默认 sessions/train_forever.log）")
    args = ap.parse_args()

    out = Path(args.out)
    log_path = Path(args.log) if args.log else ROOT / "sessions" / "train_forever.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, args.max_restarts + 1):
        resume = newest_checkpoint(out)
        source = resume if resume else Path(args.checkpoint)
        print(f"\n=== 第 {attempt} 次尝试：从 {source.name} 开始 "
              f"（{'上次的 checkpoint' if resume else '底模'}）", flush=True)
        cmd = [
            sys.executable, "-u", str(TRAINER),
            "--dataset-dir", str(args.dataset_dir),
            "--checkpoint", str(source),
            "--out", str(out),
            "--lr", str(args.lr),
            "--batch-size", str(args.batch_size),
            "--epochs", str(args.epochs),
            "--checkpoint-epochs", str(args.checkpoint_epochs),
            "--threads", str(args.threads),
            "--min-frames-per-id", str(args.min_frames_per_id),
        ]
        started = time.time()
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n\n########## 第 {attempt} 次尝试 source={source} ##########\n")
            handle.flush()
            code = subprocess.call(cmd, stdout=handle, stderr=subprocess.STDOUT)
        spent = (time.time() - started) / 60.0

        if code == 0:
            print(f"√ 训练正常结束（第 {attempt} 次尝试，用时 {spent:.1f} 分钟）")
            return 0
        why = CRASH_CODES.get(code, f"退出码 {code}")
        print(f"★ 第 {attempt} 次尝试崩了：{why}（跑了 {spent:.1f} 分钟）→ 自动重启")
        if spent < 0.5:
            print("   ★注意★ 这次连 30 秒都没撑到，可能不是偶发崩溃 —— "
                  "先去看看上面日志里的最后一次报错")
        time.sleep(5)

    print(f"★ 连续 {args.max_restarts} 次都没跑完，先停下来看看日志：{log_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
