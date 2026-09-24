"""清理早期版本写坏的数据（备忘「录吗？」、事件标题「我」「定一个的闹钟」这类）。

    python scripts/clean_junk_data.py            # 只看看，不动文件
    python scripts/clean_junk_data.py --apply    # 真的删掉

只会删「明显是解析 bug 产物」的条目，正常数据一律不动。
两种文件形状都认：`memos.json` 是 `{"items": [...]}`，`events.json` 是**裸数组**。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.nlp_time import parse_datetime  # noqa: E402  (只为确保依赖可用)

# 早期 bug 留下的垃圾内容
_MEMO_JUNK = re.compile(r"^(录吗|录嘛|录吧|吗|呢|录|吗？|录吗？)[?？]?$")
_ALARM_JUNK = re.compile(
    r"^(我|你|他|一下|一个|的|了|定一个的闹钟|定一个闹钟|闹钟|提醒|时间|"
    r"定一个的|设一个的)[?？]?$"
)


def _load(p: Path):
    """读整个文件（备忘是 dict，事件表是 list）；没有就返回 None。"""
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def main() -> int:
    apply = "--apply" in sys.argv
    root = ROOT / "data"
    total = 0

    # 名字 / 看哪个字段 / 垃圾模式 / 标题 / 形状（items = {"items": [...]}，list = 裸数组）
    for name, key, pattern, label, shape in (
        ("memos.json", "content", _MEMO_JUNK, "备忘", "items"),
        ("events.json", "title", _ALARM_JUNK, "事件", "list"),
    ):
        p = root / name
        data = _load(p)
        items = [] if data is None else (data.get("items", []) if shape == "items" else data)
        keep, drop = [], []
        for it in items:
            value = str(it.get(key, "")).strip()
            (drop if pattern.match(value) else keep).append(it)
        print(f"\n{name}：共 {len(items)} 条，其中 {len(drop)} 条是垃圾")
        for it in drop:
            print(f"    删除 → {it.get(key)!r}  (id={it.get('id')}, 建于 {it.get('created_at')})")
        total += len(drop)
        if drop and apply:
            if shape == "items":
                data["items"] = keep
            else:
                data = keep              # 裸数组：整份换成留下的那些
            _save(p, data)
            print(f"    已写回，剩余 {len(keep)} 条")

    print()
    if total and not apply:
        print(f"共发现 {total} 条垃圾。加上 --apply 才会真的删除。")
    elif total:
        print(f"已清理 {total} 条。")
    else:
        print("没有发现垃圾数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
