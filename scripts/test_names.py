"""人名校正的自测：用真人耳听攒下的听错 + 日常口语负样本。

跑法：
    python scripts/test_names.py

正样本来自 data/personas/*.json 的 aliases（实测听错，必须全部认出来），
另外加一批**表里没有**的新听错（拼音近音推广必须也能收），
负样本是日常词句（一个都不许改）。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.names import NameCorrector  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def load_personas() -> tuple[list[str], dict[str, list[str]]]:
    names: list[str] = []
    aliases: dict[str, list[str]] = {}
    for cid in ("kaltsit", "amiya"):
        data = json.load(io.open(ROOT / "data" / "personas" / f"{cid}.json", encoding="utf-8"))
        label = str(data.get("name") or "").strip()
        words = [str(w).strip() for w in data.get("wake_words") or []]
        for word in [label, *words]:
            if word and word not in names:
                names.append(word)
        for word, variants in (data.get("aliases") or {}).items():
            bucket = aliases.setdefault(word, [])
            for v in variants:
                if v not in bucket:
                    bucket.append(v)
    return names, aliases


def main() -> int:
    names, aliases = load_personas()
    print(f"规范名：{names}；别名表共 {sum(len(v) for v in aliases.values())} 条")
    corr = NameCorrector(names, aliases)
    if not corr.enabled:
        print("× pypinyin 不可用，功能已降级（先 pip install pypinyin）")
        return 1
    # 别名表里「谁是谁」：kaltsit 的别名归凯尔希，amiya 的归阿米娅
    owner = {
        "凯尔希": "凯尔希",
        "阿米娅": "阿米娅",
    }

    print("\n[1] 别名表里的听错（只算纯汉字的；KRC/开C 这种走唤醒词的精确表）")
    total = bad = latin = 0
    for word, variants in aliases.items():
        want = owner.get(word, word)
        for variant in variants:
            if not all("\u4e00" <= c <= "\u9fff" for c in variant):
                latin += 1
                continue
            total += 1
            fixed, hits = corr.correct(f"{variant}，现在几点了")
            if variant == want:      # 规范名自己当然不用改
                continue
            if hits and hits[0].name == want:
                continue
            bad += 1
            print(f"    {FAIL} {variant!r} → {fixed!r}")
    check(f"别名 {total} 条全部认出（昵称那类靠查表）", bad == 0, f"漏 {bad}")
    check(f"拉丁/数字的 {latin} 条交给唤醒词精确表（不在本模块职责内）", latin > 0)

    print("\n[2] 表里没有的新听错（靠拼音近音推广）")
    fresh = [
        ("凯而希", "凯尔希"),
        ("恺尔希", "凯尔希"),
        ("开耳西", "凯尔希"),
        ("海尔西医生", "凯尔希"),
        ("台尔西，帮我看看日程", "凯尔希"),
        ("凯尔希医生在吗", "凯尔希"),
        ("阿米牙", "阿米娅"),
        ("阿咪娅小姐", "阿米娅"),
    ]
    for spoken, want in fresh:
        fixed, hits = corr.correct(spoken)
        got = hits[0].name if hits else "（没认出）"
        check(f"{spoken!r} → {want}", got == want, f"实际 {got}")

    print("\n[3] 负样本：日常词句一个字都不许改（含实测踩到的假阳性）")
    negatives = [
        "今天天气不错", "开会呢", "开视频会议", "卡尔曼滤波", "希尔伯特空间", "凯旋而归",
        "西安", "太累了", "排队等一会儿", "阿米巴原虫", "咪呀一声",
        "喀尔巴阡", "开尔文是谁", "台儿庄战役", "胎儿心率监测", "米亚的论文", "牙齿有点疼",
        "可惜我没去", "谢谢", "帮我把灯关了", "明天早上七点叫我起床",
        "海尔洗衣机怎么用",     # 海尔洗 ≈ 凯尔希的拼音，靠 _BLOCK 挡住
    ]
    wrong = 0
    for text in negatives:
        fixed, hits = corr.correct(text)
        if fixed != text:
            wrong += 1
            print(f"    {FAIL} {text!r} 被改成 {fixed!r}")
    check(f"负样本 {len(negatives)} 条全不动", wrong == 0, f"误改 {wrong}")

    print("\n[4] 位置与幂等")
    fixed, hits = corr.correct("开尔西，现在几点了")
    check("名字在句首时改对", fixed == "凯尔希，现在几点了", fixed)
    check("命中的下标指向原文那段字", bool(hits) and hits[0].spoken == "开尔西", str(hits[0] if hits else None))
    again, hits2 = corr.correct(fixed)
    check("改过的文本再改一次不变（幂等）", again == fixed and not hits2, again)
    tail, hits3 = corr.correct("现在几点了，凯尔西")
    check("句尾的名字不改（本模块只管句首；句尾的听错交给唤醒词匹配）",
          tail == "现在几点了，凯尔西" and not hits3, tail)

    print("\n[5] 角色文件来的索引能用")
    from voice_loop.persona import CharacterRegistry  # noqa: E402
    from voice_loop.settings import load_settings  # noqa: E402

    settings = load_settings()
    reg = CharacterRegistry(settings.resolve(settings.persona.file))
    reg.load()
    live = NameCorrector.from_characters(reg.all(only_enabled=True))
    check("from_characters 建得出索引", live.enabled)
    for spoken, want in (("开尔西在吗", "凯尔希"), ("阿迷娅", "阿米娅")):
        got = live.correct(spoken)[1]
        check(f"{spoken!r} → {want}", bool(got) and got[0].name == want, str(got))

    print("\n[6] 和唤醒词匹配配合起来用（校正 + 匹配）")
    from voice_loop.wake import WakeWordMatcher  # noqa: E402

    wake = WakeWordMatcher(settings.resolve(settings.wake.file))
    wake.set_characters(reg.all(only_enabled=True))
    for spoken, want in (("开尔西，现在几点了", "kaltsit"), ("阿米娅，帮我看看", "amiya")):
        fixed = live.correct(spoken)[0]
        hit = wake.match(fixed)
        check(f"{spoken!r} → {fixed!r} → 归属 {want}",
              bool(hit) and hit.character == want, str(hit))
    tail = "现在几点了，凯尔西"
    hit_tail = wake.match(tail)
    check("句尾的听错虽然没被校正，但唤醒词表能直接命中",
          bool(hit_tail) and hit_tail.character == "kaltsit", str(hit_tail))

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：" + "；".join(_failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
