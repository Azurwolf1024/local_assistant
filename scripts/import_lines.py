"""把「名字 + 文本」清单导入到 json 的某个数组字段里（默认角色文件的 ``lines``）。

★只添加，不替换★：已有的内容一个字都不动、也不擦；已经存在的同文本条目会跳过。
默认先试运行（dry-run），确认了再加 ``--apply``。

    # 只看会发生什么（默认）
    python scripts/import_lines.py data/personas/kaltsit/kaltsit.txt

    # 真的写进去（目标 json 自动找：同目录 <名字>.json -> 上一级 <名字>.json -> 索引里对应的 file）
    python scripts/import_lines.py data/personas/kaltsit/kaltsit.txt --apply

    # 明确指定目标与场景名，并过滤掉作战类、超长的台词
    python scripts/import_lines.py 我的台词.txt --json data/personas/amiya.json --apply \
        --scene 随意对话 --skip 作战 --skip 部署 --max-chars 40

导入的每条都是 ``{"scene": 清单里的名字, "text": 清单里那段文本}``（``--scene`` 给了就
统一用它）。清单格式见 :mod:`voice_loop.manifest`：一行名字、空行、一段文本。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.manifest import parse_manifest_file  # noqa: E402

DEFAULT_KEY = "lines"


def _norm(text: str) -> str:
    """比较「是不是同一条」用的归一化：去掉所有空白与标点。"""
    return re.sub(r"[\s\u3000]+", "", text).strip()


def find_target_json(txt: Path) -> tuple[Path | None, list[Path]]:
    """按约定找目标 json，返回 ``(选中的, 候选列表)``。

    顺序：① 同目录 ``<名字>.json`` ② 上一级 ``<名字>.json``
         ③ 索引 ``data/characters.json`` 里指向的人格文件
    """
    candidates: list[Path] = []
    here = txt.parent / f"{txt.stem}.json"
    if here.is_file():
        return here, [here]
    candidates.append(here)

    up = txt.parent.parent / f"{txt.stem}.json"
    if up.is_file():
        return up, [up]
    candidates.append(up)

    index = ROOT / "data" / "characters.json"
    if index.is_file():
        try:
            raw = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        for entry in raw.get("characters", []) if isinstance(raw, dict) else []:
            if isinstance(entry, str):
                rid, rel = Path(entry).stem, entry
            elif isinstance(entry, dict):
                rid = str(entry.get("id") or Path(str(entry.get("file") or "")).stem)
                rel = str(entry.get("file") or "")
            else:
                continue
            if not rel:
                continue
            path = (index.parent / rel).resolve()
            candidates.append(path)
            if txt.stem == rid or txt.stem == path.stem:
                return path, candidates
    return None, candidates


def load_json(path: Path) -> dict:
    """读目标 json；不合法就抛 ValueError（调用方转成退出码 2，不半途写坏文件）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"不是合法 json（{exc}）") from None
    if not isinstance(raw, dict):
        raise ValueError("顶层不是对象，不敢动它")
    return raw


def _prompt_len(data: dict) -> int | None:
    """用角色渲染器量一下提示词长度（只对角色文件有意义）。"""
    try:
        from voice_loop.persona import Character, render_system_prompt
    except Exception:  # pragma: no cover
        return None
    try:
        return len(render_system_prompt(Character.from_dict(data)))
    except Exception:  # pragma: no cover
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把「名字 + 文本」清单导入 json 的数组字段（只添加，不替换）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("txt", help="清单文件，如 data/personas/kaltsit/kaltsit.txt")
    ap.add_argument("--json", default="", help="目标 json；不给就按约定自动找")
    ap.add_argument("--key", default=DEFAULT_KEY, help=f"写进哪个数组字段（默认 {DEFAULT_KEY}）")
    ap.add_argument(
        "--as",
        dest="as_kind",
        choices=("line", "text"),
        default="line",
        help="line = 写入 {scene, text}（默认，适合 lines）；text = 只写正文字符串（适合 style 这类）",
    )
    ap.add_argument("--scene", default="", help="统一用这个场景名（不给就用清单里的名字）")
    ap.add_argument(
        "--plain",
        action="store_true",
        help="整份文件都当正文（每段一条），不把短行当名字——适合「就是一堆段落」的文本",
    )
    ap.add_argument("--skip", action="append", default=[], help="跳过名字里含这个词的条目（可多次）")
    ap.add_argument("--max-chars", type=int, default=0, help="跳过超过这个字数的文本（0 = 不限）")
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认只试运行）")
    ap.add_argument("--show", action="store_true", help="把解析出的每条（名字 + 文本开头）都打出来")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    args = ap.parse_args()

    txt = Path(args.txt)
    if not txt.is_absolute():
        txt = (Path.cwd() / txt).resolve()
    if not txt.is_file():
        print(f"× 找不到清单文件：{txt}", file=sys.stderr)
        return 2

    if args.json:
        target = Path(args.json)
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        if not target.is_file():
            print(f"× 找不到目标 json：{target}", file=sys.stderr)
            return 2
    else:
        target, candidates = find_target_json(txt)
        if target is None:
            print("× 自动找不到目标 json，请用 --json 指定。找过这些位置：", file=sys.stderr)
            for c in candidates:
                print(f"    {c}", file=sys.stderr)
            return 2

    entries = parse_manifest_file(txt, plain=args.plain)
    if not entries:
        print(f"× {txt.name} 里没解析出任何条目（格式：一行名字、接着一段正文）", file=sys.stderr)
        return 2

    try:
        data = load_json(target)
    except ValueError as exc:
        print(f"× {target} {exc}", file=sys.stderr)
        return 2
    items = data.get(args.key)
    if items is None:
        items = []
    if not isinstance(items, list):
        print(f"× {target.name} 的 {args.key} 不是数组，不敢动它", file=sys.stderr)
        return 2

    have = {
        _norm(str(x.get("text", ""))) if isinstance(x, dict) else _norm(str(x))
        for x in items
    }
    plain = args.as_kind == "text"
    known_scenes = {
        str(x.get("scene", "")) for x in items if isinstance(x, dict) and x.get("scene")
    }

    added: list[dict] = []
    dup: list[str] = []
    skipped: list[str] = []
    for name, body in entries:
        scene = args.scene.strip() or name or txt.stem
        if any(word and word in scene for word in args.skip):
            skipped.append(f"{scene}（--skip 命中）")
            continue
        if args.max_chars and len(body) > args.max_chars:
            skipped.append(f"{scene}（{len(body)} 字 > {args.max_chars}）")
            continue
        if _norm(body) in have:
            dup.append(scene)
            continue
        added.append(body if plain else {"scene": scene, "text": body})
        have.add(_norm(body))

    before = len(items)
    after = before + len(added)
    print(f"清单：{txt}")
    print(f"目标：{target}  ·  字段：{args.key}（现有 {before} 条）")
    print(f"解析出 {len(entries)} 条 → 新增 {len(added)} 条，跳过 {len(dup) + len(skipped)} 条")
    if dup:
        print(f"  已是同文本（不动）：{'、'.join(dup)}")
    if skipped:
        print(f"  按规则跳过：{'、'.join(skipped)}")
    if added:
        print(f"  将追加：{'、'.join(str(x if plain else x['scene']) for x in added)}")
    if args.show:
        print("  解析明细：")
        for name, body in entries:
            print(f"    {name or '(无名)':14s} → {body[:32]}{'…' if len(body) > 32 else ''}")

    if not added:
        print("\n没有可新增的内容，文件保持原样。")
        return 0

    # 提示词长度影响（只有角色文件量得出来）
    merged = dict(data)
    merged[args.key] = items + added
    old_len, new_len = _prompt_len(data), _prompt_len(merged)
    if old_len is not None and new_len is not None:
        print(f"\n角色提示词：{old_len} 字 → {new_len} 字（+{new_len - old_len}）")
        longest = max((len(x) if isinstance(x, str) else len(x["text"]) for x in added), default=0)
        if longest > 40 or new_len - old_len > 600:
            print(
                "  ★注意★提示词变长了，首字会稍慢。但「台词多→回答变长篇」这个担心实测**不成立**：\n"
                "  38 条凯尔希台词（+1221 字）反而让回答变短（平均 54.8 字 vs 78.7 字），也没复读。\n"
                "  真觉得走形就用 --skip / --max-chars 重挑一遍，或回滚 .bak。"
            )

    if not args.apply:
        print("\n（试运行，没有写文件；确认无误后加 --apply）")
        return 0

    if not args.no_backup:
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copyfile(target, backup)
        print(f"\n已备份：{backup.name}")

    data[args.key] = items + added
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    print(f"已写入：{target}（{args.key}：{before} → {after} 条）")
    print(f"想回滚：copy {target.name}.bak {target.name}")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
