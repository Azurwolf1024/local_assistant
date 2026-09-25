"""`scripts/train_piper_forever.py` 的离线自测（防重入锁 + PID 判活）。

为什么值得单独测：这套「锁」是用来挡「同一个目录起两份训练」的，
而它一旦判错，后果是两个方向都很难受 ——
  * 判成「没人跑」→ 两份训练抢内存（2026-09-26 凌晨就是这么把四个进程一起弄死的）；
  * 判成「有人在跑」→ 你想训练却起不来，而且理由还是假的。
所以死锁文件必须能自动失效、自己的锁不能算「别人在跑」。

    python scripts\\test_train_forever.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.train_piper_forever import (  # noqa: E402
    LOCK_NAME,
    other_watchdog,
    pid_alive,
    release_lock,
)

FAILED: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    if want is _UNSET:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def write_lock(out: Path, text: str) -> None:
    (out / LOCK_NAME).write_text(text, encoding="utf-8")


def main() -> int:
    print("=" * 70)
    print(" 训练看门狗自测（防重入锁 / PID 判活）")
    print("=" * 70)

    print("\n[1] pid_alive：★不能用 os.kill(pid, 0)★（Windows 上那会直接杀掉对方）")
    check("自己活着", pid_alive(os.getpid()), True)
    check("不存在的 PID 不活", pid_alive(999_999), False)
    check("0 / 负数不算活", pid_alive(0), False)

    print("\n[2] other_watchdog：谁在占这个输出目录")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        check("没有锁文件 → None（可以起）", other_watchdog(out), None)

        write_lock(out, "999999 09-26 04:00:00\n")
        check("锁里是**已经死掉**的 PID → None（死锁自动失效）", other_watchdog(out), None)

        write_lock(out, f"{os.getpid()} 09-26 04:00:00\n")
        check("锁里是**自己** → None（不是‘别人在跑’）", other_watchdog(out), None)

        write_lock(out, "垃圾内容")
        check("锁文件坏了 → None（宁可放行，别把用户挡在外面）", other_watchdog(out), None)

        write_lock(out, "")
        check("锁文件是空的 → None", other_watchdog(out), None)

        # 真的起一个活着的进程，它的 PID 就应该是「有人在跑」
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            write_lock(out, f"{sleeper.pid} 09-26 04:00:00\n")
            check("锁里是**活着的**别的进程 → 报出它的 PID", other_watchdog(out), sleeper.pid)
        finally:
            sleeper.kill()
            sleeper.wait(timeout=10)
        check("那个进程一死，锁立刻失效", other_watchdog(out), None)

    print("\n[3] release_lock：只删自己的那把")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        write_lock(out, f"{os.getpid()} 09-26 04:00:00\n")
        release_lock(out)
        check("自己的锁被删掉", (out / LOCK_NAME).exists(), False)

        write_lock(out, "999999 09-26 04:00:00\n")
        release_lock(out)
        check("别人的锁不许动", (out / LOCK_NAME).exists(), True)

        release_lock(out)  # 已经没有自己的锁了
        check("没有自己的锁也不炸", True)

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
