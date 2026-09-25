"""控制台能不能在终端里退出（★回归测试★，对应「Ctrl+C 退不出、只能关网页」）。

为什么要单独一个测试文件：这个 bug **只在「网页还开着」时才出现**——
SSE 是永不结束的响应，而 uvicorn 的关闭顺序是「先等活跃连接结束，再跑 lifespan shutdown」，
所以订阅者不自己收尾的话，服务会一直等下去（实测 20 秒都退不出）。

    python scripts\test_console_exit.py

两组对照：
    ① 挂着 SSE（= 网页开着）：应该在几秒内退出 —— 靠「关闭时叫醒订阅者」
    ② 没有 SSE：本来也该退出 —— 保证别把普通情况弄坏
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def hold_sse(port: int, stop: threading.Event, opened: threading.Event) -> None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=60) as resp:
            opened.set()
            while not stop.is_set():
                if not resp.read1(1024):
                    break
                time.sleep(0.05)
    except Exception:
        opened.set()


def run_case(*, with_sse: bool, wait: float = 15.0) -> tuple[bool, float, str]:
    """起控制台 → （可选）挂 SSE → 发 Ctrl+C → 量退出耗时。"""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "ui", "--port", str(port), "--no-browser"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONUTF8="1"),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    stop = threading.Event()
    try:
        ready = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    json.loads(r.read().decode("utf-8"))
                    ready = True
                    break
            except Exception:
                time.sleep(0.2)
        if not ready:
            out = proc.communicate(timeout=5)[0]
            return False, 0.0, f"控制台没起来：{out[:300]}"

        thread = None
        if with_sse:
            opened = threading.Event()
            thread = threading.Thread(target=hold_sse, args=(port, stop, opened), daemon=True)
            thread.start()
            opened.wait(timeout=10)
            time.sleep(0.4)          # 让订阅者真正挂上

        t0 = time.perf_counter()
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=wait)
            cost = time.perf_counter() - t0
            return cost < 10.0, cost, (out or "")
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, time.perf_counter() - t0, "★卡住了：没在预期时间内退出★"
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()


def main() -> int:
    print("=" * 70)
    print(" 控制台退出测试（网页开着时也要能在终端退出）")
    print("=" * 70)

    print("\n[1] ★网页开着（挂着 SSE）时按 Ctrl+C★ —— 就是用户遇到的那一种")
    ok, cost, out = run_case(with_sse=True)
    check("几秒内退出", ok, True, detail=f"{cost:.1f}s")
    check("打印了退出提示", "控制台已退出" in out, True, detail=out.strip().splitlines()[-1] if out.strip() else "")

    print("\n[2] 没有 SSE 的对照组（别把普通情况弄坏）")
    ok2, cost2, _ = run_case(with_sse=False)
    check("几秒内退出", ok2, True, detail=f"{cost2:.1f}s")

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
