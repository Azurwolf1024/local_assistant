"""人格体检：查那些**会让模型出戏/编造**的写法。

    python scripts/lint_persona.py                 # 所有角色（有问题退出码 1）
    python scripts/lint_persona.py --who kaltsit   # 只看一个
    python scripts/lint_persona.py --quiet         # 只打印有问题的角色

查的都是**实测会导致具体症状**的写法（见 docs/ENGINEERING_LOG.md §58）：

=========================================  ==========================================
检查项                                      会出什么事
=========================================  ==========================================
``可照抄示例``  style/背景里有「比如「完整的一句话」」  4B 模型会把那句话**原样搬**到
                                            毫不相干的回合里（「白天说『你又熬夜了』」）
``作息话题``    style/背景/规矩里出现作息、熬夜、     模型把这些当成**待办话题**，每隔几轮
                睡、身体、累这类词                   就挑一个来「关心」你
``提示词预算``  渲染后超过 ``PROMPT_TOKEN_BUDGET``   加上历史/工具提示后越过 ``num_ctx``，
                                            溢出会**从头截断** → 人设整段丢失
``台词重复``    两条台词正文一样                     白占预算``装饰符号``    style/背景里有 ★ /** /【】            4B 当成「格式要求」，回答里开始冒星号方括号
``长度``        渲染后超 ``COMPACT_MAX_CHARS``         提示词一长，模型就把人设当「一份待分析的文档」``作品名``      有 worlds 却没写 work（或反过来）     提示词第一行的「你是《X》里的…」
=========================================  ==========================================
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import persona as P  # noqa: E402

# 「可照抄示例」：比如/例如 + 引号里的完整句子（跟 persona._COPYABLE 同一份判断）
COPYABLE = P._COPYABLE  # noqa: SLF001 - 体检就是要用渲染时同一份规则

# 容易变成「话题清单」的词（用来关心人的那种）
ROUTINE_WORDS = ("作息", "熬夜", "早睡", "晚睡", "睡觉", "身体", "累不累", "太累",
                 "注意身体", "休息", "摸鱼")
ROUTINE_OK = ("不要", "别", "不用", "避免")     # 写了「不要」的是**禁令**，不算诱惑
"""同一个句子里有这些词时，说明是「别这么做」，不是在邀请它提。"""

DECOR = ("★", "☆", "**", "【", "】")
"""手写人格文件里常见的装饰符号（渲染时会洗掉，但写了就说明原本是当文档写的）。"""

COMPACT_MAX_CHARS = 420
"""compact 提示词的字符上限。超过这个量，4B 开始把提示词当文档议论（§58 实测）。"""


def check_char(char: P.Character, *, lines_in_prompt: int = 0) -> list[tuple[str, str]]:
    """返回 ``[(级别, 说明)]``；级别是 ``warn`` / ``info``。"""
    out: list[tuple[str, str]] = []

    for field_name, items in (("style", char.style), ("background", [char.background]),
                              ("rules", char.rules)):
        for raw in items:
            text = str(raw or "")
            if not text:
                continue
            got = COPYABLE.search(text)
            if got:
                out.append(("warn", f"{field_name} 里有可照抄的示例句：「{got.group(1)}」"
                                    "（会被原样搬到别的回合里，改成描述而不是例句）"))
            if any(d in text for d in DECOR):
                out.append(("warn", f"{field_name} 里有装饰符号：「{text[:34]}」"
                                    "（★/**/【】会让模型把它当格式要求，回答里跟着冒符号）"))
            if any(w in text for w in ROUTINE_WORDS) and not any(w in text for w in ROUTINE_OK):
                out.append(("warn", f"{field_name} 里提到作息/身体这类话题：「{text[:34]}」"
                                    "（模型会把它当成要主动提的话题）"))

    stats = P.prompt_stats(char, lines_in_prompt=lines_in_prompt)
    if stats["over_budget"]:
        out.append(("warn", f"提示词 ~{stats['tokens']} tokens，超过建议上限 "
                            f"{P.PROMPT_TOKEN_BUDGET}（台词 {stats['lines_in_prompt']}/"
                            f"{stats['lines_total']} 条）。"
                            "调小 [persona] lines_in_prompt，或把背景/风格写短些"))
    if stats["chars"] > COMPACT_MAX_CHARS:
        out.append(("warn", f"提示词 {stats['chars']} 字，超过 compact 建议的 "
                            f"{COMPACT_MAX_CHARS} 字（太长了 4B 会开始议论提示词）"))
    rendered = P.render_system_prompt(char, lines_in_prompt=lines_in_prompt)
    if "学那个语气" in rendered or "第一人称" in rendered:      # 护栏：元指令不该出现在 compact
        out.append(("warn", "compact 提示词里混进了元指令（应该只有陈述句）"))
    seen: dict[str, int] = {}
    for index, line in enumerate(char.lines, 1):
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        if text in seen:
            out.append(("warn", f"第 {index} 条台词和第 {seen[text]} 条一模一样"))
        else:
            seen[text] = index
    if not char.work and char.worlds:
        out.append(("info", f"没写 work → 提示词用 worlds[0]「{char.worlds[0]}」当作品名"))
    if char.work and not char.worlds:
        out.append(("info", f"写了 work「{char.work}」但没挂 worlds（知识库不会共享该世界观）"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="人格体检（查会让模型出戏/编造的写法）")
    ap.add_argument("--who", default="", help="只看这个角色（id 或名字）")
    ap.add_argument("--quiet", action="store_true", help="只打印有问题的角色")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    index = json.loads((ROOT / "data" / "characters.json").read_text(encoding="utf-8"))
    files: list[Path] = []
    for entry in index.get("characters") or []:
        rel = entry if isinstance(entry, str) else str(entry.get("file") or "")
        cid = "" if isinstance(entry, str) else str(entry.get("id") or "")
        path = (index and ROOT / "data" / rel) if rel else None
        if path and path.is_file() and (not args.who
                                        or args.who.lower() in (cid.lower(), path.stem.lower())):
            files.append(path)
    if not files:
        print(f"× 没找到角色（--who {args.who!r}），看 data/characters.json")
        return 2

    report: dict[str, list] = {}
    bad = 0
    for path in files:
        char = P.Character.from_dict(json.loads(path.read_text(encoding="utf-8")))
        found = check_char(char)
        warnings = [msg for level, msg in found if level == "warn"]
        report[char.id] = [{"level": lv, "text": msg} for lv, msg in found]
        if warnings:
            bad += 1
        stats = P.prompt_stats(char)
        head = (f"{char.name}（{char.id}）· 提示词 ~{stats['tokens']} tokens · "
                f"台词 {stats['lines_in_prompt']}/{stats['lines_total']} · "
                f"作品《{stats['work']}》" if stats["work"] else
                f"{char.name}（{char.id}）· 提示词 ~{stats['tokens']} tokens · "
                f"台词 {stats['lines_in_prompt']}/{stats['lines_total']} · 无作品名")
        if not warnings and args.quiet:
            continue
        print("\n" + head)
        if not found:
            print("  √ 没发现问题（示例句、作息话题、预算、台词重复都干净）")
        for level, msg in found:
            print(f"  {'×' if level == 'warn' else '·'} {msg}")

    if args.json:
        io.open(ROOT / "sessions" / "persona_lint.json", "w", encoding="utf-8").write(
            json.dumps(report, ensure_ascii=False, indent=2))
    print("\n" + "=" * 70)
    print(f" 结论：{len(files)} 个角色，{bad} 个要处理"
          if bad else f" 结论：{len(files)} 个角色都干净")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
