"""记忆的两个新能力自测：★角色级知识库（L4 隔离）★ + ★记忆 MCP 工具★。

为什么值得钉住：
    1. 知识库隔离坏起来是**静默**的 —— 共享库多收了一层子目录，凯尔希就会拿明日方舟的设定
       去回答原神的问题，而且**不报错**。所以「共享库绝不能看见任何角色子目录」要断言。
    2. MCP 工具是模型能写记忆的唯一入口，写错角色（白泽记的事跑到凯尔希名下）同样是静默的。

    python scripts\\test_memory_mcp.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.mcp.servers import memory as mem_server  # noqa: E402
from voice_loop.memory import MemoryHub  # noqa: E402
from voice_loop.persona import (  # noqa: E402
    Character,
    CharacterRegistry,
    can_read_all_memory,
    knowledge_spec_for,
    render_system_prompt,
)
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    if want is _UNSET:
        ok = bool(got)
        print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail or got}" if detail else ""))
    else:
        ok = got == want
        print(f"  {PASS if ok else FAIL} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else ""))
    if not ok:
        _failures.append(name)


def check_text(name: str, condition, text: str) -> None:
    """「条件 + 现场文字」：过了只打名字，没过把接下来该怎么办用的文本一起打出来。

    ★别写成 `check(name, cond, text)`★：三参形式比的是「相等」，第三参是期望值，
    拿说明当期望就会成批假失败（这个自测第一版就这么翻了 15 条）。
    """
    check(name if condition else f"{name}  ← 拿到：{text[:140]}", condition, True, "")


# --------------------------------------------------------------------------- #
# 搭一个「两个角色、各有一份专属世界观」的临时环境
# --------------------------------------------------------------------------- #
BAIZE_WORD = "白泽纹章"        # 只应出现在白泽的世界观里
KALTSIT_WORD = "罗德岛病房"     # 只应出现在凯尔希的世界观里
SHARED_WORD = "这台机器的主人叫阁下"   # 共享资料，谁都该看到
WORLD_WORD = "源石"            # ★世界观组：挂了「明日方舟」的角色共享★
ARKS = "明日方舟"


def make_world(tmp: Path):
    """写知识库文件 + 角色索引，返回 (settings, registry, knowledge_dir)。"""
    kn = tmp / "knowledge"
    (kn / "baize").mkdir(parents=True)
    (kn / "kaltsit").mkdir(parents=True)
    (kn / "_worlds" / ARKS).mkdir(parents=True)          # ★同一个 IP 共享的那份★
    (kn / "共享说明.md").write_text(f"# 共享说明\n\n{SHARED_WORD}，用中文跟他说话。\n",
                                    encoding="utf-8")
    (kn / "_说明.md").write_text("# 说明\n\n这份是给人看的，不该被当成知识。\n", encoding="utf-8")
    (kn / "baize" / "世界观.md").write_text(f"# 白泽的世界\n\n白泽是瑞兽，{BAIZE_WORD}是它的凭证。\n",
                                            encoding="utf-8")
    (kn / "kaltsit" / "世界观.md").write_text(f"# 罗德岛\n\n{KALTSIT_WORD}在三层，她是医疗主管。\n",
                                              encoding="utf-8")
    (kn / "_worlds" / ARKS / "泰拉.md").write_text(
        f"# 泰拉\n\n{WORLD_WORD}是整个世界观的基础设定。\n", encoding="utf-8")

    personas = tmp / "personas"
    personas.mkdir()
    (personas / "baize.json").write_text(json.dumps({
        "id": "baize", "name": "白泽", "user_title": "阁下",
        "knowledge_shared": True,
        "memory_all": True,          # ★只有默认助手开这个权限★
    }, ensure_ascii=False), encoding="utf-8")
    (personas / "kaltsit.json").write_text(json.dumps({
        "id": "kaltsit", "name": "凯尔希", "user_title": "博士",
        # ★这个角色连共享那份也不看★（全隔离）；但世界观组是它自己挂的，照用
        "knowledge_shared": False,
        "worlds": [ARKS],
    }, ensure_ascii=False), encoding="utf-8")
    (personas / "amiya.json").write_text(json.dumps({
        "id": "amiya", "name": "阿米娅",
        # 用**显式路径 + 内联世界观**的那种写法（不在 data/knowledge 下也能用）
        "knowledge": ["knowledge/共享说明.md"],
        "world": "阿米娅是罗德岛的领袖，年纪不大但很坚定。",
        "knowledge_title": "阿米娅的世界观",
        "worlds": [ARKS],            # ★同一个世界观，不用把设定拷到她名下★
    }, ensure_ascii=False), encoding="utf-8")

    index = tmp / "characters.json"
    index.write_text(json.dumps({
        "default": "baize",
        "characters": [
            {"id": "baize", "file": "personas/baize.json", "default": True},
            {"id": "kaltsit", "file": "personas/kaltsit.json"},
            {"id": "amiya", "file": "personas/amiya.json"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    settings = load_settings()
    settings.skills.data_dir = str(tmp / "data")          # 记忆跟着 data_dir 走（自动隔离）
    settings.memory.knowledge_paths = [str(kn)]           # 绝对路径 → 不吃项目根的相对解析
    settings.memory.world = ""
    settings.memory.llm_summary = False
    # ★角色索引也要指到临时目录★：默认角色、跨角色查询的范围都从它读
    settings.persona.file = str(tmp / "characters.json")
    registry = CharacterRegistry(index)
    return settings, registry, kn


def make_hub(settings, registry) -> MemoryHub:
    return MemoryHub(settings,
                     character_knowledge=lambda cid: knowledge_spec_for(registry, cid),
                     logger=None)


def texts(kb) -> str:
    return "\n".join(c.text for c in kb.chunks())


def test_shared_layer(tmp: Path) -> None:
    print("\n[1] 共享知识库只收顶层文件（★绝不能被角色子目录污染★）")
    settings, registry, _ = make_world(tmp)
    hub = make_hub(settings, registry)
    shared = texts(hub.knowledge)
    check("共享库看得到顶层文件", SHARED_WORD in shared)
    check("共享库看不到任何角色的专属世界观（白泽）", BAIZE_WORD not in shared)
    check("共享库看不到任何角色的专属世界观（凯尔希）", KALTSIT_WORD not in shared)


def test_per_character(tmp: Path) -> None:
    print("\n[2] 目录约定：data/knowledge/<角色id>/ 只有那个角色看得到")
    settings, registry, _ = make_world(tmp)
    hub = make_hub(settings, registry)
    baize = texts(hub.knowledge_for("baize"))
    kaltsit = texts(hub.knowledge_for("kaltsit"))
    check("白泽看得到自己的世界观", BAIZE_WORD in baize)
    check("白泽看得到共享资料（knowledge_shared 默认 true）", SHARED_WORD in baize)
    check("凯尔希看得到自己的世界观", KALTSIT_WORD in kaltsit)
    check("★凯尔希设了 knowledge_shared=false → 看不到共享资料★", SHARED_WORD not in kaltsit)
    check("凯尔希也看不到白泽的（她没全知权限）", BAIZE_WORD not in kaltsit)
    check("★阿米娅（无全知权限）同样看不到凯尔希的★",
          KALTSIT_WORD not in texts(hub.knowledge_for("amiya")), True)
    check("stats 分得清谁是谁（全知角色能看到多个来源）",
          {"local", "local:baize", "local:kaltsit"} <= set(hub.knowledge_for("baize").stats()), True)
    check("★没有全知权限的角色：stats 里只有自己那几层★",
          sorted(hub.knowledge_for("amiya").stats()),
          ["local", "local:amiya", f"local:world:{ARKS}"])


def test_persona_fields(tmp: Path) -> None:
    print("\n[3] 人格文件的字段 + 说明书函数")
    _settings, registry, _ = make_world(tmp)
    raw = json.loads((tmp / "personas" / "amiya.json").read_text(encoding="utf-8"))
    char = Character.from_dict(raw)
    check("knowledge 解析出来了", char.knowledge == ["knowledge/共享说明.md"])
    check("world 解析出来了", "领袖" in char.world)
    check("knowledge_title 解析出来了", char.knowledge_title == "阿米娅的世界观")
    check("knowledge_shared 默认是 True", char.knowledge_shared is True)
    check("memory_all 默认是 False（权限宁严不松）",
          Character.from_dict({"id": "x", "name": "X"}).memory_all is False, True)
    check("memory_all 能解析出来", registry.get("baize").memory_all is True, True)
    check("没写的角色没有这个权限", registry.get("kaltsit").memory_all is False, True)
    check("凯尔希的 knowledge_shared=false 解析出来了",
          registry.get("kaltsit").knowledge_shared is False)

    spec = knowledge_spec_for(registry, "amiya")
    check("说明书：路径", spec["paths"] == ["knowledge/共享说明.md"])
    check("说明书：标题", spec["title"] == "阿米娅的世界观")
    check("说明书：共享开关", spec["shared"] is True)
    check("找不到角色 → 路径为空", knowledge_spec_for(None, "谁")["paths"] == [])


def test_explicit_path_and_inline(tmp: Path) -> None:
    print("\n[4] 显式路径 + 内联世界观（不用建目录也能给角色一份设定）")
    settings, registry, kn = make_world(tmp)
    hub = make_hub(settings, registry)
    amiya = texts(hub.knowledge_for("amiya"))
    check("显式路径的文件读到了", SHARED_WORD in amiya)
    check("内联世界观也在里面", "领袖" in amiya)
    check("共享目录里的顶层文件照样给（她没关共享）", SHARED_WORD in amiya)
    check("白泽的专属词没跑到她那儿", BAIZE_WORD not in amiya)
    # 目录约定 + 显式路径是**叠加**的，不是二选一
    (kn / "amiya").mkdir()
    (kn / "amiya" / "extra.md").write_text("# 补充\n\n阿米娅喜欢在甲板上看星星。\n", encoding="utf-8")
    check("后来新建的角色目录，下一次取就生效（热更新）",
          "甲板" in texts(hub.knowledge_for("amiya")))


def test_recall_isolation(tmp: Path) -> None:
    print("\n[5] 检索层面真的隔离（不是只写在 stats 里）")
    settings, registry, _ = make_world(tmp)
    hub = make_hub(settings, registry)
    baize_hits = hub.for_character("baize").recall(BAIZE_WORD, limit=5)
    kaltsit_hits = hub.for_character("kaltsit").recall(BAIZE_WORD, limit=5)
    check_text("白泽能检索到自己的世界观",
               any("白泽" in texts_of(h) for h in baize_hits),
               str([texts_of(h)[:24] for h in baize_hits]))
    check("★凯尔希检索不到白泽的（隔离生效）★", kaltsit_hits, [])
    check_text("共享资料：开着共享的角色查得到",
               bool(hub.for_character("baize").recall(SHARED_WORD)), "（空）")
    check("共享资料：关了共享的角色查不到", hub.for_character("kaltsit").recall(SHARED_WORD), [])


def texts_of(hit) -> str:
    return str(getattr(hit.item, "text", "") or getattr(hit.item, "title", ""))


# --------------------------------------------------------------------------- #
# MCP 工具
# --------------------------------------------------------------------------- #
def call(server, name: str, args: dict) -> str:
    """走协议调用（和模型走的是同一条路）。"""
    reply = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }) or {}
    result = reply.get("result") or {}
    if "error" in reply:                      # 服务器内部错误也要看得见
        return f"ERROR {reply['error']}"
    parts = [c.get("text", "") for c in (result.get("content") or [])]
    return "\n".join(parts)


def test_mcp_tools(tmp: Path) -> None:
    print("\n[6] 记忆 MCP 工具：recall / remember / memory_stats")
    settings, registry, _ = make_world(tmp)
    hub = make_hub(settings, registry)
    server = mem_server.build_server(settings=settings, hub=lambda: hub,
                                    character=lambda: "baize")

    tools = {t.name for t in server.tools()}
    check_text("三个工具都在（露哪几个由 config.toml 白名单决定）",
               tools == {"recall", "remember", "memory_stats"}, str(sorted(tools)))

    out = call(server, "remember", {"text": "记住：我周五下午三点跟导师见面"})
    check_text("remember 记到当前角色（baize）名下", "baize" in out, out)
    check("真的写进了那个角色的记忆库",
          (tmp / "data" / "memory" / "baize" / "episodes.jsonl").exists(), True)

    out = call(server, "recall", {"query": "导师"})
    check_text("recall 查得到刚记的", "导师" in out, out)

    out = call(server, "recall", {"when": "今天"})
    check_text("★时间检索★：只给时间也能查（今天）", "导师" in out, out)

    out = call(server, "remember", {"text": "凯尔希的私事：她喜欢喝黑咖啡", "who": "kaltsit"})
    check_text("指定 who 能写到别的角色名下", "kaltsit" in out, out)
    out = call(server, "recall", {"query": "咖啡"})
    # ★别用「out 里没出现『咖啡』」当断言★：返回文本的**头部会回显查询词**，
    # 于是它总能匹配到自己（这个自测第一版就这么假失败了一次）。
    # 真正要守的是「凯尔希的私事没被白泽看到」。
    check_text("★默认只看当前角色 → 白泽看不到凯尔希的私事★",
               "她喜欢喝" not in out and "凯尔希" not in out, out)
    out = call(server, "recall", {"query": "黑咖啡", "who": "kaltsit"})
    check_text("显式 who=kaltsit 就查得到", "咖啡" in out, out)

    out = call(server, "recall", {})
    check_text("什么都不给会提示（而不是瞎编）", "要查什么" in out, out)

    out = call(server, "memory_stats", {})
    check_text("memory_stats 报得出四层",
               all(k in out for k in ("事件", "事实", "知识库")), out)
    out = call(server, "recall", {"query": BAIZE_WORD})
    check_text("知识库也能通过 recall 查到（资料那一路）", "白泽" in out, out)

    check_text("服务器里没有的工具会报错（config.toml 白名单在宿主那层再挡一道）",
               "没有这个工具" in call(server, "forget", {"key": "x"}),
               call(server, "forget", {"key": "x"}))


def test_standalone_fallback(tmp: Path) -> None:
    print("\n[7] 独立进程（stdio）那条路：没有宿主注入的 hub 也要能用")
    settings, _registry, _ = make_world(tmp)
    server = mem_server.build_server(settings=settings)      # 没给 hub / character
    out = call(server, "memory_stats", {})
    check_text("自己建 hub 也能报统计", "事件" in out, out)
    check_text("默认角色来自角色索引（白泽，不是字面量 default）", "baize" in out, out)
    out = call(server, "remember", {"text": "记住：独立进程也能记东西"})
    check_text("独立进程也能写（落在临时数据目录）", "baize" in out, out)
    real = ROOT / "data" / "memory" / "baize" / "episodes.jsonl"
    check_text("★真实记忆库没多出这条（隔离没漏）★",
               "独立进程也能记东西" not in (real.read_text(encoding="utf-8", errors="replace")
                                        if real.exists() else ""),
               f"{real} 里有这条 → 隔离漏了")


def test_cross_character(tmp: Path) -> None:
    print("\n[8] ★跨角色查全部记忆★（白泽的权限）+ 来源必须标清楚")
    settings, registry, _ = make_world(tmp)
    hub = make_hub(settings, registry)
    baize = hub.for_character("baize")
    kaltsit = hub.for_character("kaltsit")
    baize.remember("记住：我周五下午跟导师见面")
    kaltsit.remember("记住：博士下周要交体检报告")

    pairs = hub.recall_everywhere("导师", characters=["baize", "kaltsit"])
    check_text("hub 层：跨角色检索能查到", bool(pairs) and pairs[0][0] == "baize",
               str([(cid, h.text[:16]) for cid, h in pairs]))
    pairs = hub.recall_everywhere("体检报告", characters=["baize", "kaltsit"])
    check("★每条都带着它属于谁（不是混在一起）★", [cid for cid, _ in pairs], ["kaltsit"])
    # ★话题完全不通的不能靠「新鲜+重要」混进来★（打分是加性的，第一版就撞上了）
    check("话题不重合的查询：白泽那边一无所获", hub.for_character("baize").recall("体检报告"), [])
    check("同一个角色自己的话题照常查得到",
          bool(hub.for_character("baize").recall("导师")), True)
    check("只给时间（不给内容）仍然能查：今天",
          bool(hub.for_character("baize").recall("", when="今天")), True)

    # 服务器：不给权限回调 → 自己读人格文件（完全按生产路径走）
    server = mem_server.build_server(settings=settings, hub=lambda: hub,
                                     character=lambda: "baize",
                                     character_label=lambda cid: registry.get(cid).name
                                     if registry.get(cid) else cid)
    out = call(server, "recall", {"query": "体检报告", "who": "*"})
    check_text("白泽用 who=* 能查全部", "体检报告" in out, out)
    check_text("★并且标出了是谁记的（凯尔希）★", "凯尔希" in out, out)
    check_text("头部也说了这是跨角色查询", "跨角色" in out, out)

    # 凯尔希：没权限 → 直接拒（这才叫权限）
    server2 = mem_server.build_server(settings=settings, hub=lambda: hub,
                                      character=lambda: "kaltsit",
                                      character_label=lambda cid: registry.get(cid).name
                                      if registry.get(cid) else cid)
    out = call(server2, "recall", {"query": "导师", "who": "*"})
    check_text("★没权限的角色跨角色查询被拒★", "权限" in out and "导师" not in out, out)
    out = call(server2, "recall", {"query": "导师"})
    check_text("但查自己的照常能用", "没有相关记忆" in out or "导师" in out, out)

    out = call(server, "recall", {"query": "导师", "who": "全部角色"})
    check_text("中文写法『全部角色』也认", "导师" in out, out)


def test_shared_world(tmp: Path) -> None:
    print("\n[9] ★同一个 IP 的角色共享世界观★（一份文件，不用拷到每个人名下）")
    settings, registry, kn = make_world(tmp)
    hub = make_hub(settings, registry)

    kaltsit = texts(hub.knowledge_for("kaltsit"))
    amiya = texts(hub.knowledge_for("amiya"))
    baize = texts(hub.knowledge_for("baize"))
    check_text("凯尔希拿到世界观（她挂了明日方舟）", WORLD_WORD in kaltsit, kaltsit[:80])
    check_text("★阿米娅也拿到同一份★（两人共享一份文件）", WORLD_WORD in amiya, amiya[:80])
    check_text("★白泽没挂，但它是全知角色 → 也看得到★", WORLD_WORD in baize, baize[:80])
    check("★差别在归属标注：白泽看到的带着「谁的世界观」★",
          [c.owner for c in hub.knowledge_for("baize").chunks() if WORLD_WORD in c.text],
          [f"{ARKS}（世界观）"])
    check("共享一份文件：两人看到的是同一个来源",
          [c.source for c in hub.knowledge_for("kaltsit").chunks() if WORLD_WORD in c.text]
          == [c.source for c in hub.knowledge_for("amiya").chunks() if WORLD_WORD in c.text], True)

    check("stats 里能看出这是世界观那一层",
          [k for k in hub.knowledge_for("kaltsit").stats() if k.startswith("local:world:")],
          [f"local:world:{ARKS}"])
    check("凯尔希做到了三层叠加（世界观 + 自己 + 关掉了共享）",
          ("local:world:" + ARKS in hub.knowledge_for("kaltsit").stats()
           and "local:kaltsit" in hub.knowledge_for("kaltsit").stats()
           and "local" not in hub.knowledge_for("kaltsit").stats()), True)
    check("凯尔希检索得到世界观里的词", bool(hub.for_character("kaltsit").recall(WORLD_WORD)), True)
    check("★白泽（全知）检索得到，且带着归属★",
          [h.item.owner for h in hub.for_character("baize").recall(WORLD_WORD, limit=3)
           if h.kind == "chunk" and WORLD_WORD in h.item.text][:1], [f"{ARKS}（世界观）"])
    check("★阿米娅没有全知权限 → 只拿得到自己挂的那份★",
          [h.item.owner for h in hub.for_character("amiya").recall("白泽纹章", limit=3)], [])

    # 挂两个世界观 + 世界观目录可以后建（下一次取就生效）
    (kn / "_worlds" / "联动").mkdir()
    (kn / "_worlds" / "联动" / "活动.md").write_text("# 联动\n\n这次联动有共同剧情。\n", encoding="utf-8")
    raw = json.loads((tmp / "personas" / "amiya.json").read_text(encoding="utf-8"))
    raw["worlds"] = [ARKS, "联动"]
    (tmp / "personas" / "amiya.json").write_text(json.dumps(raw, ensure_ascii=False),
                                                encoding="utf-8")
    # ★人格文件的改动要先热加载★（生产里是 pipeline 每轮调 maybe_reload；
    #   hub 每次取知识库都会重读「说明书」，所以热加载之后下一句话就生效）
    registry.maybe_reload()
    amiya2 = texts(hub.knowledge_for("amiya"))
    check_text("★可以挂多个世界观★（后加的那个下一句话就生效）", "共同剧情" in amiya2, amiya2[:80])
    check_text("凯尔希没挂那个 → 看不到", "共同剧情" not in texts(hub.knowledge_for("kaltsit")),
               texts(hub.knowledge_for("kaltsit"))[:80])

    check("下划线开头的说明文件不当知识（顶层）",
          "这份是给人看的" not in texts(hub.knowledge_for("baize")), True)
    check("下划线开头的目录不当知识（_worlds 不会当成角色）",
          "_worlds" not in [k for k in hub.knowledge_for("baize").stats()], True)


def test_knowledge_all_switch(tmp: Path) -> None:
    print("\n[10] ★全知默认开、可以特意关★ + 非全知角色仍然隔离")
    settings, registry, _ = make_world(tmp)

    # 默认：memory_all=true 的角色，knowledge_all 跟着走（白泽就是这种）
    raw = json.loads((tmp / "personas" / "baize.json").read_text(encoding="utf-8"))
    check("没写 knowledge_all 时跟随 memory_all",
          Character.from_dict(raw).knowledge_all, None)
    check("说明书里算出来的 all = True", knowledge_spec_for(registry, "baize")["all"], True)
    check("非全知角色：all = False", knowledge_spec_for(registry, "kaltsit")["all"], False)

    hub = make_hub(settings, registry)
    check("白泽看得到凯尔希的专属资料（全知）",
          KALTSIT_WORD in texts(hub.knowledge_for("baize")), True)
    check("带上了归属标注",
          [c.owner for c in hub.knowledge_for("baize").chunks() if KALTSIT_WORD in c.text],
          ["凯尔希 的专属资料"])
    check("stats 里能看到别人的那层",
          "local:kaltsit" in hub.knowledge_for("baize").stats(), True)

    # ★特意关掉★：还能跨角色查记忆，但不看别人的世界观/资料
    raw["knowledge_all"] = False
    (tmp / "personas" / "baize.json").write_text(json.dumps(raw, ensure_ascii=False),
                                                 encoding="utf-8")
    registry.maybe_reload()
    hub2 = make_hub(settings, registry)
    spec = knowledge_spec_for(registry, "baize")
    check("关掉之后 all = False", spec["all"], False)
    check("跨角色查记忆的权限不受影响（memory_all 还在）", spec["paths"] is not None and
          can_read_all_memory(registry, "baize"), True)
    check_text("★看不到别人的专属资料了★",
               KALTSIT_WORD not in texts(hub2.knowledge_for("baize")),
               texts(hub2.knowledge_for("baize"))[:80])
    check("自己挂的世界观还在", WORLD_WORD in texts(hub2.knowledge_for("kaltsit")), True)


def test_no_imitation_prompt(tmp: Path) -> None:
    print("\n[11] ★不代入：全知角色的系统提示里要有「旁观」要求★")
    _settings, registry, _ = make_world(tmp)
    baize = registry.get("baize")
    prompt = render_system_prompt(baize)
    check_text("写了「不要说成自己的身份」", "不要说成自己的身份" in prompt, prompt[-200:])
    check_text("写了要旁观（据我所知）", "据我所知" in prompt, prompt[-200:])
    check("全知角色才有这一段（凯尔希没有）",
          "不要说成自己的身份" not in render_system_prompt(registry.get("kaltsit")), True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="memory_mcp_"))
    try:
        test_shared_layer(tmp / "a")
        test_per_character(tmp / "b")
        test_persona_fields(tmp / "c")
        test_explicit_path_and_inline(tmp / "d")
        test_recall_isolation(tmp / "e")
        test_mcp_tools(tmp / "f")
        test_standalone_fallback(tmp / "g")
        test_cross_character(tmp / "h")
        test_shared_world(tmp / "i")
        test_knowledge_all_switch(tmp / "j")
        test_no_imitation_prompt(tmp / "k")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 记忆知识库 + MCP 工具自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
