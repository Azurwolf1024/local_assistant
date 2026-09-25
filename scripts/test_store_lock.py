"""存储层跨进程写安全的自测（控制台 UI 是另一个进程，改的是同一批文件）。

要防的那个 bug 很阴：整表回写模式下两个进程各持一份快照，后写的**静默覆盖**先写的，
**不报错、不崩溃，数据就没了**。光靠「load() 能看见别人改动」挡不住它——
「看得见」和「不会互相覆盖」是两件事。

    python scripts/test_store_lock.py

§3 是真正的回归测试：两个**真进程**同时改同一个文件，各自在锁内故意睡一下把窗口撑开。
没有锁的话，两边都读到空表、各自写回一条 → 最后只剩 1 条（必错）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.store import JsonStore  # noqa: E402

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


# 子进程要跑的代码。用占位符替换而不是 f-string：里面全是花括号，转义容易写错。
_CHILD = r"""
import sys, time
sys.path.insert(0, r"__ROOT__")
from voice_loop.store import JsonStore
store = JsonStore(r"__PATH__", default=[])
# ★整段持锁★：读 → 睡（人为把窗口撑大）→ 改 → 写
with store.locked():
    items = store.load(force=True)
    time.sleep(float("__SLEEP__"))
    items.append({"title": "__TAG__"})
    store.save(items)
"""


def _spawn(path: Path, tag: str, sleep: float) -> subprocess.Popen:
    code = (
        _CHILD.replace("__ROOT__", str(ROOT))
        .replace("__PATH__", str(path))
        .replace("__SLEEP__", str(sleep))
        .replace("__TAG__", tag)
    )
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def section_1() -> None:
    print("\n[1] 可重入：嵌套持锁不能自己把自己锁死")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.json"
        store = JsonStore(path, default=[])
        t0 = time.perf_counter()
        with store.locked():
            with store.locked():          # 嵌套一层
                store.append({"title": "x"})   # 内部还会再拿一次
        cost = time.perf_counter() - t0
        check("嵌套 3 层没卡住（<2s）", cost < 2.0, detail=f"{cost:.3f}s")
        check("数据写进去了", len(store.load(force=True)), 1)


def section_2() -> None:
    print("\n[2] 同进程多线程同时 append：一条都不能丢")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.json"
        store = JsonStore(path, default=[])
        n_threads, per = 6, 25

        def work(tag: int) -> None:
            for i in range(per):
                store.append({"title": f"t{tag}-{i}"})

        threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        items = JsonStore(path, default=[]).load(force=True)
        check("条数对得上", len(items), n_threads * per)
        check("id 不重复", len({it["id"] for it in items}), len(items))


def section_3() -> None:
    print("\n[3] ★真·跨进程★：两个进程同时改同一个文件（回归测试）")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        JsonStore(path, default=[]).ensure()
        # A 先拿锁并睡 0.35s；B 晚 0.05s 起来。没有锁时两边都读到空表，最后只剩 1 条。
        a = _spawn(path, "A", 0.35)
        time.sleep(0.05)
        b = _spawn(path, "B", 0.05)
        out_a, err_a = a.communicate(timeout=60)
        out_b, err_b = b.communicate(timeout=60)
        if a.returncode or b.returncode:
            print(f"    子进程报错：A={err_a.decode(errors='replace')[:200]} B={err_b.decode(errors='replace')[:200]}")
        items = JsonStore(path, default=[]).load(force=True)
        titles = sorted(str(it.get("title")) for it in items)
        check("两个进程的改动都在（没有互相覆盖）", titles, ["A", "B"])
        check("子进程都正常退出", (a.returncode, b.returncode), (0, 0))


def section_4() -> None:
    print("\n[4] 外部（编辑器 / 另一个进程）手改文件 → 看得见")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "e.json"
        store = JsonStore(path, default=[])
        store.append({"title": "old"})
        _ = store.load()                                   # 让缓存热起来
        path.write_text(json.dumps([{"title": "hand-edited"}], ensure_ascii=False), encoding="utf-8")
        got = JsonStore.load(store, force=False)
        check("只靠 mtime+大小也能重读", [it["title"] for it in got], ["hand-edited"])


def section_5() -> None:
    print("\n[5] 锁文件是单独的 .lock，不碰数据文件")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "l.json"
        store = JsonStore(path, default=[])
        store.append({"title": "keep"})
        check("lock_path 不是数据文件", str(store.lock_path) != str(store.path))
        check("lock_path 以 .lock 结尾", str(store.lock_path).endswith(".lock"))
        with store.locked():
            check("拿锁期间数据文件仍然可读可替换（写盘是 tmp+replace）", path.exists())
        check("锁文件已生成", store.lock_path.exists())
        check("锁没有把数据弄脏", [it["title"] for it in store.load(force=True)], ["keep"])


def main() -> int:
    print("=" * 70)
    print(" 存储层跨进程写安全自测")
    print("=" * 70)
    section_1()
    section_2()
    section_3()
    section_4()
    section_5()
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
