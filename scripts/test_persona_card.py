"""角色资料卡自测：表单规范 / 校验 / 落盘 / 导入 / 接口。

★为什么值得一条条钉住★：这个功能会**写人格文件 + 改索引**。
人格文件的解析是白名单式的（字段名写错不会报错，只会静默忽略），
索引写坏则会让「所有角色一起消失」。所以这里全程用临时目录当项目根，
最后还有一道闸门：跑完比对真实仓库那两份文件的字节数（§48.3 那个教训）。

    python scripts\\test_persona_card.py
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.console import persona_card as card   # noqa: E402
from voice_loop.persona import CharacterRegistry      # noqa: E402
from voice_loop.settings import load_settings         # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()

GOOD = {"id": "shining", "name": "临光", "title": "罗德岛干员",
        "background": "我来自卡西米尔。", "user_title": "博士",
        "wake_words": ["临光"], "ack": "在。",
        "aliases": {"临光": ["邻光"]},
        "style": ["句子短"], "rules": ["不要编造"], "avoid": ["作为一个AI"],
        "lines": [{"scene": "打招呼", "text": "在。"}],
        "temperature": 0.5, "backend": "piper", "notes": "自测用"}


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def _posix(path: str) -> str:
    """Windows 的绝对路径是反斜杠 —— 比断言前先规整成 posix。"""
    return Path(str(path)).as_posix()


def setup(tmp: Path):
    """沙盒：临时索引 + 一个已存在的角色（用来验「不碰别人」）。"""
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "keeper.json").write_text(json.dumps(
        {"id": "keeper", "name": "守卫", "wake_words": ["守卫"], "notes": "别动我"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp / "characters.json").write_text(json.dumps({
        "_说明": "沙盒索引",
        "default": "keeper",
        "characters": [{"id": "keeper", "file": "personas/keeper.json", "enabled": True}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    settings = load_settings()
    settings.persona.file = str(tmp / "characters.json")
    return settings


# --------------------------------------------------------------------------- #
def test_spec() -> None:
    print("\n[1] 表单规范：唯一真相源自洽")
    spec = card.groups()
    check("有分组", len(spec) >= 4, True, detail=f"{len(spec)} 组")
    kinds = {"text", "textarea", "list", "json", "number", "bool", "select"}
    flat = [f for g in spec for f in g["fields"]]
    check("每栏都有 key/label/kind", all(f.get("key") and f.get("label") and f.get("kind") for f in flat), True)
    check("kind 都在白名单里", sorted({f["kind"] for f in flat} - kinds), [])
    check("key 不重复", len({f["key"] for f in flat}), len(flat))
    check("必填只有 id 和名字等少数几栏",
          sorted(f["key"] for f in flat if f.get("required")), ["id", "name", "wake_words"])
    ex = card.example()
    check("模板覆盖所有栏目", sorted(ex) , sorted(card.KNOWN_KEYS))
    check("模板本身能过校验", card.parse(ex)[1], [])
    check("★模板里没有「问答式台词」这种坑★",
          all(ln.get("scene") and ln.get("text") for ln in ex["lines"]), True)
    # 拿真角色当样板
    real = CharacterRegistry(ROOT / "data" / "characters.json").get("kaltsit")
    got = card.example(real)
    check("能从真角色抄一份样板", got["name"], "凯尔希")
    # ★编辑时用的是同一份映射★（fields_of 就是 example 的同义名，只有一份实现）
    same = card.fields_of(real)
    check("编辑表单字段 = SPEC 全集", sorted(same), sorted(card.KNOWN_KEYS))
    check("字段里带着她当前的值", (same["id"], same["name"], same["wake_words"]),
          ("kaltsit", "凯尔希", got["wake_words"]))


def test_parse() -> None:
    print("\n[2] 校验：该拦的拦、该提醒的提醒")
    fields, problems, warnings = card.parse(GOOD)
    check("正常卡没问题", (problems, warnings), ([], []))
    check("list 支持按行字符串", card.parse({**GOOD, "style": "句子短\n不要绕弯"})[0]["style"],
          ["句子短", "不要绕弯"])
    check("空行的 list 项被丢掉", card.parse({**GOOD, "style": ["", "  ", "有用"]})[0]["style"],
          ["有用"])
    check("★id 不能当路径用★（防着写到目录外面）",
          bool(card.parse({**GOOD, "id": "../evil"})[1]), True)
    check("id 必须小写", bool(card.parse({**GOOD, "id": "Shining"})[1]), True)
    check("名字不能空", bool(card.parse({**GOOD, "name": "  "})[1]), True)
    check("json 写错会拦", bool(card.parse({**GOOD, "aliases": "{不是 json"})[1]), True)
    check("aliases 给数组会拦（该是对象）", bool(card.parse({**GOOD, "aliases": "[1,2]"})[1]), True)
    check("温度不是数字会拦", bool(card.parse({**GOOD, "temperature": "很热"})[1]), True)
    check("温度超出范围会拦", bool(card.parse({**GOOD, "temperature": 5})[1]), True)
    check("backend 只认三个值", bool(card.parse({**GOOD, "backend": "gpt"})[1]), True)
    check("backend 留空是对的", card.parse({**GOOD, "backend": ""})[1], [])
    check("唤醒词为空只是提醒（还能手动切）",
          card.parse({**GOOD, "wake_words": []})[2][0].startswith("唤醒词为空"), True)
    check("★写了人格文件不认识的字段会提醒★（否则会被静默忽略）",
          any("不认识" in w for w in card.parse({**GOOD, "favorit_food": "胡萝卜"})[2]), True)
    check("参考音频不是 wav 会提醒",
          any("wav" in w for w in card.parse({**GOOD, "voice_ref": "a.mp3"})[2]), True)


def test_preview(tmp: Path) -> None:
    print("\n[3] 预览：写出去的就是读得回来的")
    settings = setup(tmp)
    fields, _, _ = card.parse({**GOOD, "unknown_thing": "x"})
    got = card.preview(settings, fields)
    check("不认识的字段没进 JSON", "unknown_thing" in got, False)
    check("id/name 排在最前面", list(got)[:2], ["id", "name"])
    check("★能被真解析器读回来★", CharacterRegistry(tmp / "characters.json") is not None, True)
    from voice_loop.persona import Character  # noqa: PLC0415

    char = Character.from_dict(got)
    check("读回来名字/唤醒词/风格都对",
          (char.name, char.wake_words, char.style), ("临光", ["临光"], ["句子短"]))
    check("lines 也对", char.lines[0]["text"], "在。")
    check("空的字段不会写成空串占位（to_dict 会剔掉）", "knowledge" in got, False)


def test_create(tmp: Path) -> None:
    print("\n[4] 落盘：写两个文件、留备份、不碰别人")
    settings = setup(tmp)
    keeper_before = (tmp / "personas" / "keeper.json").read_bytes()
    index_before = json.loads((tmp / "characters.json").read_text(encoding="utf-8"))

    plan = card.create(settings, GOOD, dry_run=True)
    check("试运行不写文件", (tmp / "personas" / "shining.json").exists(), False)
    check("试运行也会说要做什么",
          (_posix(plan["file_rel"]).endswith("personas/shining.json"), plan["added_index_row"]),
          (True, True))

    got = card.create(settings, GOOD, dry_run=False)
    target = tmp / "personas" / "shining.json"
    saved = json.loads(target.read_text(encoding="utf-8"))
    check("资料卡写进去了", saved["name"], "临光")
    check("索引里加了一行",
          [c["id"] for c in json.loads((tmp / "characters.json").read_text(encoding="utf-8"))["characters"]],
          ["keeper", "shining"])
    check("★别的人格文件一个字节没动★", (tmp / "personas" / "keeper.json").read_bytes(), keeper_before)
    check("索引原有行没被重排", json.loads((tmp / "characters.json").read_text(
        encoding="utf-8"))["default"], index_before["default"])
    check("报告里给了路径（沙盒在项目外 → 绝对路径是对的）",
          _posix(got["file_rel"]).endswith("personas/shining.json"), True)
    check("★真的能被加载★",
          [c.id for c in CharacterRegistry(tmp / "characters.json").all()], ["keeper", "shining"])

    try:
        card.create(settings, GOOD, dry_run=False)
        check("重名会被挡住", False)
    except ValueError as exc:
        check("重名会被挡住（要显式勾覆盖）", "已经有" in str(exc), True)

    again = card.create(settings, {**GOOD, "title": "改写过的身份"},
                        overwrite=True, dry_run=False)
    check("覆盖时才改，并留了备份",
          (json.loads(target.read_text(encoding="utf-8"))["title"], Path(again["backup"]).is_file()),
          ("改写过的身份", True))
    check("覆盖不会把索引再加一行",
          len(json.loads((tmp / "characters.json").read_text(encoding="utf-8"))["characters"]), 2)
    check("坏输入（id 非法）直接报错、什么都不写",
          _raises(lambda: card.create(settings, {**GOOD, "id": "../x"}, dry_run=False)), True)


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def test_import() -> None:
    print("\n[5] 导入：认 .json 和素材清单 .txt")
    fields, problems, note = card.read_import("shining.json", json.dumps(GOOD, ensure_ascii=False))
    check("json 资料卡读得出来", (problems, fields["name"], note), ([], "临光", ""))
    check("读出来的能直接过校验", card.parse(fields)[1], [])
    arr, _, note2 = card.read_import("two.json", json.dumps([GOOD, {**GOOD, "id": "b"}], ensure_ascii=False))
    check("数组只取第一条并说明", (arr["id"], "数组" in note2), ("shining", True))
    idx, _, note3 = card.read_import("characters.json", json.dumps(
        {"default": "x", "characters": [{"id": "keeper", "file": "personas/keeper.json"}]}, ensure_ascii=False))
    check("索引文件取第一个角色并说明", (idx["id"], "索引" in note3), ("keeper", True))
    check("没写 id 就按文件名推（并说明）",
          card.read_import("newbie.json", json.dumps({"name": "新人"}, ensure_ascii=False))[0]["id"], "newbie")
    check("坏 json 给一句人话",
          "JSON" in card.read_import("x.json", "{不是")[1][0], True)

    manifest = "打招呼\n在，博士。\n\n催促\n文件在这里。\n"
    got, problems, note = card.read_import("shining.txt", manifest)
    check("素材清单 → 示例台词", [(ln["scene"], ln["text"]) for ln in got["lines"]],
          [("打招呼", "在，博士。"), ("催促", "文件在这里。")])
    check("按文件名推 id/name", (got["id"], got["name"]), ("shining", "shining"))
    check("说明了「只填了这一栏」", "示例台词" in note, True)
    plain, plain_problems, _ = card.read_import("x.txt", "就一句话没有任何名字行。")
    check("没有名字行的 txt 当成一段台词（不是报错）",
          (plain_problems, [(ln["scene"], ln["text"]) for ln in plain["lines"]]),
          ([], [("随意对话", "就一句话没有任何名字行。")]))
    check("空文件才给提示", bool(card.read_import("x.txt", "   \n")[1]), True)


def test_endpoints(tmp: Path) -> None:
    print("\n[6] 接口：真走 FastAPI 路由")
    settings = setup(tmp)
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001
        print(f"    · 没装 fastapi/httpx，跳过（{exc}）")
        return
    from voice_loop.console.panels import persona as panel  # noqa: PLC0415

    class Ctx:
        root = tmp

        def __init__(self) -> None:
            self.settings = settings

        def characters(self):
            return CharacterRegistry(tmp / "characters.json")

    app = FastAPI()
    panel.register(app, Ctx())
    client = TestClient(app)

    spec = client.get("/api/persona/spec")
    check("GET spec", (spec.status_code, len(spec.json()["groups"]) > 0), (200, True))
    check("spec 里带了现有 id", spec.json()["ids"], ["keeper"])

    prev = client.post("/api/persona/preview", json={"fields": GOOD})
    check("POST preview 给 JSON 与目标路径",
          (prev.status_code, _posix(prev.json()["target"]).endswith("personas/shining.json")),
          (200, True))
    bad = client.post("/api/persona/preview", json={"fields": {**GOOD, "id": "Bad Id"}})
    check("坏 id 走 200 + problems（预览不报错）", len(bad.json()["problems"]), 1)

    made = client.post("/api/persona/create", json={"fields": GOOD, "apply": True})
    check("POST create 真落盘", (made.status_code, made.json()["added_index_row"]), (200, True))
    check("文件在", (tmp / "personas" / "shining.json").is_file(), True)
    clash = client.post("/api/persona/create", json={"fields": GOOD, "apply": True})
    check("重名给 400 + 人话", (clash.status_code, "已经有" in clash.json()["detail"]), (400, True))

    imp = client.post("/api/persona/import",
                      json={"filename": "shining.txt", "data": _b64("打招呼\n在。\n")})
    check("POST import 回填字段、不落盘", (imp.status_code, len(imp.json()["fields"]["lines"])), (200, 1))
    exp = client.get("/api/persona/export", params={"cid": "keeper"})
    check("GET export 导出人格内容", (exp.status_code, exp.json()["json"]["name"]), (200, "守卫"))

    # ★编辑★：读回表单字段 → 改一栏 → 写回
    got = client.get("/api/persona/get", params={"cid": "keeper"})
    body = got.json()
    check("GET get 回表单字段（形状与建角色一致）",
          (got.status_code, sorted(body["fields"]), body["fields"]["name"]),
          (200, sorted(card.KNOWN_KEYS), "守卫"))
    check("GET get 报告改的是哪个文件", _posix(body["file"]).endswith("personas/keeper.json"), True)
    check("GET get 没这个角色 → 404", client.get("/api/persona/get", params={"cid": "无"}).status_code, 404)
    fields = dict(body["fields"])
    fields["ack"] = "在的"
    upd = client.post("/api/persona/update", json={"fields": fields, "apply": True})
    check("POST update 真改一栏", (upd.status_code, upd.json()["changed"]), (200, ["ack"]))
    check("改完还是能读回来的角色",
          json.loads((tmp / "personas" / "keeper.json").read_text(encoding="utf-8"))["ack"], "在的")
    check("改不存在的人 → 400 + 人话",
          (lambda r: (r.status_code, "创建" in r.json()["detail"]))(
              client.post("/api/persona/update", json={"fields": {**GOOD, "id": "nobody"}, "apply": True})),
          (400, True))
    check("导出不存在的角色 → 404", client.get("/api/persona/export", params={"cid": "无"}).status_code, 404)
    over = client.post("/api/persona/import", json={"filename": "x.json", "data": _b64("x" * 10)})
    check("不是 JSON 的文件给 200 + problems", len(over.json()["problems"]), 1)


def test_update(tmp: Path) -> None:
    print("\n[8] 修改已有角色：只动表单那几栏（★不是整份覆盖★）")
    settings = setup(tmp)
    card.create(settings, GOOD, dry_run=False)
    target = tmp / "personas" / "shining.json"
    # 塞进「表单管不着」的键：索引级的 + 将来新加的字段。它们必须活下来。
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw.update({"enabled": False, "voice_dir": "data/personas/shining",
                "world": "卡西米尔", "future_field": "以后才有的字段"})
    target.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    index_before = (tmp / "characters.json").read_bytes()

    plan = card.update(settings, {**GOOD, "title": "改过的身份"}, dry_run=True)
    check("试运行不写文件", json.loads(target.read_text(encoding="utf-8"))["title"], "罗德岛干员")
    check("试运行也说改了哪几栏", plan["changed"], ["title"])

    got = card.update(settings, {**GOOD, "title": "改过的身份", "style": []}, dry_run=False)
    saved = json.loads(target.read_text(encoding="utf-8"))
    check("改动写进去了", saved["title"], "改过的身份")
    check("★表单里清空的栏真的被清掉★", "style" in saved, False)
    check("报告了改动 / 被清掉的键", (got["changed"], got["removed"]), (["title"], ["style"]))
    check("★enabled / voice_dir / world / 不认识的键都原样保住★",
          (saved.get("enabled"), saved.get("voice_dir"), saved.get("world"), saved.get("future_field")),
          (False, "data/personas/shining", "卡西米尔", "以后才有的字段"))
    check("索引一个字节没动（改角色不动索引）", (tmp / "characters.json").read_bytes(), index_before)
    check("留了备份", Path(got["backup"]).is_file(), True)
    check("★改完能被真解析器读出新的值★",
          CharacterRegistry(tmp / "characters.json").get("shining").title, "改过的身份")
    check("没真改动时如实报「内容没变」",
          card.update(settings, {**GOOD, "title": "改过的身份", "style": []})["changed"], [])
    check("改别人的角色不误伤",
          json.loads((tmp / "personas" / "keeper.json").read_text(encoding="utf-8"))["notes"], "别动我")
    # ★归一化才是真相★：解析器有默认值（user_title 缺省是「你」），
    # 表单里空着的栏跟「文件里压根没这个键」是同一件事 —— 不该报成改动，更不该写进文件。
    keeper = tmp / "personas" / "keeper.json"
    kf = dict(card.fields_of(CharacterRegistry(tmp / "characters.json").get("keeper")))
    kf["notes"] = "别动我（改过）"
    rep = card.update(settings, kf, dry_run=False)
    check("★空着的栏不算改动（否则会刷一片假的 user_title）★", rep["changed"], ["notes"])
    check("★解析器的默认值不会被顺手写进她的文件★",
          "user_title" in json.loads(keeper.read_text(encoding="utf-8")), False)
    check("改了 notes、没动唤醒词",
          (json.loads(keeper.read_text(encoding="utf-8"))["notes"],
           json.loads(keeper.read_text(encoding="utf-8"))["wake_words"]),
          ("别动我（改过）", ["守卫"]))
    try:
        card.update(settings, {**GOOD, "id": "nobody"})
        check("★编辑不会偷偷建角色★", False)
    except ValueError as exc:
        check("★编辑不会偷偷建角色（并指路去「创建」）★", "创建" in str(exc), True)
    check("坏输入（id 非法）什么都不写",
          _raises(lambda: card.update(settings, {**GOOD, "id": "../x"})), True)


def test_gate(tmp: Path) -> None:
    print("\n[7] 闸门：真实仓库没被动过")
    watched = [ROOT / "data" / "characters.json",
               ROOT / "data" / "personas" / "kaltsit.json",
               ROOT / "data" / "personas" / "baize.json"]
    before = {p: (p.stat().st_size if p.is_file() else -1) for p in watched}
    settings = setup(tmp)                       # 沙盒（settings 指向 tmp 的索引）
    card.create(settings, GOOD, dry_run=False)
    after = {p: (p.stat().st_size if p.is_file() else -1) for p in watched}
    for path, size in before.items():
        check(f"{path.relative_to(ROOT).as_posix()} 字节数没变", after[path], size)
    check("沙盒里真的写了（不是整个测试空跑）",
          (tmp / "personas" / "shining.json").is_file(), True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="persona_card_"))
    try:
        test_spec()
        test_parse()
        test_preview(tmp / "a")
        test_create(tmp / "b")
        test_import()
        test_endpoints(tmp / "c")
        test_update(tmp / "e")
        test_gate(tmp / "d")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 角色资料卡自测全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
