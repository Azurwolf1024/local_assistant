"""`scripts/train_piper_watch.py` 的离线自测（不联网、不起训练、不碰 PowerShell）。

为什么这些函数值得单独测：看板的价值全在「说真话」——
它要是把速率算错、把早就死掉的 version 目录当成进度、或者把看门狗进程的内存
当成训练的内存，用户就会**照着错的信息做决定**（比如以为还在跑、或者以为很健康）。
这三件事都真的发生过一次，只有断言能挡住。

    python scripts\\test_train_watch.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.train_piper_watch import (  # noqa: E402
    human_age,
    human_duration,
    latest_version,
    match_process,
    newest_checkpoint,
    parse_attempts,
    parse_progress_hint,
    rate_per_min,
    render_arm,
    select_arms,
    series_stats,
    version_key,
)

FAILED: list[str] = []
_UNSET = object()  # ★不能用 None 当「没给期望」★：好几条断言期望的正好就是 None


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


def make_version(root: Path, name: str, age_seconds: float = 0.0) -> Path:
    """造一个假的 version 目录（可以指定它「多久以前」被动过）。"""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def main() -> int:
    print("=" * 70)
    print(" 训练看板自测（版本排序 / 速率 / 进程归属 / 降级渲染）")
    print("=" * 70)

    print("\n[1] version 目录排序：★version_10 必须比 version_9 新★（按名字排会反）")
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        make_version(logs, "version_9", age_seconds=600)
        make_version(logs, "version_10", age_seconds=10)
        check("latest_version 取 version_10", latest_version(logs).name, "version_10")
        check("version_key 数字优先", version_key(logs / "version_10")[0], 10)
        check("没有 version_* 目录 → None", latest_version(logs / "空的"), None)

    print("\n[2] parse_progress_hint：目标步数从那句日志里来")
    real = "训练样本 24 条 → 每 epoch 6 步；共 300 epoch ≈ 1800 步\n"
    check("真实日志行", parse_progress_hint(real),
          {"steps_per_epoch": 6, "epochs": 300, "target_steps": 1800})
    check("切短数据集那行", parse_progress_hint("训练样本 69 条 → 每 epoch 9 步；共 200 epoch ≈ 1800 步"),
          {"steps_per_epoch": 9, "epochs": 200, "target_steps": 1800})
    check("无关文本 → 空 dict", parse_progress_hint("GPU available: False"), {})
    check("空串不炸", parse_progress_hint(""), {})

    print("\n[3] parse_attempts：本次启动是第几次 + 启动时可用内存 + 累计崩过几次")
    watchdog = (
        "=== [09-25 21:12:03] 第 1 次尝试：从 epoch=44-step=540.ckpt 开始；可用内存 9.2 GB\n"
        "★ [09-25 21:12:47] 第 1 次尝试崩了：0xC0000005 访问违例（跑了 0.7 分钟）→ 自动重启\n"
        "=== [09-25 21:13:00] 第 2 次尝试：从 epoch=14-step=180.ckpt 开始；可用内存 8.3 GB\n"
    )
    check("本次启动 = 最后一次尝试", parse_attempts(watchdog)[0], 2)
    check("内存取最后一次启动的", parse_attempts(watchdog)[1], 8.3)
    check("累计崩溃次数", parse_attempts(watchdog)[2], 1)
    check("没有尝试记录 → (0, None, 0)", parse_attempts("训练样本 24 条"), (0, None, 0))
    # ★追加写的日志里，上一轮跑到 7、新的一轮从 1 重数★：必须报「第 1 次」，不是 max=7
    appended = (
        "=== 第 7 次尝试：从 epoch=59-step=1080.ckpt 开始；可用内存 8.1 GB\n"
        "=== [09-26 03:56:20] 第 1 次尝试：从 epoch=29-step=540.ckpt 开始；可用内存 12.4 GB\n"
    )
    check("追加日志里新看门狗报第 1 次（不是 7）", parse_attempts(appended)[0], 1)
    check("此时内存取新的那次", parse_attempts(appended)[1], 12.4)

    print("\n[4] series_stats：首段均值 / 末值 / 降幅")
    series = [(0, 100.0, 0.0), (1, 80.0, 30.0), (2, 40.0, 60.0), (3, 30.0, 90.0), (4, 20.0, 120.0)]
    stats = series_stats(series)
    check("首段 = 前 10%（至少 1 个点）", stats["head"], 100.0)
    check("末值", stats["last"], 20.0)
    check("降幅 80%", round(stats["drop_pct"]), 80)
    check("点个数", int(stats["count"]), 5)
    check("空序列 → 空 dict", series_stats([]), {})

    print("\n[5] rate_per_min：★用事件自带的时间戳算，不用 now★")
    # 每 30 秒一个 step → 2 步/分
    even = [(i, 1.0, i * 30.0) for i in range(10)]
    check("30 秒/步 → 2 步/分", round(rate_per_min(even), 2), 2.0)
    check("只有一个点 → 0（算不出来）", rate_per_min([(0, 1.0, 0.0)]), 0.0)
    check("时间没前进 → 0（不能除零）", rate_per_min([(0, 1.0, 5.0), (1, 1.0, 5.0)]), 0.0)
    check("step 没前进 → 0", rate_per_min([(3, 1.0, 0.0), (3, 1.0, 60.0)]), 0.0)

    print("\n[6] human_duration / human_age：单位切换")
    check("45 秒", human_duration(45), "45 秒")
    check("30 分钟", human_duration(1800), "30 分钟")
    check("5 小时", human_duration(5 * 3600), "5.0 小时")
    check("3 天", human_duration(3 * 86400), "3.0 天")
    check("NaN → ?", human_duration(float("nan")), "?")
    check("45 秒前", human_age(45), "45 秒前")
    check("刚满 90 秒就换成分钟", human_age(90), "2 分钟前")
    check("30 分钟前", human_age(1800), "30 分钟前")
    check("2 小时前", human_age(2 * 3600), "2.0 小时前")

    print("\n[7] match_process：★看门狗和训练都带 --out，必须挑内存大的那个★")
    arm = Path("data/piper/kaltsit_long/exp_A")
    procs = [
        {"ProcessId": 111, "WorkingSetSize": 20 * 1024 ** 2,
         "CommandLine": ".venv-piper\\Scripts\\python.exe scripts\\train_piper_forever.py --out data/piper/kaltsit_long/exp_A"},
        {"ProcessId": 222, "WorkingSetSize": 5 * 1024 ** 3,
         "CommandLine": "python -u scripts\\train_piper.py --out data/piper/kaltsit_long/exp_A --lr 1e-5"},
    ]
    hit = match_process(procs, arm)
    check("挑到训练进程（PID 222）", hit.get("ProcessId"), 222)
    check("不是看门狗（PID 111）", hit.get("ProcessId") != 111, True)
    check("其它臂不串台",
          match_process([procs[1]], Path("data/piper/kaltsit_split/exp_B")), None)
    check("进程表为空 → None", match_process([], arm), None)

    print("\n[8] newest_checkpoint：跨 version 取 mtime 最新的（看门狗就是照它续跑）")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        old = make_version(out, "lightning_logs/version_0/checkpoints")
        new = make_version(out, "lightning_logs/version_3/checkpoints")
        for target, name, age in ((old, "epoch=9-step=180.ckpt", 900), (new, "epoch=44-step=540.ckpt", 5)):
            (target / name).write_text("x", encoding="utf-8")
            os.utime(target / name, (time.time() - age, time.time() - age))
        check("取最新那个", newest_checkpoint(out).name, "epoch=44-step=540.ckpt")
        check("一个都没有 → None", newest_checkpoint(out / "nope"), None)

    print("\n[9] render_arm：有进度 / 没 tfevents / 没进程 三种降级")
    info = {
        "name": "exp_A", "dataset": "kaltsit_long", "version": "version_2",
        "lr": "1.0e-05", "batch": "4", "pid": 222, "working_set_gb": 5.0,
        "scalars": {
            "loss_gen_all": [(i, 100.0 - i, 1000.0 + i * 60) for i in range(10)],
            "epoch": [(i, float(i), 1000.0 + i * 60) for i in range(10)],
        },
        "hint": {"steps_per_epoch": 6, "epochs": 300, "target_steps": 1800},
        "ckpt": Path("epoch=29-step=360.ckpt"), "ckpt_age": 600.0, "ckpt_epoch": 29, "ckpt_count": 2,
        "attempts": 3, "ram_at_start": 8.3, "crashes": 2,
    }
    text = "\n".join(render_arm(info))
    check("有在跑标记", "在跑" in text, True)
    check("显示 PID", "222" in text, True)
    check("步数带目标与百分比", "step 9/1800（0%）" in text, True)
    check("epoch 带目标", "epoch 9/300" in text, True)
    check("显示 loss", "loss_gen" in text, True)
    check("显示 ETA", "若继续跑还需" in text, True)
    check("显示 checkpoint", "epoch=29-step=360.ckpt" in text, True)
    check("显示看门狗本次是第几次", "本次启动第 3 次尝试" in text, True)
    check("显示累计崩溃次数", "累计崩过 2 次" in text, True)

    dead = dict(info)
    dead.pop("pid"); dead.pop("working_set_gb")
    text_dead = "\n".join(render_arm(dead))
    check("已停的臂：明说不在跑了", "★已经不在跑了★" in text_dead, True)
    check("已停的臂：不说「跑完」", "跑完）" in text_dead, False)
    check("已停的臂：剩余步数照说", "剩 1791 步" in text_dead, True)

    bare = dict(info)
    bare.pop("scalars"); bare.pop("pid"); bare.pop("working_set_gb")
    text2 = "\n".join(render_arm(bare))
    check("没进程 → 明说", "没找到训练进程" in text2, True)
    check("没 tfevents → 明说", "还没有 tfevents" in text2, True)
    check("降级后不冒充进度", "预计还需" in text2 or "若继续跑还需" in text2, False)

    print("\n[10] select_arms：点名 / 活跃度过滤 / --all")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fresh, stale = root / "exp_A", root / "exp_B"
        make_version(fresh / "lightning_logs", "version_0", age_seconds=60)
        make_version(stale / "lightning_logs", "version_0", age_seconds=10 * 3600)
        dirs = [fresh, stale]
        picked, hidden = select_arms(dirs, [], "A", 3.0, False)
        check("点名 A 只出 A", [p.name for p in picked], ["exp_A"])
        check("点名时不算隐藏", hidden, 0)
        picked, hidden = select_arms(dirs, [], "", 3.0, False)
        check("默认只出活跃的", [p.name for p in picked], ["exp_A"])
        check("隐藏计数正确", hidden, 1)
        picked, hidden = select_arms(dirs, [], "", 3.0, True)
        check("--all 全出", sorted(p.name for p in picked), ["exp_A", "exp_B"])
        check("--all 时无隐藏", hidden, 0)

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
