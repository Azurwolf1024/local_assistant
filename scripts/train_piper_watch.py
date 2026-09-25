"""Piper 微调进度看板（终端里实时刷新，Ctrl+C 退出）。

为什么需要它
------------
1. ★**日志里没有进度**★：`train_piper.py` 关掉了 Lightning 的进度条
   （`enable_progress_bar=False`），关掉之后 logger 也**不再逐行打印 loss** ——
   所以 `sessions/train_*_watchdog.log` 里只有启动那几行，光看日志根本不知道跑到哪了。
   真正有进度的是 **tfevents**（`<out>/lightning_logs/version_*/events.out.tfevents.*`）。
2. ★**必须同时盯内存**★：这台机器上训练会因内存不足在**写 checkpoint 时硬崩**
   （`0xC0000005`，没有 traceback；根因见 docs/ENGINEERING_LOG.md 第 30 节）。
   可用内存低于 3 GB 就离崩溃不远了，看板把它摆在最显眼的位置。
3. ★**看门狗重启会换目录**★：每次重启新开一个 `version_N`，步数从 0 重新数、
   但 checkpoint 里的 epoch 是接着上一段的。所以「本轮（最新 version）」和
   「重启过几次 / 最新 checkpoint 到哪」必须**分开显示**，混在一起会得出错的进度。

用法
----
    python scripts\\train_piper_watch.py              # 一直刷新（每 15 秒）
    python scripts\\train_piper_watch.py --once       # 只看一眼就退（给脚本/日志用）
    python scripts\\train_piper_watch.py -i 30        # 30 秒刷一次
    python scripts\\train_piper_watch.py --arms A,B   # 只看这两支
    python scripts\\train_piper_watch.py --all        # 连不活跃的实验目录一起看

读 tfevents 需要 tensorboard，而它**只装在 `.venv-piper` 里** → 这个脚本发现当前解释器
没有 tensorboard 时，会**自动用 `.venv-piper\\Scripts\\python.exe` 重新跑自己**，
所以用系统 python 直接跑也一样能用（`--no-reexec` 可关掉这个行为）。

想看曲线图（不是看板）：`.venv-piper\\Scripts\\tensorboard.exe --logdir data\\piper`，
然后浏览器开 http://localhost:6006 。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPER_VENV_PY = ROOT / ".venv-piper" / "Scripts" / "python.exe"
ARM_GLOB = "data/piper/*/exp_*"

# 训练日志里 train_piper.py 打的那行：「训练样本 24 条 → 每 epoch 6 步；共 300 epoch ≈ 1800 步」
PROGRESS_HINT_RE = re.compile(r"每 epoch (\d+) 步；共 (\d+) epoch ≈ (\d+) 步")
ATTEMPT_RE = re.compile(r"第 (\d+) 次尝试")
CRASH_RE = re.compile(r"第 \d+ 次尝试崩了")
RAM_AT_START_RE = re.compile(r"可用内存 ([\d.]+) GB")
CKPT_RE = re.compile(r"epoch=(\d+)-step=(\d+)")


# --------------------------------------------------------------------------- #
# 纯函数（可单测，见 scripts/test_train_watch.py）
# --------------------------------------------------------------------------- #
def version_key(version_dir: Path) -> tuple[int, float]:
    """version 目录的排序键。

    ★别按名字排★：`sorted()` 下 `version_10` 会排在 `version_9` **前面**（字符串比较），
    于是看板会显示一个早就死掉的旧目录。按「编号 + 修改时间」排才对。
    """
    found = re.search(r"version_(\d+)", version_dir.name)
    number = int(found.group(1)) if found else -1
    try:
        stamp = version_dir.stat().st_mtime
    except OSError:
        stamp = 0.0
    return (number, stamp)


def latest_version(logs: Path) -> Path | None:
    """最新的 version 目录（= 看门狗最近一次启动的那个）。"""
    versions = [p for p in logs.glob("version_*") if p.is_dir()]
    return max(versions, key=version_key) if versions else None


def parse_progress_hint(text: str) -> dict[str, int]:
    """从训练日志里抠出「每 epoch N 步；共 E epoch ≈ S 步」→ 目标步数。"""
    found = PROGRESS_HINT_RE.search(text or "")
    if not found:
        return {}
    return {
        "steps_per_epoch": int(found.group(1)),
        "epochs": int(found.group(2)),
        "target_steps": int(found.group(3)),
    }


def parse_attempts(text: str) -> tuple[int, float | None, int]:
    """看门狗日志 →（本次启动是第几次尝试, 启动时可用内存 GB, 这个文件里累计崩过几次）。

    ★取**最后一次**尝试，不取最大值★：console 日志现在是追加写的，新起一个看门狗时
    它的尝试计数从 1 重数，而文件里还留着上一轮跑到 7 的记录 —— 取 max 会把
    「本次启动第 1 次」报成「第 7 次」（改追加之后当场踩到的）。
    内存那一项来自看门狗每次启动打印的「可用内存 X GB」（★它就是为了追这个崩溃加的★）。
    """
    attempts = [int(m.group(1)) for m in ATTEMPT_RE.finditer(text or "")]
    rams = [float(m.group(1)) for m in RAM_AT_START_RE.finditer(text or "")]
    crashes = len(CRASH_RE.findall(text or ""))
    return (attempts[-1] if attempts else 0, rams[-1] if rams else None, crashes)


def human_duration(seconds: float) -> str:
    """秒 → 「3.2 小时」「47 分钟」这种一眼能读的量。"""
    if seconds != seconds or seconds < 0:  # NaN / 负数
        return "?"
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 3600 * 1.5:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 3600 * 48:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def human_age(seconds: float) -> str:
    if seconds != seconds or seconds < 0:
        return "?"
    if seconds < 90:
        return f"{seconds:.0f} 秒前"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def series_stats(events: list[tuple[int, float, float]]) -> dict[str, float]:
    """一串标量 →（首段均值, 末值, 降幅百分比, 速率步/分）。

    `events` 是 `[(step, value, wall_time), ...]`（按时间升序）。
    首段 = 前 10%（至少 1 个点）：微调的 loss 前几个 step 会从很高的地方掉下来，
    拿它跟末值比才看得出「到底有没有在学」。
    """
    if not events:
        return {}
    head_n = max(1, len(events) // 10)
    head = sum(v for _, v, _ in events[:head_n]) / head_n
    last = events[-1][1]
    out = {"head": head, "last": last, "count": float(len(events))}
    out["drop_pct"] = (head - last) / head * 100 if head else 0.0
    out["rate_per_min"] = rate_per_min(events)
    return out


def rate_per_min(events: list[tuple[int, float, float]], window: int = 20) -> float:
    """最近 `window` 个标量算出「步/分」。

    ★必须用 events 里的 wall_time、不能用「现在」★：读文件的那一刻跟最后一个标量
    之间可能有几分钟差（tfevents 是攒着写的），拿 now 算会把速度算低。
    """
    tail = events[-window:] if len(events) > window else events
    if len(tail) < 2:
        return 0.0
    step_span = tail[-1][0] - tail[0][0]
    time_span = tail[-1][2] - tail[0][2]
    if step_span <= 0 or time_span <= 0:
        return 0.0
    return step_span / (time_span / 60.0)


# --------------------------------------------------------------------------- #
# 采集（碰系统的地方，出错一律降级成「不知道」，不让看板整个挂掉）
# --------------------------------------------------------------------------- #
def free_ram_gb() -> float:
    """可用物理内存（GB）。

    实现只有一份：直接用看门狗里的那个（`train_piper_forever.py`）—— 它就是为了
    追这次的崩溃加的，这里再抄一遍迟早会走样（这个项目已经吃过「同名逻辑两份实现」的亏）。
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from train_piper_forever import free_ram_gb as impl  # noqa: PLC0415

        return impl()
    except Exception:  # noqa: BLE001 - 非 Windows / 导入失败就不显示
        return float("nan")


def read_python_processes() -> list[dict]:
    """当前 python.exe 进程（PID / 内存 / 命令行），用来把训练进程跟臂对上。

    走 PowerShell 的 CIM（本机没装 psutil，也**不想为看个进度**去给训练环境加依赖）。
    拿不到就返回空表 —— 看板上少一行，不影响别的。
    """
    if os.name != "nt":
        return []
    query = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Select-Object ProcessId,WorkingSetSize,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        raw = (done.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):  # 只有一个进程时 ConvertTo-Json 给的是对象
            data = [data]
        return [d for d in data if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        return []


def match_process(procs: list[dict], out_dir: Path) -> dict | None:
    """哪条 python 进程属于这个臂。

    ★两个进程都带 `--out ...\\exp_A`★：看门狗 `train_piper_forever.py` 和它拉起的
    训练 `train_piper.py`。占内存、会因内存不足崩掉的是**后者**（几十 MB vs 几个 GB），
    所以取匹配到的里面**工作集最大的那个**，否则看板上会一直显示看门狗的 20 MB。
    """
    needle = str(out_dir).lower()
    name = out_dir.name.lower()
    hits = []
    for proc in procs:
        cmd = str(proc.get("CommandLine") or "").lower()
        if needle in cmd or (name in cmd and "--out" in cmd):
            hits.append(proc)
    if not hits:
        return None
    return max(hits, key=lambda p: int(p.get("WorkingSetSize") or 0))


def read_scalars(version_dir: Path, tags: tuple[str, ...]) -> dict[str, list[tuple[int, float, float]]]:
    """读一个 version 目录里的 tfevents → {tag: [(step, value, wall_time), ...]}。"""
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

    acc = EventAccumulator(str(version_dir), size_guidance={"scalars": 0})
    acc.Reload()
    available = acc.Tags().get("scalars", [])
    out: dict[str, list[tuple[int, float, float]]] = {}
    for tag in tags:
        if tag in available:
            out[tag] = [(e.step, float(e.value), float(e.wall_time)) for e in acc.Scalars(tag)]
    return out


def newest_checkpoint(out_dir: Path) -> Path | None:
    """所有 version 里最新的 checkpoint（按修改时间；看门狗就是照这个续跑的）。"""
    found = [p for p in out_dir.glob("lightning_logs/version_*/checkpoints/*.ckpt") if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def last_progress_hint(out_dir: Path) -> dict[str, int]:
    """目标步数从训练日志里来（`--epochs` 写在那句「共 N epoch ≈ M 步」里）。

    ★一个臂的日志文件叫什么不固定★：走看门狗的是 `train_<臂>_watchdog.log`，
    直接跑 `arm_*.cmd` 的只有 `train_<臂>.log` —— 名字要都试，否则那支就没有 ETA。
    """
    suffix = out_dir.name.replace("exp_", "")
    candidates = [
        f"train_{suffix}_watchdog.log",
        f"train_{suffix}_watchdog_console.log",
        f"train_{suffix}.log",
        "train_forever.log",
    ]
    for name in candidates:
        path = ROOT / "sessions" / name
        if path.is_file():
            hint = parse_progress_hint(path.read_text(encoding="utf-8", errors="replace"))
            if hint:
                return hint
    return {}


def watchdog_state(out_dir: Path) -> tuple[int, float | None, int]:
    """看门狗本次启动是第几次、启动时的可用内存、这个日志里累计崩过几次。"""
    suffix = out_dir.name.replace("exp_", "")
    for name in (f"train_{suffix}_watchdog_console.log", f"train_{suffix}_watchdog.log"):
        path = ROOT / "sessions" / name
        if path.is_file():
            attempts, ram, crashes = parse_attempts(path.read_text(encoding="utf-8", errors="replace"))
            if attempts:
                return attempts, ram, crashes
    return 0, None, 0


def collect_arm(out_dir: Path, procs: list[dict]) -> dict:
    """把一个臂要显示的东西全捞出来（读不到的部分留空，渲染时降级）。"""
    info: dict = {"out": out_dir, "name": out_dir.name, "dataset": out_dir.parent.name}
    logs = out_dir / "lightning_logs"
    version = latest_version(logs) if logs.is_dir() else None
    info["version"] = version.name if version else ""
    if version:
        try:
            info["scalars"] = read_scalars(version, ("loss_gen_all", "loss_disc_all", "epoch"))
        except Exception as exc:  # noqa: BLE001 - 正写着的时候读会失败，不是致命错
            info["read_error"] = str(exc)
        hparams = version / "hparams.yaml"
        if hparams.is_file():
            text = hparams.read_text(encoding="utf-8", errors="replace")
            info["lr"] = next((line.split(":", 1)[1].strip() for line in text.splitlines()
                               if line.startswith("learning_rate")), "")
            info["batch"] = next((line.split(":", 1)[1].strip() for line in text.splitlines()
                                  if line.startswith("batch_size")), "")
    ckpt = newest_checkpoint(out_dir)
    if ckpt:
        info["ckpt"] = ckpt
        info["ckpt_age"] = max(0.0, time.time() - ckpt.stat().st_mtime)
        found = CKPT_RE.search(ckpt.name)
        info["ckpt_epoch"] = int(found.group(1)) if found else -1
        info["ckpt_count"] = len(list(out_dir.glob("lightning_logs/version_*/checkpoints/*.ckpt")))
    info["hint"] = last_progress_hint(out_dir)
    info["attempts"], info["ram_at_start"], info["crashes"] = watchdog_state(out_dir)
    proc = match_process(procs, out_dir)
    if proc:
        info["pid"] = proc.get("ProcessId")
        try:
            info["working_set_gb"] = float(proc.get("WorkingSetSize") or 0) / (1024 ** 3)
        except (TypeError, ValueError):
            pass
    return info


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_arm(info: dict) -> list[str]:
    """一个臂 → 几行文字。★进度分三层说★：本轮(version) / checkpoint / 重启。"""
    title = f"{info['name']}  {info['dataset']}"
    if info.get("lr"):
        title += f"  lr {info['lr']}  batch {info.get('batch') or '-'}"
    if info.get("pid"):
        mem = f"内存 {info['working_set_gb']:.1f} GB" if "working_set_gb" in info else "内存 ?"
        title += f"   ★在跑★ PID {info['pid']} {mem}"
    else:
        title += "   （没找到训练进程：可能已结束、或还没启动）"
    lines = [f"  {title}"]

    scalars = info.get("scalars") or {}
    loss = series_stats(scalars.get("loss_gen_all") or [])
    hint = info.get("hint") or {}
    target = int(hint.get("target_steps") or 0)
    if loss:
        step = int((scalars.get("loss_gen_all") or [(0, 0, 0)])[-1][0])
        epoch_series = scalars.get("epoch") or []
        epoch = int(epoch_series[-1][1]) if epoch_series else -1
        progress = f"step {step}"
        if target:
            progress += f"/{target}（{step / target * 100:.0f}%）"
        if epoch >= 0:
            progress += f"  epoch {epoch}"
        if hint.get("epochs"):
            progress += f"/{hint['epochs']}"
        lines.append(f"    本轮 {info.get('version', '')}：{progress}")
        # ★★重启之后不能用「本轮 step」说进度★：看门狗重启会新开一个 version、
        # 步数从 0 重数，而模型是接着 checkpoint 训的。第一版就因此报了「还需 7.4 小时」，
        # 而模型其实只剩 91 个 epoch（≈ 2 小时）—— 两边的数字对不上会让人做出错的安排。
        ckpt_epoch = int(info.get("ckpt_epoch", -1))
        if ckpt_epoch >= 0 and hint.get("epochs"):
            lines.append(f"    模型：checkpoint epoch {ckpt_epoch}/{hint['epochs']}"
                         f"（{ckpt_epoch / hint['epochs'] * 100:.0f}%）  ← 跨重启的真实进度")

        first_step, _, first_wall = (scalars.get("loss_gen_all") or [(0, 0, 0)])[0]
        elapsed = (scalars.get("loss_gen_all") or [(0, 0, 0)])[-1][2] - first_wall
        if elapsed > 0 and step > first_step:
            lines.append(f"    本轮已跑 {human_duration(elapsed)}"
                         f"（启动于 {(datetime.fromtimestamp(first_wall)).strftime('%m-%d %H:%M')}）")
        lines.append(f"    loss_gen {loss['last']:.1f}（前 10% 均值 {loss['head']:.1f} → "
                     f"{'降' if loss['drop_pct'] >= 0 else '升'} {abs(loss['drop_pct']):.0f}%）"
                     f"   共 {int(loss['count'])} 个点")
        rate = loss.get("rate_per_min") or 0.0
        if rate > 0:
            ckpt_epoch = int(info.get("ckpt_epoch", -1))
            epochs_total = int(hint.get("epochs") or 0)
            per_epoch = int(hint.get("steps_per_epoch") or 0)
            if ckpt_epoch >= 0 and epochs_total and per_epoch:
                # 模型还差多少个 epoch（跨重启有意义），而不是本段的 step
                remaining = max(0, epochs_total - ckpt_epoch) * per_epoch
            else:
                remaining = max(0, target - step)
            eta = ""
            if remaining > 0:
                seconds = remaining / rate * 60
                when = (datetime.now() + timedelta(seconds=seconds)).strftime('%m-%d %H:%M')
                # ★进程已经停了就别假装还在跑★：速率是“当时”的，算出来的完成时间是不存在的
                # （第一版就这样报了「预计还需 2.2 小时」——而那个臂两分钟前已经被关掉了）。
                eta = (f"   若继续跑还需 {human_duration(seconds)}（约 {when} 跑完）"
                       if info.get("pid") else
                       f"   ★已经不在跑了★：剩 {remaining} 步，按停掉前的速率算要 {human_duration(seconds)}")
            elif target:
                eta = "   （已经跑完目标轮数）"
            lines.append(f"    速率 {rate * 60:.0f} 步/小时（{rate:.2f} 步/分）{eta}")
        # ★「最后更新」必须看最后一个标量的时间，不能看目录 mtime★：
        # 目录 mtime 只在增删文件时才变（比如存 checkpoint），训练一路写 events 它不动 ——
        # 拿它当存活信号会得岀「42 分钟没动」（其实一直在跑）。
        last_wall = (scalars.get("loss_gen_all") or scalars.get("epoch") or [])[-1][2]
        lines.append(f"    最后一个标量 {human_age(time.time() - last_wall)}"
                     "（超过 ~5 分钟没动 = 可能卡住/已经死了）")
    elif info.get("read_error"):
        lines.append(f"    读 tfevents 失败（训练正在写？）：{info['read_error']}")
    else:
        lines.append("    还没有 tfevents：训练刚启动，或者还没写第一个标量")

    if info.get("ckpt"):
        lines.append(f"    checkpoint {info.get('ckpt_count', 0)} 个，最新 {info['ckpt'].name}"
                     f"（epoch {info.get('ckpt_epoch', -1)}，{human_age(info['ckpt_age'])}）")
    else:
        lines.append("    还没存过 checkpoint（到 --checkpoint-epochs 才会存）")
    if info.get("attempts"):
        ram = info["ram_at_start"]
        extra = f"，启动时可用内存 {ram:.1f} GB" if ram is not None else ""
        crashed = f"；这个日志里累计崩过 {info['crashes']} 次" if info.get("crashes") else ""
        lines.append(f"    看门狗：本次启动第 {info['attempts']} 次尝试{extra}{crashed}"
                     "（崩了自动从最近 checkpoint 续跑，只搬权重、不恢复优化器）")
    return lines


def render(all_infos: list[dict], hidden: int, interval: int) -> list[str]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"Piper 微调进度   {now}   （每 {interval} 秒刷新，Ctrl+C 退出）", "═" * 78]
    for info in all_infos:
        lines.extend(render_arm(info))
        lines.append("")
    ram = free_ram_gb()
    if ram == ram:  # 不是 NaN
        warn = ""
        if ram < 3.0:
            warn = "   ★★太低了：存档时会硬崩（0xC0000005）—— 先关掉本地大模型/浏览器，或停掉一支训练★★"
        elif ram < 5.0:
            warn = "   （偏低，写 checkpoint 时容易失败）"
        lines.append(f"可用内存 {ram:.1f} GB{warn}")
    if hidden:
        lines.append(f"（另有 {hidden} 个不活跃的实验目录没显示，加 --all 可看全）")
    lines.append("曲线图：.venv-piper\\Scripts\\tensorboard.exe --logdir data\\piper → http://localhost:6006")
    return lines


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def need_piper_venv() -> bool:
    """当前解释器能不能读 tfevents。"""
    try:
        import tensorboard  # noqa: F401,PLC0415

        return False
    except ImportError:
        return True


def reexec_with_piper_venv(args: argparse.Namespace) -> None:
    """★读 tfevents 要 tensorboard，它只在 `.venv-piper` 里★ → 换解释器重跑自己。

    用 `os.execv` 直接替换进程（不套一层父进程），Ctrl+C 才能原样传到看板脚本上。
    """
    if not need_piper_venv():
        return
    if args.no_reexec or not PIPER_VENV_PY.is_file():
        print("★ 当前解释器没有 tensorboard，读不到 tfevents（进度只能靠 checkpoint 猜）。\n"
              f"  想看到 loss/步数就用：{PIPER_VENV_PY} {Path(__file__)}", flush=True)
        return
    sys.stdout.flush()
    os.execv(str(PIPER_VENV_PY), [str(PIPER_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])


def select_arms(all_dirs: list[Path], procs: list[dict], wanted: str, recent_hours: float,
                show_all: bool) -> tuple[list[Path], int]:
    """挑出要显示的臂：命令行点名的一律显示，其余只显示「最近动过」的。"""
    names = {x.strip().upper() for x in wanted.split(",") if x.strip()}
    if names:
        # 点名看哪几支时不再嘀咕「还有别的目录没显示」—— 那是用户明确不要看的。
        picked = [d for d in all_dirs if d.name.upper().replace("EXP_", "") in names]
        return sorted(picked), 0
    picked, hidden = [], 0
    for out_dir in all_dirs:
        newest = newest_checkpoint(out_dir)
        stamps = [newest.stat().st_mtime] if newest else []
        stamps += [v.stat().st_mtime for v in (out_dir / "lightning_logs").glob("version_*")]
        stamp = max(stamps) if stamps else 0.0
        fresh = (time.time() - stamp) <= recent_hours * 3600
        if show_all or fresh or match_process(procs, out_dir):
            picked.append(out_dir)
        else:
            hidden += 1
    return sorted(picked), hidden


def main() -> int:
    ap = argparse.ArgumentParser(description="Piper 微调进度看板（终端实时刷新）")
    ap.add_argument("-i", "--interval", type=int, default=15, help="刷新间隔秒数（默认 15）")
    ap.add_argument("--once", action="store_true", help="只打印一次就退出")
    ap.add_argument("--arms", default="", help="只看这几支，例如 --arms A,B（对应 exp_A/exp_B）")
    ap.add_argument("--all", action="store_true", help="连不活跃的实验目录一起显示")
    ap.add_argument("--recent-hours", type=float, default=3.0,
                    help="不点名时，只显示最近这么多小时动过的实验目录（默认 3）")
    ap.add_argument("--no-clear", action="store_true", help="不清屏（适合重定向到文件）")
    ap.add_argument("--no-reexec", action="store_true", help="不要自动换 .venv-piper 的解释器")
    args = ap.parse_args()

    reexec_with_piper_venv(args)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 免得中文在某些终端炸掉
    except Exception:  # noqa: BLE001
        pass

    all_dirs = sorted(p for p in ROOT.glob(ARM_GLOB) if (p / "lightning_logs").is_dir())
    if not all_dirs:
        print(f"没找到任何实验目录（找的是 {ARM_GLOB}）—— 训练还没启动？")
        return 1

    while True:
        procs = read_python_processes()
        picked, hidden = select_arms(all_dirs, procs, args.arms, args.recent_hours, args.all)
        infos = [collect_arm(d, procs) for d in picked]
        text = "\n".join(render(infos, hidden, args.interval))
        if not args.no_clear and not args.once:
            sys.stdout.write("\x1b[2J\x1b[H")  # 清屏+光标归位（Windows Terminal 认这个）
        print(text, flush=True)
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n（已退出看板；训练不受影响，它跑在别的进程里）")
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
