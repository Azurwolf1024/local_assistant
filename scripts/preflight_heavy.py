"""跑重活（全套自测 / 评测）之前的起飞前检查。

★为什么要有这个★
这台机器 31.5 GB 内存，但空转时也只剩 ~5 GB（编辑器 + 浏览器就吃掉了大半）。
Piper 训练崩了十几次，退出码全是 `3221225477`（0xC0000005，访问违例）——
就是「保存检查点那一刻内存不够」。所以**只要训练在跑，就别再往机器上压重活**：
跑一次全套自测会把 Whisper / SenseVoice / ZipVoice / Ollama 全加载一遍，几个 GB 就这么没了。

用法（`sessions\\run_tests.cmd` 会自动调用）：

    python scripts\\preflight_heavy.py            # 不够就退出码 1，够就 0
    python scripts\\preflight_heavy.py --force    # 明知有风险也要跑

判据（两条，任一命中就拦）：
  1. 有训练在看（看门狗锁里是活着的进程，或有 `train_piper*.py` 在跑）且空闲内存 < 8 GB；
  2. 空闲内存 < 4 GB（没训练也拦 —— 这么点内存跑什么都会崩）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

TRAIN_WHEN_BUSY_GB = 8.0
HARD_FLOOR_GB = 4.0


def free_ram_gb() -> float:
    """空闲物理内存（GB）。查不到就返回一个很大的数（宁可放过，不要误拦）。"""
    if sys.platform != "win32":
        return 999.0
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return int(out) / 1024 / 1024
    except Exception:  # noqa: BLE001 - 查不到就别拦
        return 999.0


def trainings_running() -> list[int]:
    """正在跑的 Piper 训练 PID（空 = 没训练）。★只读，不碰对方★。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=25,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    hits = [ln for ln in out.splitlines() if "train_piper" in ln and "preflight" not in ln]
    # venv 的启动器 + 真解释器会各出现一次，去重只看「有几个训练」
    return sorted({hash(ln) % 100000 for ln in hits})


def main(argv: list[str]) -> int:
    force = "--force" in argv
    free = free_ram_gb()
    trains = trainings_running()
    busy = bool(trains)

    print(f"[起飞检查] 空闲内存 {free:.2f} GB；检测到 {'训练在跑' if busy else '没有训练'}。")

    if free >= 999:  # 非 Windows / 查不到
        return 0
    if free < HARD_FLOOR_GB:
        print(f"  ✗ 空闲内存不足 {HARD_FLOOR_GB:.0f} GB —— 现在跑重活极可能把正在跑的东西一起弄崩。")
    elif busy and free < TRAIN_WHEN_BUSY_GB:
        print(f"  ✗ 训练在跑，而空闲只有 {free:.2f} GB（要求 ≥ {TRAIN_WHEN_BUSY_GB:.0f} GB）。")
        print("     全套自测会把 Whisper / SenseVoice / ZipVoice 全加载一遍，训练很可能在存检查点时崩掉。")
    else:
        print("  ✓ 可以跑。")
        return 0

    if force:
        print("  → --force，照跑（出事别怪没人提醒）。")
        return 0
    print("  → 已拦下。想跑请：等训练间隙 / 先停训练（sessions 里有停止脚本）/ 或加 --force。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
