"""把 alarms.json + schedule.json 合成一份 events.json。

    python scripts/migrate_events.py              # 只看会发生什么（默认试运行）
    python scripts/migrate_events.py --apply      # 真写（旧文件原样保留，另存 .bak 一份）
    python scripts/migrate_events.py --apply --force   # events.json 已有内容也覆盖

为什么默认试运行：这两个文件是**用户真实的提醒和课表**，写错了没法撤销。
所以流程是「先打印完整转换结果 → 你确认 → 再落盘」，落盘时还会：

1. 把旧文件各备份一份 ``.bak``（已在 .gitignore 里）；
2. 旧文件**不删**（过渡期两边都在，出问题可以直接拷回去）；
3. 写完立刻读回来核对一遍（条数、fired、skipped、链），不一致就报错并**回滚**。

映射规则（新 schema 本来就是旧 schedule 的超集，而且**连类型都没有**）：

    alarms.json   {when, what, fired}          → {title, start, remind_before: [0]}   （无 repeat = 只响一次）
    schedule.json {title, kind, _fired, skip}  → {title, category: <原 kind>, state: {fired, skipped}}

★闹钟和日程不再是两个类型★：一张可填可不填的表，说了重复/时长/提前量就填上，
没说就用缺省（``duration_minutes=0``、无 ``repeat``、``remind_before=[0]``）。
旧文件里的 ``kind`` 只剩日程那个能当**标签**用（course/meeting/task）。
详见 voice_loop/events.py 的模块说明。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import events as ev  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402


def _load_raw(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! 读不了 {path.name}：{exc}")
        return []
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    return [x for x in raw if isinstance(x, dict)]


def _fmt(item: dict) -> str:
    st = item.get("state") or {}
    tags = [str(item.get("category") or ""), ev.repeat_text(item)]
    lead = ev.leads_of(item)
    tags.append("准时" if lead == [0] else "提前" + ",".join(str(x) for x in lead))
    bits = [f"#{item.get('id')}", f"{'/'.join(t for t in tags if t):<16}",
            str(item.get("start"))[:16], str(ev.display_title(item))[:18]]
    if st.get("fired"):
        bits.append(f"fired×{len(st['fired'])}")
    if st.get("skipped"):
        bits.append(f"skip×{len(st['skipped'])}")
    if item.get("chain"):
        bits.append(f"chain←{item['chain'].get('after')}")
    return "  ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="alarms + schedule → events（统一事件表）")
    ap.add_argument("--apply", action="store_true", help="真写；不加只会打印会写什么")
    ap.add_argument("--force", action="store_true", help="events.json 已有内容也覆盖")
    ap.add_argument("--target", default="", help="目标文件（默认用 config.toml 的 [skills] event_file）")
    args = ap.parse_args()

    settings = load_settings()
    cfg = settings.skills
    alarm_file = settings.resolve(cfg.alarm_file)
    schedule_file = settings.resolve(cfg.schedule_file)
    target = settings.resolve(args.target) if args.target else settings.resolve(cfg.event_file)

    print("数据源：")
    for label, path in (("闹钟", alarm_file), ("日程", schedule_file)):
        n = len(_load_raw(path))
        print(f"  {label:<4} {path}  （{n} 条{'，文件不存在' if not path.exists() else ''}）")
    print(f"目标  ： {target}")

    items, stats = ev.migrate_legacy(alarm_file, schedule_file)
    if not items:
        print("\n两个旧文件里都没有条目，没什么要迁的。")
        return 0

    print(f"\n转换结果（{len(items)} 条：闹钟 {stats['alarm']} / 日程 {stats['schedule']}；"
          f"已播报记录 {stats['fired']} 条、跳过 {stats['skipped']} 条）")
    for item in items:
        print(f"  {_fmt(item)}")

    if not args.apply:
        print("\n（试运行。确认上面没问题就加 --apply 落盘；旧文件会另存 .bak，且不会删。）")
        return 0

    if target.exists() and _load_raw(target) and not args.force:
        print(f"\n× {target.name} 里已经有 {len(_load_raw(target))} 条，没有 --force 不覆盖。")
        return 1

    for src in (alarm_file, schedule_file):
        if src.exists():
            backup = src.with_suffix(src.suffix + ".bak")
            shutil.copy2(src, backup)
            print(f"\n已备份 {src.name} → {backup.name}")

    target.parent.mkdir(parents=True, exist_ok=True)
    ev.EventStore(target, default_lead=cfg.default_remind_before).save(items)
    print(f"已写入 {target}（{len(items)} 条）")

    # 落盘后立刻读回来核对——迁移最怕「看着写了、其实丢了」
    back = ev.EventStore(target)
    check = back.load()
    problems: list[str] = []
    if len(check) != len(items):
        problems.append(f"条数对不上：{len(check)} != {len(items)}")
    fired = sum(len((it.get("state") or {}).get("fired") or []) for it in check)
    skipped = sum(len((it.get("state") or {}).get("skipped") or []) for it in check)
    if fired != stats["fired"]:
        problems.append(f"已播报记录丢了：{fired} != {stats['fired']}")
    if skipped != stats["skipped"]:
        problems.append(f"跳过记录丢了：{skipped} != {stats['skipped']}")
    if len({it.get("id") for it in check}) != len(check):
        problems.append("id 有重复")

    if problems:
        print("\n× 核对失败：" + "；".join(problems))
        print("  旧文件一个字没动，可以放心排查。")
        return 1
    print(f"核对通过：{len(check)} 条、fired×{fired}、skipped×{skipped}、id 无重复。")
    print("\n旧的两个文件仍在原地（没删）。确认一切正常后可以自己删，或留着当备份。")
    print("还要代码切到它上面才会真正生效——见 docs/ENGINEERING_LOG.md 第 17 节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
