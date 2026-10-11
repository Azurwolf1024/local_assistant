"""角色素材导入的自测：``voice_loop/persona_import.py`` + ``scripts/import_persona.py``。

★全程在临时目录里★：只有第 [9] 节会碰真实仓库，而且它只**读**、只核对「没被动过」。

要钉住的是「适配不同模板」这件事本身，所以这里用**两套完全不同的档案排版**
（中文方括号元信息 + 门禁行；Markdown 标题 + 英文字段）各走一遍，另外加一份纯散文。
还有两个真踩过的坑，各有一条回归：

    ★坑一★ 算完合并却忘了写回 ``data`` → 报告说「台词 +38」而文件里 0 条；
    ★坑二★ 正文缓冲区跨章节没结算 → 第一节的字跑到最后一节去（串味）。

    python scripts/test_persona_import.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import persona_import as pi  # noqa: E402
from voice_loop.manifest import manifest_pairs  # noqa: E402

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


# --------------------------------------------------------------------------- #
# 两套「不是同一个游戏」的档案模板
# --------------------------------------------------------------------------- #
PROFILE_A = """基础档案

初始开放
【代号】克莱门莎
【性别】女
【出身地】阿戈尔
【身高】168cm

综合体检测试

提升信赖至25%以查看
【物理强度】标准
【战场机动】优良

客观履历

提升信赖至50%以查看
克莱门莎，阿戈尔荣誉军团长，现以合作姿态协助罗德岛开展工作。

档案资料一

提升信赖至100%以查看
【一份对话记录】
“为什么刻在剑上？”
“用来提醒我自己。”
"""

PROFILE_B = """# Neon Wanderer — Character Sheet

## Identity
- Name: Nova Kestrel
- Age: 27
- Origin: Orbital Ring 3

## Appearance
Silver hair, one cybernetic eye. Wears a battered flight jacket.

## Personality
Blunt, dry-humoured, allergic to ceremony.

## Backstory
Nova grew up in the maintenance shafts of Ring 3 and learned to fly before she could read.
"""

PROFILE_C = """这是一段没有任何标题、也没有字段的散文。它就只是一段设定描述，中间连着写下来，
甚至没有空行分隔成小节，讲的是某个人物和某个世界的关系——这种手写的角色卡也得能进来。
虽然它没有结构，但它足够长，所以应当被当成一份档案而不是台词清单。
再补几句凑够长度：她说话很慢，喜欢在句尾停顿，不太愿意谈过去，只在深夜才开口讲一点。
她最不喜欢别人替她做决定，哪怕那个决定是对的——这一点谁都不太敢提。"""


def make_project(tmp: Path, *, with_json: bool = True, with_wavs: bool = True) -> Path:
    """搭一个像真的临时项目：一个角色目录 + 台词清单 + 档案。"""
    char_dir = tmp / "data" / "personas" / "clementia"
    char_dir.mkdir(parents=True)
    (tmp / "data" / "characters.json").write_text(
        json.dumps({"default": "clementia",
                    "characters": ([{"id": "clementia", "file": "personas/clementia.json",
                                     "enabled": True}] if with_json else [])},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (char_dir / "clementia.txt").write_text(
        "任命助理\n这些基础的工作，您希望由我来做？\n\n交谈1\n和解是伊比利亚人自己的功劳。\n",
        encoding="utf-8")
    (char_dir / "profile.txt").write_text(PROFILE_A, encoding="utf-8")
    if with_wavs:
        for name in ("任命助理", "交谈1", "ref_join_B"):
            (char_dir / f"{name}.wav").write_bytes(b"RIFF")
        (char_dir / "ref_join_B.txt").write_text("短的一段参考音频逐字文本。", encoding="utf-8")
    if with_json:
        (tmp / "data" / "personas" / "clementia.json").write_text(json.dumps({
            "id": "clementia", "name": "克莱门莎", "title": "代表", "background": "我是……",
            "user_title": "博士", "wake_words": ["克莱门莎"], "style": ["温和"],
            "rules": ["不知道就说不知道"], "avoid": [], "lines": [], "worlds": [],
            "enabled": True, "notes": "测试用",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return tmp


def snapshot(root: Path) -> dict[str, str]:
    """整个项目的文件 → 内容哈希（用来证明「试运行一个字节都没写」）。"""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
def section_parse() -> None:
    print("\n[1] 档案解析：两套完全不同的模板各走一遍")
    doc = pi.parse_profile(PROFILE_A, source="profile.txt")
    titles = [s.title for s in doc.sections]
    check("中文模板：章节都认出来了", titles,
          ["基础档案", "综合体检测试", "客观履历", "一份对话记录"])
    check("元信息抽出来了", doc.meta[:4],
          [("代号", "克莱门莎"), ("性别", "女"), ("出身地", "阿戈尔"), ("身高", "168cm")])
    check("第二张表归到它自己那一节", [k for k, _ in doc.sections[1].meta],
          ["物理强度", "战场机动"])
    check("★门禁行丢掉了（不是内容）★", len(doc.dropped) >= 4, True, detail=str(doc.dropped[:3]))
    check("章节正文没有被门禁行污染", "提升信赖" not in doc.sections[2].body, True)

    keep = pi.parse_profile(PROFILE_A, keep_gates=True)
    check("--keep-gates 时留着", keep.dropped, [])
    check("留着时正文里能看到它",
          any("提升信赖至50%以查看" in s.body for s in keep.sections), True)

    doc_b = pi.parse_profile(PROFILE_B)
    check("英文模板：一级标题当成文档标题", doc_b.title, "Neon Wanderer — Character Sheet")
    titles_b = [s.title for s in doc_b.sections]
    check("英文模板：Markdown 标题也认", titles_b,
          ["Identity", "Appearance", "Personality", "Backstory"])
    check("英文模板：键值也抽成元信息", doc_b.sections[0].meta[:2],
          [("Name", "Nova Kestrel"), ("Age", "27")])
    check("英文模板：一段话的正文原样", doc_b.sections[3].body.startswith("Nova grew up"),
          True, detail=doc_b.sections[3].body[:40])

    doc_c = pi.parse_profile(PROFILE_C)
    check("纯散文：整篇当一个章节", len(doc_c.sections), 1)
    check("纯散文：正文没丢", len(doc_c.sections[0].body) > 80, True,
          detail=str(len(doc_c.sections[0].body)))

    md = pi.profile_markdown(doc, title="克莱门莎 · 档案", source_rel="x/profile.txt")
    check("生成的 markdown 有标题层级", md.count("## "), len(doc.sections))
    check("生成的 markdown 带来源", "x/profile.txt" in md, True)
    check("生成的 markdown 里门禁行没了", "以查看" not in md, True)


def section_no_bleed() -> None:
    print("\n[2] ★回归：节与节不许串味★（第一节的字跑到最后一节去过）")
    text = "第一节\n\n第一段正文。\n\n第二节\n\n第二段正文。\n\n第三节\n\n第三段正文。\n"
    doc = pi.parse_profile(text)
    check("三节", [s.title for s in doc.sections], ["第一节", "第二节", "第三节"])
    check("各节的正文各归各位", [s.body for s in doc.sections],
          ["第一段正文。", "第二段正文。", "第三段正文。"])
    # 空白行分隔的多个段落要留在同一节里（段落是检索切片的单位）
    multi = "设定\n\n第一段。\n\n第二段。\n"
    doc2 = pi.parse_profile(multi)
    check("同节多段都在", doc2.sections[0].body, "第一段。\n\n第二段。")


def section_classify(tmp: Path) -> None:
    print("\n[3] 素材识别：台词清单 / 档案 / 都不是")
    char_dir = tmp / "data" / "personas" / "clementia"
    stems = {p.stem.lower() for p in char_dir.glob("*.wav")}
    lines_text = (char_dir / "clementia.txt").read_text(encoding="utf-8")
    prof_text = (char_dir / "profile.txt").read_text(encoding="utf-8")
    ref_text = (char_dir / "ref_join_B.txt").read_text(encoding="utf-8")

    check("台词清单：名字对得上音频", pi.classify(char_dir / "clementia.txt", lines_text,
                                               wav_stems=stems)[0], "lines")
    check("档案：一条都对不上但是有结构", pi.classify(char_dir / "profile.txt", prof_text,
                                                   wav_stems=stems)[0], "profile")
    check("★和音频同名 = 那段音频的逐字文本，不是素材★",
          pi.classify(char_dir / "ref_join_B.txt", ref_text, wav_stems=stems)[0], "ignore")
    check("没有音频时：纯散文按长度也算档案",
          pi.classify(Path("x.txt"), PROFILE_C, wav_stems=set())[0], "profile")
    check("没有音频、又没有结构 → 不算素材",
          pi.classify(Path("y.txt"), "就一句话。", wav_stems=set())[0], "ignore")
    check("只有两条台词的小清单也要认（不能因为少就当成档案）",
          pi.classify(char_dir / "clementia.txt", lines_text, wav_stems=stems)[1].startswith("2 条里 2 条"),
          True)
    check("--as 强制指定能覆盖判断",
          pi.classify(char_dir / "profile.txt", prof_text, wav_stems=stems, assume="lines")[0],
          "lines")

    # ★没有音频的目录★（音频还没下、或刚建目录只有文本）：靠命名约定与形状判断
    # ——沙盒里就是这么发现「台词清单被当成档案」的
    fresh = tmp / "data" / "personas" / "silent"
    fresh.mkdir(parents=True)
    (fresh / "silent.txt").write_text("开场\n你好。\n\n结束\n再见。\n", encoding="utf-8")
    (fresh / "extra.txt").write_text("开场\n你好。\n\n结束\n再见。\n", encoding="utf-8")
    (fresh / "profile.txt").write_text(PROFILE_A, encoding="utf-8")
    kinds = {s.path.name: s.kind for s in pi.scan_dir(fresh)}
    check("没音频时：和目录同名的当台词清单", kinds.get("silent.txt"), "lines")
    check("没音频时：不改名的「短标签+一段」也当台词清单", kinds.get("extra.txt"), "lines")
    check("没音频时：档案还是档案", kinds.get("profile.txt"), "profile")

    sources = pi.scan_dir(char_dir)
    check("扫描只留下两种素材（ref_join_B 被跳过）",
          sorted((s.path.name, s.kind) for s in sources),
          [("clementia.txt", "lines"), ("profile.txt", "profile")])


def section_manifest_skip(tmp: Path) -> None:
    print("\n[4] ★回归：profile.txt 不许被当成台词清单★（同名会喂错参考文本）")
    char_dir = tmp / "data" / "personas" / "clementia"
    pairs = manifest_pairs(char_dir)
    check("清单里只有台词", sorted(pairs), ["交谈1", "任命助理"])
    check("档案的小标题没混进来",
          [k for k in pairs if k in ("基础档案", "客观履历", "档案资料一")], [])
    check("同名的 ref_join_B.txt 也没混进来", "ref_join_b" in pairs, False)

    real = ROOT / "data" / "personas" / "kaltsit"
    real_pairs = manifest_pairs(real)
    check("真实角色目录：ref_join_* 不再污染清单",
          [k for k in real_pairs if k.startswith("ref_")], [])
    check("真实角色目录：台词还在（38 条上下）", len(real_pairs) >= 30, True,
          detail=str(len(real_pairs)))


def section_merge() -> None:
    print("\n[5] 合并台词：只添加、不替换、幂等")
    items = [{"scene": "旧", "text": "已经有的台词"}]
    added = pi.plan_lines_merge(items, [("任命助理", "已经有的台词"), ("交谈1", "新的台词")])
    check("已有的不算新增", added.added, [{"scene": "交谈1", "text": "新的台词"}])
    check("重复的被记下来", added.dup, ["任命助理"])
    again = pi.plan_lines_merge(items + added.added, [("交谈1", "新的台词")])
    check("再合一次没有新增（幂等）", (again.added, again.dup), ([], ["交谈1"]))
    check("空白差异也算同一条",
          pi.plan_lines_merge([{"scene": "x", "text": "你好 呀"}], [("y", "你好呀")]).added, [])


def section_import(tmp: Path) -> None:
    print("\n[6] 真导入：试运行不写、写就写全（含备份）")
    proj = make_project(tmp / "p1")
    plans = pi.scan(proj, only=["clementia"])
    check("扫到一个角色", len(plans), 1)
    check("认出了两份素材", sorted(s.kind for s in plans[0].sources), ["lines", "profile"])

    before = snapshot(proj)
    dry = pi.import_char(proj, plans[0], apply=False)
    check("试运行报告要加几条台词", dry.lines_added, 2)
    check("试运行有档案章节数", dry.profile_sections, 4)
    check("★试运行一个字节都没写★", snapshot(proj), before)
    check("试运行也给了目标路径", dry.profile_path is not None, True)

    res = pi.import_char(proj, plans[0], apply=True)
    check("真导入成功", res.ok, True, detail="; ".join(res.problems))
    data = json.loads((proj / "data" / "personas" / "clementia.json").read_text(encoding="utf-8"))
    # ★回归：算完合并必须真的写进文件★（第一版报告 +38、文件里 0 条）
    check("★台词真的写进 json 了★", len(data["lines"]), 2)
    check("台词内容对", data["lines"][0]["text"], "这些基础的工作，您希望由我来做？")
    check("原有字段没被动（style/notes）", (data["style"], data["notes"]), (["温和"], "测试用"))
    check("写了备份", any(b.endswith(".bak") for b in res.backups), True, detail=str(res.backups))
    check("备份是导入前那份（0 条台词）",
          len(json.loads((proj / "data" / "personas" / "clementia.json.bak")
                         .read_text(encoding="utf-8"))["lines"]), 0)

    know = proj / "data" / "knowledge" / "clementia" / "档案.md"
    check("档案写进知识库了", know.is_file(), True)
    text = know.read_text(encoding="utf-8")
    check("知识库里有代号", "**代号**：克莱门莎" in text, True)
    check("知识库里有章节标题", "## 客观履历" in text, True)
    check("知识库里没有门禁行", "以查看" not in text, True)
    check("知识库文件里有来源", "profile.txt" in text, True)

    again = pi.import_char(proj, plans[0], apply=True)
    check("★再导一次什么都不做（幂等）★",
          (again.lines_added, again.lines_dup, again.profile_changed), (0, 2, False))
    check("档案 md 没被动", know.read_text(encoding="utf-8"), text)


def section_world_and_create(tmp: Path) -> None:
    print("\n[7] 世界观组（--world）与骨架（--create）")
    proj = make_project(tmp / "p2")
    plans = pi.scan(proj, only=["clementia"])
    res = pi.import_char(proj, plans[0], apply=True, world="明日方舟", lines=False)
    dest = proj / "data" / "knowledge" / "_worlds" / "明日方舟" / "clementia-档案.md"
    check("档案放进了世界观组", dest.is_file(), True, detail=str(dest))
    check("世界观的档案不落在专属目录", (proj / "data" / "knowledge" / "clementia").exists(), False)
    data = json.loads((proj / "data" / "personas" / "clementia.json").read_text(encoding="utf-8"))
    check("世界观的字段被追加", data["worlds"], ["明日方舟"])
    res2 = pi.import_char(proj, pi.scan(proj, only=["clementia"])[0], apply=True,
                         world="明日方舟", lines=False)
    check("再导不会重复追加", res2.worlds_added, [])

    print("\n[8] 没有人生格文件时：默认报错，--create 才生成骨架")
    proj2 = make_project(tmp / "p3", with_json=False, with_wavs=True)
    (proj2 / "data" / "personas" / "clementia.json").unlink(missing_ok=True)
    plan2 = pi.scan(proj2, only=["clementia"])[0]
    no_create = pi.import_char(proj2, plan2, apply=True)
    check("没有 json 且没给 --create → 明确报错", no_create.ok, False)
    check("错误里指了路", "--create" in " ".join(no_create.problems), True,
          detail=str(no_create.problems))
    check("报错时没写文件", (proj2 / "data" / "personas" / "clementia.json").exists(), False)

    made = pi.import_char(proj2, plan2, apply=True, create=True)
    check("生成骨架成功", made.ok, True, detail="; ".join(made.problems))
    data2 = json.loads((proj2 / "data" / "personas" / "clementia.json").read_text(encoding="utf-8"))
    check("骨架里名字来自档案的代号", data2["name"], "克莱门莎")
    check("骨架里唤醒词就是名字", data2["wake_words"], ["克莱门莎"])
    check("★骨架里台词也在（不是只在报告里）★", len(data2["lines"]), 2)
    check("骨架里 background 用了档案第一段", bool(data2["background"]), True,
          detail=str(data2["background"])[:40])
    check("骨架里提醒了要人补", "style" in data2["notes"], True)

    idx = json.loads((proj2 / "data" / "characters.json").read_text(encoding="utf-8"))
    check("骨架不自动进索引（要显式 --add-index）",
          [c.get("id") for c in idx["characters"]], [])

    proj3 = make_project(tmp / "p4", with_json=False)
    (proj3 / "data" / "personas" / "clementia.json").unlink(missing_ok=True)
    plan3 = pi.scan(proj3, only=["clementia"])[0]
    res3 = pi.import_char(proj3, plan3, apply=True, create=True, add_index=True)
    idx3 = json.loads((proj3 / "data" / "characters.json").read_text(encoding="utf-8"))
    check("--add-index 时加了索引", [c.get("id") for c in idx3["characters"]], ["clementia"])
    check("索引条目指向正确文件", idx3["characters"][0]["file"], "personas/clementia.json")
    check("索引也备份了", any(b == "characters.json.bak" for b in res3.backups), True,
          detail=str(res3.backups))


def section_real_repo() -> None:
    print("\n[9] 真实仓库：只读扫一遍，且★没被动过★")
    real_persona = ROOT / "data" / "personas" / "clementia.json"
    real_knowledge = ROOT / "data" / "knowledge" / "clementia" / "档案.md"
    stamp = (hashlib.sha256(real_persona.read_bytes()).hexdigest() if real_persona.is_file() else "",
             hashlib.sha256(real_knowledge.read_bytes()).hexdigest()
             if real_knowledge.is_file() else "",
             real_persona.stat().st_mtime_ns if real_persona.is_file() else 0)

    plans = pi.scan(ROOT)
    ids = {p.char_id for p in plans}
    check("扫到了四个角色（_ 开头的目录不算）", {"kaltsit", "amiya", "exusiai"} <= ids, True,
          detail=str(sorted(ids)))
    check("没有扫到 _nonvoice 这类目录", any(x.startswith("_") for x in ids), False)
    clem = [p for p in plans if p.char_id == "clementia"]
    if clem:
        kinds = sorted(s.kind for s in clem[0].sources)
        check("克莱门莎的两份素材都在", kinds, ["lines", "profile"])
        check("她在索引里了", clem[0].in_index, True)

    time.sleep(0.01)
    now = (hashlib.sha256(real_persona.read_bytes()).hexdigest(),
           hashlib.sha256(real_knowledge.read_bytes()).hexdigest(),
           real_persona.stat().st_mtime_ns)
    check("★真实的人格文件与知识库一个字节都没被动过★", now, stamp)
    check("真实档案能解析出章节",
          len(pi.parse_profile(real_knowledge.read_text(encoding="utf-8")).sections) >= 8, True)


def main() -> int:
    print("=" * 72)
    print(" 角色素材导入自测（★不联网、不碰真实数据★）")
    print("=" * 72)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        section_parse()
        section_no_bleed()
        section_classify(make_project(tmp / "p0"))
        section_manifest_skip(make_project(tmp / "p0b"))
        section_merge()
        section_import(tmp)
        section_world_and_create(tmp)
    section_real_repo()
    print("\n" + "=" * 72)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
