"""`voice_loop/memory/` 的离线自测：分级、自清洁、双路检索、隔离。

为什么这些规则值得一条条钉住：记忆系统坏起来是**静默**的 ——
衰减算错只会「慢慢想不起来」，合并过松会把两件不相干的事糊成一条，
淘汰过狠会把重要的事删掉，而这一切都不会报错。
所以每条规则都拿手造的数据断言一次（不需要模型、不需要麦克风）。

    python scripts\\test_memory.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.memory import Memory, MemoryHub  # noqa: E402
from voice_loop.memory.extract import Turn, episodes_from_session, facts_from_text, signals  # noqa: E402
from voice_loop.memory.knowledge import build_knowledge, split_text  # noqa: E402
from voice_loop.memory.levels import (  # noqa: E402
    compact_episodes,
    coverage,
    fact_eff,
    keywords_of,
    merge_episodes,
    merge_facts,
    pick_old_sessions,
    promote_to_facts,
    prune_episodes,
    prune_facts,
    salience_eff,
    shared_topics,
    tokenize,
    topics_overlap,
)
from voice_loop.memory.model import Episode, Fact  # noqa: E402
from voice_loop.memory.retrieve import parse_when  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()          # ★不能用 None 当「没给期望」★：好几条断言的期望正好是 None/0/False


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    """两种写法：`check(名, 条件)` 只判真假；`check(名, 实际, 期望)` 比相等。"""
    if want is _UNSET:
        ok = bool(got)
        print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail or got}" if detail else ""))
    else:
        ok = got == want
        print(f"  {PASS if ok else FAIL} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else ""))
    if not ok:
        _failures.append(name)


def ep(title: str, summary: str = "", ts: str = "2026-09-20 10:00", **kw) -> Episode:
    return Episode(ts=ts, title=title, summary=summary or title,
                   keywords=keywords_of(f"{title} {summary}", prefer=title), **kw)


NOW = datetime(2026, 9, 26, 12, 0, 0)


def test_tokens() -> None:
    print("\n[1] 分词与相似度：中文二元组 + 标题优先的关键词")
    check("中文切出二元组", {"组会", "下午"} <= tokenize("组会改到下午两点"))
    check("英文数字按词切", "14" in tokenize("改到 14:00"))
    check("虚词被滤掉", "的" not in tokenize("我的日程"))
    kws = keywords_of("组会时间确认 组会确认是下午两点开始", prefer="组会时间确认")
    check("关键词优先取标题里的",
          kws[0] in tokenize("组会时间确认"), detail=str(kws[:3]))
    check("关键词不含单字", all(len(k) >= 2 for k in kws), detail=str(kws[:5]))
    check("覆盖度算查询命中比例（★查询被完全覆盖就是 1.0★）", coverage({"a", "b"}, {"a", "b", "c", "d"}), 1.0)
    check("只命中一部分就给部分分", round(coverage({"a", "b", "c"}, {"a"}), 2), 0.33)
    check("空查询覆盖度 0（不算命中）", coverage(set(), {"a"}), 0.0)


def test_decay() -> None:
    print("\n[2] 衰减与回血：越久越淡，用过就回血，身份永不褪色")
    fresh = ep("今天的事", ts=NOW.isoformat(sep=" "), salience=0.8)
    old = ep("很久以前的事", ts=(NOW - timedelta(days=180)).isoformat(sep=" "), salience=0.8)
    check("新事有效重要度 ≈ 原值", round(salience_eff(fresh, NOW), 2), 0.8)
    check("半年后显著衰减（< 0.2）", salience_eff(old, NOW) < 0.2, detail=f"{salience_eff(old, NOW):.3f}")
    recalled = ep("被想起过的事", ts=(NOW - timedelta(days=180)).isoformat(sep=" "),
                  salience=0.5, recalls=5)
    check("★被反复想起的旧事回血★", salience_eff(recalled, NOW) > salience_eff(old, NOW),
          detail=f"{salience_eff(recalled, NOW):.3f} > {salience_eff(old, NOW):.3f}")
    pinned = Fact(key="self.名字", value="白泽", pinned=True, confidence=1.0,
                  last_seen=(NOW - timedelta(days=999)).isoformat(sep=" "))
    check("★pinned 的事实不衰减★", fact_eff(pinned, NOW), 1.0)
    weak = Fact(key="user.喜好.茶", value="喜欢茶", confidence=0.6,
                last_seen=(NOW - timedelta(days=180)).isoformat(sep=" "))
    check("普通事实会衰减（< 0.2）", fact_eff(weak, NOW) < 0.2, detail=f"{fact_eff(weak, NOW):.3f}")


def test_merge() -> None:
    print("\n[3] 合并：同一次组会说两遍 → 合一条（真实踩过的坑）")
    a = ep("组会改到下午两点", "原定上午十点的组会改到 14:00，地点不变", ts="2026-09-24 12:00")
    b = ep("组会时间确认", "组会确认是下午两点开始", ts="2026-09-24 13:00")
    check("主题覆盖度够（>= 0.3）", topics_overlap(a, b) >= 0.3, detail=f"{topics_overlap(a, b):.2f}")
    check("非通用词至少共 2 个", shared_topics(a, b) >= 2,
          detail=str(sorted(set(a.keywords) & set(b.keywords))))
    merged, count = merge_episodes([a, b])
    check("★合并成一条★", (len(merged), count), (1, 1))
    check("合并后保留更长的摘要", "原定上午十点" in merged[0].summary)
    check("记录合并了 2 条", merged[0].merged, 2)
    check("留了 links 可追", bool(merged[0].links))

    far = ep("组会时间确认", "组会确认是下午两点开始", ts="2026-09-26 13:00")
    check("隔天的同主题不合并", merge_episodes([a, far])[1], 0)
    other = ep("买牛奶", "记得买牛奶", ts="2026-09-24 12:30")
    check("无关的事不合并", merge_episodes([a, other])[1], 0)
    generic = ep("今天下午的事", "今天下午收拾一下", ts="2026-09-24 12:30")
    check("★只共通用词（下午/今天）不算同一件事★", shared_topics(a, generic) < 2,
          detail=str(sorted(set(a.keywords) & set(generic.keywords))))


def test_compact_and_prune() -> None:
    print("\n[4] 压缩与淘汰：旧的次要内容只留摘要；超上限按分数留")
    old_minor = ep("一次闲聊", "聊了天气", ts=(NOW - timedelta(days=120)).isoformat(sep=" "),
                   salience=0.3, detail="很长的原文" * 20)
    old_major = ep("搬家的日子", "那天搬了家", ts=(NOW - timedelta(days=120)).isoformat(sep=" "),
                   salience=0.9, detail="重要的原文")
    check("压缩掉 1 条", compact_episodes([old_minor, old_major], NOW), 1)
    check("次要的原文清空", old_minor.detail, "")
    check("★重要的原文留着★", bool(old_major.detail))

    rows = [ep(f"事{i}", f"第 {i} 件", ts=NOW.isoformat(sep=" "), salience=i / 10.0)
            for i in range(1, 11)]
    kept, dropped = prune_episodes(rows, NOW, max_count=4)
    check("超上限就淘汰", (len(kept), dropped), (4, 6))
    check("★留下的都是高重要度★", min(e.salience for e in kept) >= 0.7,
          detail=str(sorted(round(e.salience, 1) for e in kept)))
    check("上限内不动", prune_episodes(rows, NOW, 99)[1], 0)


def test_promote() -> None:
    print("\n[5] L2→L3：反复出现的事上升为「事实」（模仿人脑）")
    rows = [ep("组会", "周三下午组会", ts=f"2026-09-{day:02d} 10:00") for day in (1, 8, 15)]
    facts = promote_to_facts(rows, [])
    check("提到 3 次（不同天）→ 生成事实", len(facts), 1)
    check("事实标了来源与次数", facts[0].evidence >= 3 and facts[0].note.startswith("由"))
    same_day = [ep("组会", "周三下午组会", ts=f"2026-09-01 1{hour}:00") for hour in range(3)]
    check("★同一天说三遍不算习惯★（不生成事实）", promote_to_facts(same_day, []), [])
    existing = [Fact(key="theme.组会", value="x")]
    check("已有同 key 不重复生成", len(promote_to_facts(rows, existing)), 1)


def test_facts() -> None:
    print("\n[6] 事实合并：同 key 留证据多的，弱的清掉，pinned 永存")
    a = Fact(key="user.喜好.咖啡", value="喜欢咖啡", confidence=0.6, evidence=1,
             last_seen="2026-09-01 10:00")
    b = Fact(key="user.喜好.咖啡", value="喜欢咖啡", confidence=0.7, evidence=3,
             last_seen="2026-09-20 10:00", sources=["s1"])
    out = merge_facts([a, b])
    check("同 key 合成一条", len(out), 1)
    check("★保留证据多的那条★", out[0].evidence >= 3)
    check("证据累加（1 + 3）", out[0].evidence, 4)
    weak = Fact(key="user.喜好.茶", value="喜欢茶", confidence=0.1, evidence=1,
                last_seen="2026-09-25 10:00")
    check("弱到没意义的事实被清", merge_facts([weak]), [])
    strong = Fact(key="user.喜好.茶", value="喜欢茶", confidence=0.9, evidence=2,
                  last_seen="2026-09-25 10:00")
    keep, dropped = prune_facts([strong, weak, Fact(key="self.名字", value="白泽", pinned=True)],
                                NOW, max_count=2)
    check("淘汰时 pinned 一定留下", any(f.pinned for f in keep))
    check("超上限淘汰弱事实", dropped >= 1)
    check("身份 pinned 不算弱", len(merge_facts([Fact(key="x", value="y", confidence=0.0, pinned=True)])), 1)


def test_when() -> None:
    print("\n[7] 时间解析：中文时间窗（认不出就说认不出，不猜）")
    cases = {
        "昨天": (datetime(2026, 9, 25), datetime(2026, 9, 26)),
        "今天": (datetime(2026, 9, 26), datetime(2026, 9, 27)),
        "上周": (datetime(2026, 9, 14), datetime(2026, 9, 21)),
        "上周三": (datetime(2026, 9, 16), datetime(2026, 9, 17)),
        "最近三天": (datetime(2026, 9, 24), datetime(2026, 9, 26, 12, 0)),
        "上个月": (datetime(2026, 8, 1), datetime(2026, 9, 1)),
        "去年": (datetime(2025, 1, 1), datetime(2026, 1, 1)),
    }
    for text, want in cases.items():
        got = parse_when(text, NOW)
        check(f"「{text}」", got == want, detail="" if got == want else f"{got} != {want}")
    check("★认不出就返回 None（不猜一个窗口）★", parse_when("那阵子", NOW) is None)
    check("空文本 → None", parse_when("", NOW) is None)


def test_memory_facade(tmp: Path) -> None:
    print("\n[8] Memory 门面：归档 / 巩固 / 双路检索 / 回血 / 隔离")
    kb = build_knowledge([], [("世界观", "白泽：通晓万物之名的瑞兽，住在本地机器里。")])
    mem = Memory(tmp, "baize", knowledge=kb)
    check("身份种子写进 L3", mem.seed_identity("白泽", "本地助手", "阁下"), 3)
    check("身份是 pinned", all(f.pinned for f in mem.facts() if f.key.startswith("self.")))
    check("重复种子幂等", mem.seed_identity("白泽", "本地助手", "阁下"), 0)

    mem.note("组会改到下午两点", "原定上午十点的组会改到 14:00", kind="event",
             when="2026-09-24 12:00", event_id=42)
    mem.note("组会时间确认", "组会确认是下午两点开始", kind="event", when="2026-09-24 13:00")
    mem.note("学长生日", "学长生日在 10 月 3 日", kind="event", when="2026-09-16 12:00")
    _, facts = mem.remember("我喜欢喝拿铁，不加糖", when="2026-09-25 09:00")
    check("从一句话抽出事实", [f.key for f in facts] == ["user.喜好.拿铁"],
          detail=str([f.key for f in facts]))

    report = mem.consolidate(now=NOW)
    check("巩固把重复的合并了", report["merged"] >= 1, detail=str(report))
    check("巩固后事件数下降", report["episodes"], 3)

    by_topic = mem.recall("组会几点", now=NOW, use_knowledge=False)
    check("按内容检索命中组会", bool(by_topic) and "组会" in by_topic[0].text,
          detail=by_topic[0].text[:20] if by_topic else "（没命中）")
    by_time = mem.recall("", when="上周", now=NOW, use_knowledge=False)
    check("按时间检索命中上周的事", bool(by_time) and "学长生日" in by_time[0].text,
          detail=str(len(by_time)))
    check("★纯时间检索不掺事实★", all(h.kind != "fact" for h in by_time))
    both = mem.recall("组会", when="上周", now=NOW, use_knowledge=False, limit=3)
    check("时间窗不匹配的主题也不放进来", all("组会" not in h.text or "09-2" in h.text for h in both))
    kb_hits = mem.recall("白泽是什么", now=NOW)
    check("★知识库能被检索到★", any(h.kind == "chunk" for h in kb_hits),
          detail=str([h.kind for h in kb_hits]))
    check("★命中会回血★", any(e.recalls > 0 for e in mem.episodes()))

    block = mem.prompt_block("组会几点", now=NOW)
    check("提示词块含身份", "身份" in block and "白泽" in block)
    check("提示词块有硬上限", len(mem.prompt_block("组会", max_chars=80, now=NOW)) <= 120)
    check("forget 能删掉一条", mem.forget(mem.episodes()[0].id))
    check("forget 不认识的东西返回 False", mem.forget("不存在"), False)

    hub = MemoryHub(settings=None, root=tmp, global_knowledge=kb)
    check("★按角色隔离★", len(hub.for_character("kaltsit").episodes()), 0)
    check("角色目录被列出", "baize" in hub.characters())


def test_session_archive(tmp: Path) -> None:
    print("\n[9] L1→L2：原始对话归档（幂等），游动窗口只清归档过的")
    sessions = tmp / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / "session-20260924-120000.jsonl"
    rows = [
        {"turn": 1, "user_text": "记住，周五下午三点跟导师见面", "answer": "记下了。"},
        {"turn": 2, "user_text": "今天天气不错", "answer": "嗯。"},
        {"turn": 3, "user_text": "我喜欢喝拿铁", "answer": "好。"},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (sessions / "session-broken.jsonl").write_text("{不是 json\n", encoding="utf-8")

    mem = Memory(tmp, "baize")
    first = mem.ingest_session(path, llm_call=None)
    check("归档出事件", first["episodes"] >= 2, detail=str(first))
    check("归档出事实", first["facts"] >= 1)
    check("★同一个文件不重复归档（幂等）★", mem.ingest_session(path).get("skipped"), True)
    check("坏文件不炸", mem.ingest_session(sessions / "session-broken.jsonl")["episodes"], 0)
    # 滑动窗口：老的、已归档的可以清；没归档的绝不删；最近若干条保底不动
    states = dict(mem.session_states())
    states["session-old-extracted.jsonl"] = {"extracted": True, "last_ts": "2026-01-01 10:00"}
    states["session-old-pending.jsonl"] = {"extracted": False, "last_ts": "2026-01-02 10:00"}
    # 再多造 12 个近期文件：保底（默认留最近 10 个）得真有东西可保，才测得出「超期的会被清」
    for i in range(12):
        states[f"session-recent-{i:02d}.jsonl"] = {
            "extracted": True, "last_ts": f"2026-09-{20 + i % 5:02d} 10:00"}
    from voice_loop.memory.store import write_json  # noqa: PLC0415
    write_json(mem.paths.sessions, states)
    victims = mem.prune_raw_sessions(sessions, now=NOW, dry_run=True)
    check("★超期且已归档的才清★", "session-old-extracted.jsonl" in victims)
    check("★没归档过的绝不删★", "session-old-pending.jsonl" not in victims)
    check("★保底留最近的（不漏删最新那几条）★",
          not any(name.startswith("session-recent-") for name in victims),
          detail=str(sorted(victims)))
    old = sessions / "session-old-extracted.jsonl"
    old.write_text("{}", encoding="utf-8")
    removed = mem.prune_raw_sessions(sessions, now=NOW, dry_run=False)
    check("dry_run=False 真的删文件", not old.exists() and "session-old-extracted.jsonl" in removed)


def test_extract_rules() -> None:
    print("\n[10] 抽什么、不抽什么（保守是有意的）")
    check("「记住…」标为重要", signals("记住，周五下午三点跟导师见面").important)
    check("闲聊不标重要", signals("今天天气不错").important, False)
    check("决定/承诺标重要", signals("说好了下周三一起去图书馆").important)
    check("感受标重要", signals("我最近很累").important)
    check("事实抽取：喜好", [f.key for f in facts_from_text("我喜欢喝拿铁")], ["user.喜好.拿铁"])
    check("事实抽取：名字", [f.value for f in facts_from_text("我的名字是张三")], ["张三"])
    check("疑问句不抽成事实", facts_from_text("我叫什么来着"), [])
    turns = [Turn(turn=1, user="记住，周五下午三点跟导师见面", answer="记下了。"),
             Turn(turn=2, user="今天天气不错", answer="嗯。")]
    eps, facts = episodes_from_session(turns, source="s.jsonl", now=NOW)
    check("只有重要那轮进 L2", len(eps), 1)
    check("事件带来源与轮次", eps[0].source == "s.jsonl" and eps[0].turn == 1)
    hinted, _ = episodes_from_session(turns, source="s.jsonl", now=NOW,
                                      hints=[{"title": "写了日程", "event_id": 7, "kind": "event"}])
    check("技能写进日程的事实变成事件（带 event_id）",
          any(e.event_id == 7 for e in hinted))


def test_knowledge() -> None:
    print("\n[11] 知识库：切段、检索、来源可追")
    doc = ("# 世界观\n\n罗德岛是一艘在陆地上航行的移动城市，船上有医疗部与工程部。\n\n"
           "# 人物\n\n白泽通晓万物之名，如今住在本地机器里，替你记事。")
    chunks = split_text(doc, "doc.md")
    check("按标题切段", len(chunks) >= 2, detail=str([c.title for c in chunks]))
    check("标题跟着正文", any(c.title == "人物" and "白泽" in c.text for c in chunks))
    # ★短文档不能被静默丢掉★（一行设定也是知识；丢了就变成「查不到还不知道为什么」）
    tiny = split_text("白泽：识万物之名。", "tiny.md")
    check("★很短的一份文档也能成片段★", len(tiny) == 1 and "白泽" in tiny[0].text,
          detail=str([c.text for c in tiny]))
    kb = build_knowledge([], [("设定", "白泽：通晓万物之名，住在本地机器里。")])
    check("inline 来源可用", kb.available())
    check("能搜到", bool(kb.search("白泽是谁")))
    check("统计对得上", sum(kb.stats().values()) >= 1, detail=str(kb.stats()))


def main() -> int:
    print("=" * 70)
    print(" 记忆系统自测（分级 / 自清洁 / 双路检索 / 隔离）")
    print("=" * 70)
    tmp = Path(tempfile.mkdtemp(prefix="mem-test-"))
    try:
        test_tokens()
        test_decay()
        test_merge()
        test_compact_and_prune()
        test_promote()
        test_facts()
        test_when()
        test_memory_facade(tmp / "facade")
        test_session_archive(tmp / "archive")
        test_extract_rules()
        test_knowledge()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
    else:
        print(" 全部通过 √")
    print("=" * 70)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
