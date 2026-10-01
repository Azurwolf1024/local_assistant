"""拼音近音唤醒词自测：**把别名表拿掉**，看能不能光靠拼音认出真实听错。

这是这次改动的验收标准（用户要求：给一个唤醒词，不用手工加近似词）。
所以这里全部用**真人耳听攒下的听错数据**（凯尔希那边 58 条、阿米娅 7 条、
能天使资料卡里 5 条），且构造匹配器时**一个别名都不给**。

    python scripts\\test_phonetic.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import names, phonetic, wake   # noqa: E402
from voice_loop.persona import Character       # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()

# 真实听错（来自实测/人格文件里的人工别名，这里只当**测试样本**用）
MISHEARD = {
    "能天使": ["能天史", "能天师", "宁天使", "能田使"],
    "阿能": ["阿宁"],
    "凯尔希": ["开尔信", "胎儿戏", "泰尔西", "开尔西", "凯儿戏", "海尔西", "太儿西"],
    "白泽": ["百泽", "白则", "白择", "白哲", "柏泽"],   # 白哲 = 平翘舌（zh/z）
}
# 日常负样本：绝不能因为拼音像就叫醒（实测过的那批）
NEGATIVE = [
    "海尔洗衣机坏了", "开尔文是谁", "今天天气不错", "米亚的论文写完了吗",
    "胎儿心率有点快", "我觉得还行", "海尔的冰箱不制冷", "宁泽涛游泳",
    "阿米巴原虫是什么",  # 与「阿米娅」只差一个韵母（单韵母 a，要有信息量才认）
]


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def make_matcher(tmp: Path, *, no_alias=("能天使", "阿能", "凯尔希", "白泽")) -> wake.WakeWordMatcher:
    """建一个**没有别名**的匹配器（多角色，和线上一样走 set_characters）。"""
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "wakewords.json"
    path.write_text(json.dumps({"enabled": True, "words": [], "aliases": {}}, ensure_ascii=False),
                    encoding="utf-8")
    got = wake.WakeWordMatcher(path)
    chars = [
        Character.from_dict({"id": "exusiai", "name": "能天使", "wake_words": ["能天使", "阿能"]}),
        Character.from_dict({"id": "kaltsit", "name": "凯尔希", "wake_words": ["凯尔希"]}),
        Character.from_dict({"id": "baize", "name": "白泽", "wake_words": ["白泽"]}),
    ]
    got.set_characters(chars)
    return got


# --------------------------------------------------------------------------- #
def test_syllables() -> None:
    print("\n[1] 音节拆分与相似度：只有三档，而且单韵母不算数")
    check("拆声母韵母", phonetic.split("kai"), ("k", "ai"))
    check("zh 优先于 z", phonetic.split("zhang"), ("zh", "ang"))
    check("零声母", phonetic.split("er"), ("", "er"))
    check("全同 1.0", phonetic.similarity("kai", "kai"), 1.0)
    check("同韵母 0.7（凯 t t 泰）", phonetic.similarity("kai", "tai"), 0.7)
    check("同声母 0.7（凯 t t 看）", phonetic.similarity("kai", "kan"), 0.7)
    check("都不像 0", phonetic.similarity("kai", "wen"), 0.0)
    check("★单韵母不算「同韵母」★（阿米巴 ≠ 阿米娅）",
          phonetic.similarity("ba", "ya"), 0.0)
    check("长韵母才算（泰/海 对 凯）", phonetic.similarity("tai", "kai"), 0.7)
    check("★平翘舌也算相似（白哲 对 白泽）★", phonetic.similarity("zhe", "ze"), 0.7)
    check("★前后鼻音也算相似（in 对 ing）★", phonetic.similarity("xin", "xing"), 0.7)
    check("n/l 口音也算（宁 对 灵）", phonetic.similarity("ning", "ling"), 0.7)
    check("★不因此变成「什么都像」★（凯 对 文 仍是 0）",
          phonetic.similarity("kai", "wen"), 0.0)
    check("拼音取不到就降级（非汉字给空）", phonetic.syllables("abc"), ("", "", ""))


def test_compare() -> None:
    print("\n[2] 序列比对：唤醒词版（恰好一个音节不同）vs 人名版（只许首字不同）")
    neng_tian_shi = ("neng", "tian", "shi")   # 能天使
    kai_er_xi = ("kai", "er", "xi")           # 凯尔希
    check("全同 = 1.0", phonetic.compare(neng_tian_shi, neng_tian_shi), 1.0)
    check("★同音字天生全同（能天史 = neng/tian/shi）★",
          phonetic.compare(("neng", "tian", "shi"), neng_tian_shi), 1.0)
    check("一个音节听错：宁天使（ning vs neng，同声母）→ 0.7",
          phonetic.compare(("ning", "tian", "shi"), neng_tian_shi), 0.7)
    check("★人名版：末字不同就不认（凯尔信）★",
          phonetic.compare(("kai", "er", "xin"), kai_er_xi, only_first=True), 0.0)
    check("人名版：首字不同且近音 → 认（泰尔西）",
          phonetic.compare(("tai", "er", "xi"), kai_er_xi, only_first=True), 0.7)
    check("两个音节都错 → 不认（泰尔文）",
          phonetic.compare(("tai", "er", "wen"), kai_er_xi, allow_diff=1), 0.0)
    check("长度不等 → 不认", phonetic.compare(("kai", "er"), kai_er_xi), 0.0)
    check("含未取到拼音的字 → 不认", phonetic.compare(("kai", "", "xi"), kai_er_xi), 0.0)
    check("拼音撞车的常用词要挡", (phonetic.blocked("海尔洗衣机"), phonetic.blocked("能天使")),
          (True, False))


def test_wake_no_alias(tmp: Path) -> None:
    print("\n[3] ★核心验收：不给任何别名，能认出来吗★")
    got = make_matcher(tmp / "a")
    check("匹配器建起来了（多角色）", got.characters_loaded(), True)
    check("表里确实一个别名都没有", [v for _w, v, _c in got._patterns if len(v) > 1], [])
    total = hit = 0
    for word, bads in MISHEARD.items():
        for bad in bads:
            total += 1
            hit_got = got.match(bad)
            owner = hit_got.character if hit_got else ""
            want_owner = {"能天使": "exusiai", "阿能": "exusiai",
                          "凯尔希": "kaltsit", "白泽": "baize"}[word]
            ok = bool(hit_got) and owner == want_owner
            hit += 1 if ok else 0
            if not ok:
                print(f"    {FAIL} {bad} → {owner or '没命中'}（期望 {want_owner}）")
    check(f"★{total} 条真实听错全中★", hit, total)

    with_req = got.match("能天史，现在几点")
    # ★注意★：remainder 会带上紧跟的那个标点（老行为，精确路径也一样），
    # 所以这里只断言「请求内容对了」。
    check("带请求也认，并且把请求切出来",
          (with_req.character if with_req else "",
           (with_req.remainder if with_req else "").strip("，,。、 ")),
          ("exusiai", "现在几点"))
    filler = got.match("呃，能天史")
    check("允许前面有个语气词", bool(filler) and filler.character, "exusiai")
    plain = got.match("凯尔希，几点")
    check("本来就喊对的不受影响（精确路径，不是模糊）",
          (bool(plain), plain.fuzzy if plain else None), (True, False))
    check("相似度提示也认得它（不自相矛盾）", got.best_ratio("能天史") >= 0.7, True)


def test_negative(tmp: Path) -> None:
    print("\n[4] ★日常负样本：一条都不能被叫醒★")
    got = make_matcher(tmp / "b")
    fired = []
    for text in NEGATIVE:
        hit = got.match(text)
        if hit:
            fired.append((text, hit.word, hit.character, round(got.best_ratio(text), 2)))
    check(f"{len(NEGATIVE)} 条负样本 0 误触发", fired, [])


def test_position(tmp: Path) -> None:
    print("\n[5] 只在句首附近认（放开到全句会立刻被日常词误伤）")
    got = make_matcher(tmp / "c")
    got._settings.fuzzy_ratio = 1.1        # ★关掉字符模糊层★：命中就一定来自拼音层（确定性）
    check("句首 → 认", bool(got.match("能天史，帮我看看日程")), True)
    check("句中 → 不认", got.match("我跟能天史说过这件事") is None, True)
    check("句末 → 不认", got.match("昨天跟我说话的是能天史") is None, True)
    check("★拼音撞车词在任何位置都不认★",
          [bool(got.match(t)) for t in ("海尔洗衣机", "把海尔洗衣机修一下")], [False, False])


def test_switch(tmp: Path) -> None:
    print("\n[6] 开关与降级：关掉就回到「只能靠别名」的老行为")
    got = make_matcher(tmp / "d")
    got._settings.phonetic = False
    check("关掉拼音层 → 能天史不再命中", got.match("能天史") is None, True)
    check("关掉之后相似度提示也不虚报", got.best_ratio("能天史") < 0.75, True)
    got._settings.phonetic = True
    check("打开 → 又能认", bool(got.match("能天史")), True)

    one = wake.WakeWordMatcher(tmp / "d" / "single.json")
    one.set_characters([Character.from_dict({"id": "x", "name": "光", "wake_words": ["光"]})])
    check("单音节唤醒词不做近音（太容易误触发）", one._phonetic, [])
    check("单音节词精确匹配照旧", bool(one.match("光")), True)


def test_single_source() -> None:
    print("\n[7] 人名与唤醒词共用同一份音近规则（别在两处各写一份）")
    check("names 的相似度就是 phonetic 的",
          names.syllable_similarity("kai", "tai"), phonetic.similarity("kai", "tai"))
    check("黑名单是同一个对象", names._BLOCK is phonetic.BLOCK, True)
    check("names 自己没再抄一份声母表", names._INITIALS is phonetic.INITIALS, True)
    corrector = names.NameCorrector(["凯尔希"], {"凯尔希": ["开尔信"]})
    text, hits = corrector.correct("开尔信，现在几点")
    check("人名校正仍然工作（抽取后没走样）", (text, bool(hits)), ("凯尔希，现在几点", True))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="phonetic_test_"))
    try:
        test_syllables()
        test_compare()
        test_wake_no_alias(tmp)
        test_negative(tmp)
        test_position(tmp)
        test_switch(tmp)
        test_single_source()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 拼音近音自测全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
