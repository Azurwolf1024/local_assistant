"""批量导入角色素材：角色文件夹里的 txt → 人格文件的台词 + 知识库的档案。

    python scripts/import_persona.py                      # 扫描 + 试运行（什么都不写）
    python scripts/import_persona.py --apply              # 真导入（写前自动 .bak）
    python scripts/import_persona.py --only clementia --apply
    python scripts/import_persona.py --create --add-index --only clementia --apply
    python scripts/import_persona.py --world 明日方舟 --profile-only --apply

它认两个东西（判断见 :mod:`voice_loop.persona_import`，★不看具体游戏的模板★）：

    ``<角色>.txt``        台词清单（一行名字 + 一段正文）→ 人格文件的 ``lines``
    ``profile.txt``       角色档案 / 角色卡 / 设定       → ``data/knowledge/<id>/档案.md``

三条规矩：**只添加不替换**、**默认试运行**、**幂等**（同一份导两次，第二次什么都不做）。
细节与「为什么不按固定模板解析」见 ``voice_loop/persona_import.py`` 的模块说明。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import persona_import as pi  # noqa: E402
from voice_loop.paths import app_root  # noqa: E402


def show_source(src: pi.Source, root: Path) -> str:
    kind = "档案" if src.kind == "profile" else "台词"
    unit = "个章节" if src.kind == "profile" else "条"
    return f"{src.rel(root)}　→　{kind}（{src.entries} {unit}）　·　{src.reason}"


def render(result: pi.CharResult, root: Path) -> None:
    head = f"{result.name or result.char_id}（{result.char_id}）"
    if not result.ok:
        print(f"  × {head}：{'; '.join(result.problems)}")
        return
    mark = "已导入" if result.apply else "将导入"
    parts: list[str] = []
    if result.lines_added:
        parts.append(f"台词 +{result.lines_added}")
    if result.lines_dup:
        parts.append(f"跳过已有的 {result.lines_dup}")
    if result.created_json:
        parts.append("★生成了人格文件骨架★")
    if result.index_added:
        parts.append("已加进 characters.json")
    if result.worlds_added:
        parts.append(f"世界观 +{'、'.join(result.worlds_added)}")
    print(f"  √ {head}：{mark} {' · '.join(parts) if parts else '（没有新内容）'}")

    if result.profile_path is not None:
        state = "已写入" if (result.profile_changed and result.apply) else (
            "内容没变" if not result.profile_changed else "将写入")
        try:
            rel = result.profile_path.relative_to(root).as_posix()
        except ValueError:
            rel = str(result.profile_path)
        print(f"      档案 → {rel}（{result.profile_sections} 个章节 · "
              f"{result.profile_meta} 条元信息）：{state}")
    if result.prompt_before is not None and result.prompt_after is not None:
        delta = result.prompt_after - result.prompt_before
        print(f"      角色提示词：{result.prompt_before} 字 → {result.prompt_after} 字"
              f"（{delta:+d}）")
    if result.dropped:
        sample = "、".join(result.dropped[:3])
        print(f"      丢掉了 {len(result.dropped)} 行「解锁门槛」（如 {sample}）"
              f"；要留着用 --keep-gates")
    for note in result.notes:
        print(f"      · {note}")
    if result.backups:
        print(f"      备份：{'、'.join(result.backups)}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量导入角色素材（台词 → lines；档案 → 知识库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default="", help="项目根（默认自动找）")
    ap.add_argument("--only", nargs="*", default=[],
                    help="只处理这些角色 id（默认全部；也可写目录名）")
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认只试运行）")
    ap.add_argument("--create", action="store_true",
                    help="★给还没有人格文件的角色生成骨架★（之后去控制台补风格那几栏）")
    ap.add_argument("--add-index", action="store_true",
                    help="配合 --create：顺手把它加进 characters.json（加进去才会被唤醒）")
    ap.add_argument("--lines-only", action="store_true", help="只导台词")
    ap.add_argument("--profile-only", action="store_true", help="只导档案")
    ap.add_argument("--world", default="",
                    help="把档案放进世界观组 data/knowledge/_worlds/<名字>/（同 IP 的角色共享）")
    ap.add_argument("--profile-name", default="", help=f"档案文件名（默认 {pi.DEFAULT_PROFILE_FILE}）")
    ap.add_argument("--keep-gates", action="store_true", help="保留「提升信赖至…以查看」这类门槛行")
    ap.add_argument("--as", dest="assume", default="", choices=("", "lines", "profile"),
                    help="强制把所有 txt 当成这一类（判断错了时用）")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else app_root()
    plans = pi.scan(root, only=args.only, assume=args.assume)
    if not plans:
        print(f"× 在 {pi.personas_dir(root)} 下没找到角色目录"
              + (f"（--only 指定了 {args.only}）" if args.only else ""))
        return 2

    print("=" * 72)
    print(f" 角色素材导入　{'(真写)' if args.apply else '(试运行：什么都不写)'}"
          f"　·　项目根 {root}")
    print("=" * 72)

    results: list[pi.CharResult] = []
    skipped: list[str] = []
    for plan in plans:
        if not plan.has_work:
            skipped.append(plan.char_id)
            continue
        name = plan.name or plan.char_id
        print(f"\n[{plan.char_id}] {name}"
              f"{'' if plan.json_exists else '　★还没有人格文件★'}"
              f"{'' if plan.in_index else '　（不在索引里）'}")
        for src in plan.sources:
            print("  · " + show_source(src, root))
        result = pi.import_char(
            root, plan, apply=args.apply,
            lines=not args.profile_only, profile=not args.lines_only,
            create=args.create, add_index=args.add_index, world=args.world.strip(),
            keep_gates=args.keep_gates, profile_name=args.profile_name.strip(),
            assume=args.assume,
        )
        render(result, root)
        results.append(result)

    if skipped:
        print(f"\n（这些没有可导入的素材，跳过：{'、'.join(skipped)}）")

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    print("\n" + "=" * 72)
    print(f" 结论：{len(ok)} 个角色可以导入，{len(bad)} 个要处理")
    if not args.apply:
        print(" 这是试运行 —— 确认无误后加 --apply")
    elif ok:
        print(" 下一步：")
        print("   · 想听声音：python scripts/pick_voice_ref.py --who <id>（挑参考音频）")
        print("   · 改人设：python main.py ui --window auto（控制台「角色」标签页，点卡片就能改）")
        print("   · 试一句：python main.py ask \"你好\" --character <id>")
    for result in bad:
        print(f"   × {result.char_id}：{'; '.join(result.problems)}")
    print("=" * 72)
    return 0 if not bad else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
