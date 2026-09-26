"""角色设定的自测：结构化人设 / 多角色唤醒归属 / 切换 / 热重载 / 容错。

跑法：
    python scripts/test_persona.py

它在临时目录里造角色文件，不碰你自己的 data/characters.json。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.persona import (  # noqa: E402
    SPEECH_RULES,
    Character,
    CharacterRegistry,
    render_system_prompt,
)
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.wake import WakeWordMatcher  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def write_chars(path: Path, chars: list[dict]) -> None:
    path.write_text(
        json.dumps({"characters": chars}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sample() -> list[dict]:
    return [
        {
            "id": "kaltsit",
            "name": "凯尔希",
            "title": "罗德岛医疗主管",
            "background": "冷静的医疗主管。",
            "user_title": "博士",
            "wake_words": ["凯尔希"],
            "aliases": {"凯尔希": ["凯尔西", "老猫"]},
            "ack": "我在，博士。",
            "style": ["冷静、克制"],
            "rules": ["不知道就直说"],
            "avoid": ["不要用「作为一个AI」"],
            "lines": [{"scene": "被唤醒", "text": "我在，博士。"}],
            "default": True,
        },
        {
            "id": "amiya",
            "name": "阿米娅",
            "title": "罗德岛公开领袖",
            "background": "年轻的领袖。",
            "user_title": "博士",
            "wake_words": ["阿米娅", "阿米娅小姐"],
            "aliases": {"阿米娅": ["阿米亚"]},
            "ack": "博士，我在。",
            "style": ["真诚、有条理"],
            "lines": [{"scene": "被唤醒", "text": "博士，我在。"}],
        },
    ]


# --------------------------------------------------------------------------- #
def test_registry(tmp: Path) -> CharacterRegistry:
    print("\n[1] 角色文件：加载 / 默认 / 统计 / 容错")
    path = tmp / "characters.json"
    write_chars(path, sample())
    reg = CharacterRegistry(path)

    check("读到两个角色", len(reg.all()) == 2, str([c.name for c in reg.all()]))
    check("默认角色是写了 default 的那个", (reg.default() or Character("", "")).id == "kaltsit",
          (reg.default() or Character("", "")).name)
    check("按 id 找得到", (reg.get("amiya") or Character("", "")).name == "阿米娅")
    check("按名字也找得到（大小写不敏感）", (reg.get("阿米娅") or Character("", "")).id == "amiya")
    check("找不到返回 None", reg.get("不存在的人") is None)
    check("统计一行能看明白", "2/2" in reg.stats(), reg.stats())
    check("唤醒词映射含别名", {"凯尔希": "kaltsit", "老猫": "kaltsit", "阿米亚": "amiya"}
          == {k: reg.wake_map()[k] for k in ("凯尔希", "老猫", "阿米亚")})

    # 别名也算这个角色的
    check("别名归到主唤醒词名下", set(reg.get("kaltsit").all_wake_words()) >= {"凯尔希", "老猫", "凯尔西"})

    # 停用的角色不参与唤醒，但设定还在
    write_chars(path, [{**sample()[0]}, {**sample()[1], "enabled": False}])
    reg2 = CharacterRegistry(path)
    check("停用的角色不参与唤醒", "阿米娅" not in reg2.wake_map())
    check("停用的角色设定还留着", reg2.get("amiya") is not None)

    # 坏 JSON / 重复 id / 缺字段都不能崩
    path.write_text("{ 这不是 json", encoding="utf-8")
    reg3 = CharacterRegistry(path)
    check("坏 JSON 不崩，只记一条警告", any("解析失败" in w for w in reg3.warnings)
          or reg3.characters == [], str(reg3.warnings)[:60])
    write_chars(path, [{**sample()[0]}, {"id": "x"}, {"name": "没写 id 但有名字"},
                       {**sample()[0]}])
    reg4 = CharacterRegistry(path)
    check("重复 id 只留一个", len([c for c in reg4.all() if c.id == "kaltsit"]) == 1)
    check("只有 name 也能当 id 用", any(c.name == "没写 id 但有名字" for c in reg4.all()))
    return reg


def test_render() -> None:
    print("\n[2] 拼 system prompt：字段都要进得去")
    char = Character.from_dict(sample()[0])
    prompt = render_system_prompt(char, extra="今天是很忙的一天。")

    for piece in (char.name, char.title, char.background, char.user_title,
                  char.style[0], char.rules[0], char.avoid[0], char.lines[0]["text"]):
        check(f"提示词里有「{piece[:12]}」", piece in prompt)
    check("带上了「输出要朗读」的硬要求", SPEECH_RULES[0] in prompt)
    check("要求用第一人称、别自我介绍", "第一人称" in prompt and "自我介绍" in prompt)
    check("示例台词标了「不要原样重复」", "不要原样重复" in prompt)
    check("附加要求也拼进去了", "今天是很忙的一天。" in prompt)
    check("换个人称，提示词跟着换",
          "指挥官" not in prompt
          and "指挥官" in render_system_prompt(
              Character.from_dict({**sample()[0], "user_title": "指挥官"})))
    # 字段留空也不能拼出奇怪的提示词
    slim = render_system_prompt(Character(id="a", name="甲"))
    check("字段留空也能拼出干净提示词",
          "你是《明日方舟》里的甲。" in slim and "\n\n\n" not in slim, slim[:40].replace("\n", "|"))


def test_wake_attribution(tmp: Path) -> None:
    print("\n[3] 多角色唤醒：喊谁就是谁")
    path = tmp / "characters.json"
    write_chars(path, sample())
    reg = CharacterRegistry(path)

    wake_path = tmp / "wakewords.json"
    wake_path.write_text(json.dumps({"enabled": True, "words": ["凯尔希"],
                                     "min_silence": 0.3, "fuzzy_ratio": 0.75}),
                         encoding="utf-8")
    matcher = WakeWordMatcher(wake_path)
    matcher.set_characters(reg.all(only_enabled=True))

    cases = [
        ("凯尔希", "kaltsit"),
        ("凯尔西，现在几点了", "kaltsit"),      # 别名
        ("老猫", "kaltsit"),                    # 别名（外号）
        ("阿米娅", "amiya"),
        ("阿米亚，帮我看看日程", "amiya"),
    ]
    for text, want in cases:
        hit = matcher.match(text)
        check(f"「{text}」→ {want}", hit is not None and hit.character == want,
              f"命中={hit.word if hit else None} 角色={hit.character if hit else None}")

    check("唤醒提示会列出各自的唤醒词", matcher.character_words[:2] == ["凯尔希", "阿米娅"],
          str(matcher.character_words))
    check("确实是在按角色匹配", matcher.characters_loaded())

    # 去掉角色后回到旧行为（只有 wakewords.json 的 凯尔希，且无归属）
    matcher.set_characters([])
    hit = matcher.match("阿米娅")
    check("没有角色时阿米娅不该被唤醒", hit is None or hit.character == "",
          str(hit)[:50])

    # 模糊匹配也要带上归属。★注意挑一个真正的模糊例子★：
    # 3 字唤醒词的 2/3 同字相似度恒为 0.667，低于 0.75 阈值，所以「凯尔戏」
    # 那种靠别名覆盖，而模糊那一路要用长唤醒词（阿米娅小姐 → 阿米雅小姐 = 0.8）
    matcher.set_characters(reg.all(only_enabled=True))
    hit = matcher.match("阿米雅小姐")
    check("模糊命中也有归属", hit is not None and hit.fuzzy and hit.character == "amiya",
          f"命中={hit.word if hit else None} 角色={hit.character if hit else None} "
          f"模糊={hit.fuzzy if hit else None}")


def test_pipeline(tmp: Path) -> None:
    print("\n[4] 接进 pipeline：默认角色 / 切换 / 应答语 / 声线兜底")
    chars_path = tmp / "characters.json"
    write_chars(chars_path, sample())

    settings = load_settings()
    settings.persona.file = str(chars_path)
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.wake.file = str(tmp / "wakewords.json")
    (tmp / "wakewords.json").write_text(
        json.dumps({"enabled": True, "words": ["凯尔希"], "idle_timeout": 30}),
        encoding="utf-8",
    )
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "m.json")

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        check("启动时装的是默认角色", loop.character is not None and loop.character.id == "kaltsit",
              getattr(loop.character, "name", None))
        check("system prompt 来自角色（不是 config.toml 那段）",
              "凯尔希" in (loop.llm.system_prompt or "")
              and "医疗主管" in (loop.llm.system_prompt or ""))
        check("应答语跟着角色走", loop.wake.settings.ack == "我在，博士。",
              loop.wake.settings.ack)

        # 喊另一个名字 → 切人设
        loop._switch_character("amiya")  # noqa: SLF001
        check("切到阿米娅后提示词换了",
              "阿米娅" in (loop.llm.system_prompt or "")
              and "凯尔希" not in (loop.llm.system_prompt or ""))
        check("应答语也换成了她的", loop.wake.settings.ack == "博士，我在。",
              loop.wake.settings.ack)
        check("切人时会清掉对话历史（不继承上一位的口气）",
              loop.llm._history == [], str(loop.llm._history)[:40])  # noqa: SLF001

        # 切回默认
        loop._switch_character("kaltsit")  # noqa: SLF001
        check("能切回去", "凯尔希" in (loop.llm.system_prompt or ""))

        # 角色自带声线但没装 → 保持全局声线，不抛异常
        loop.character.voice = "zh_CN-没装过的声线"
        before = settings.tts.voice
        loop._apply_voice(loop.character, "测试")  # noqa: SLF001
        check("声线没装时不改配置（不会把嘴弄哑）", settings.tts.voice == before,
              settings.tts.voice)

        # 角色文件改了 → 热重载认出新角色
        time.sleep(0.01)
        write_chars(chars_path, sample() + [{
            "id": "w", "name": "W", "user_title": "头儿",
            "wake_words": ["W"], "ack": "嗯。",
        }])
        check("检测到角色文件变化", loop.persona.maybe_reload())  # noqa: SLF001
        check("新角色能被唤醒词找到", "w" in {c.id for c in loop.persona.all(only_enabled=True)})  # noqa: SLF001
    finally:
        loop.close()

    # 关掉角色功能 → 回退到 config.toml 的 system_prompt（老行为）
    settings.persona.enabled = False
    loop2 = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        check("关掉 persona 时不覆盖 system prompt", loop2.llm.system_prompt is None
              and loop2.character is None)
        check("这时用的是 config.toml 里那段",
              loop2.llm._system_prompt() == settings.llm.system_prompt)  # noqa: SLF001
    finally:
        loop2.close()


def test_original_default(tmp: Path) -> None:
    """★原创默认助手「白泽」★：默认角色必须是自己写的，而且不能跟克隆后端绑在一起。

    这一节守的是**发出去的那个项目**：任何人 clone 下来，默认助手都该是一个不带第三方
    作品素材的角色（名字/风格/台词自己写、声线用公开的出厂 Piper）。
    最容易坏的方式是「有人在 baize.json 里填了一条 voice_ref」—— 那样它会跟着全局的
    克隆后端去用别人的配音素材，所以这里直接断言它必须是空的。
    """
    print("\n[7] 原创默认助手：默认是谁 / 声线从哪来 / 不沾克隆素材")
    reg = CharacterRegistry(ROOT / "data" / "characters.json")
    person = reg.default()
    check("默认角色是白泽（原创）", person is not None and person.name == "白泽",
          getattr(person, "name", None))
    if person is None:
        return
    check("它指定了自己的后端 = piper，不跟全局的克隆", person.backend == "piper",
          repr(person.backend))
    check("声线是公开的出厂模型", person.voice == "zh_CN-huayan-medium", person.voice)
    check("★没带克隆参考音频★", not person.voice_ref.strip(), person.voice_ref)
    check("★没带微调出来的克隆模型目录★", not person.voice_model.strip(), person.voice_model)
    check("有唤醒词", bool(person.wake_words), "、".join(person.wake_words))
    check("称呼不是别人的（不是「博士」）", person.user_title != "博士", person.user_title)
    prompt = render_system_prompt(person)
    check("提示词里没提别的作品", "凯尔希" not in prompt and "罗德岛" not in prompt)
    check("技术字段不进提示词", "piper" not in render_system_prompt(
        Character("x", "测试", backend="beizetsu-marker")))

    print("\n[7b] 按角色切 TTS 后端（原创角色 piper / 其它角色继续用全局）")
    chars_path = tmp / "backend_chars.json"
    write_chars(chars_path, [
        {"id": "orig", "name": "原创的", "backend": "piper", "voice": "zh_CN-huayan-medium"},
        {"id": "cloned", "name": "克隆的"},
        {"id": "typo", "name": "写错的", "backend": "pipe"},
    ])
    settings = load_settings()
    settings.persona.file = str(chars_path)
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.wake.file = str(tmp / "wakewords2.json")
    (tmp / "wakewords2.json").write_text(
        json.dumps({"enabled": True, "words": ["原创的"], "idle_timeout": 30}), encoding="utf-8")
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events2.json")
    settings.skills.memo_file = str(tmp / "m2.json")
    settings.tts.backend = "zipvoice"          # 故意设成克隆，看角色能不能把它掰回 piper

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        loop._apply_character(loop.persona.get("orig"), "测试")  # noqa: SLF001
        check("角色写了 piper 时后端真的切过去", settings.tts.backend == "piper",
              settings.tts.backend)
        loop._apply_character(loop.persona.get("cloned"), "测试")  # noqa: SLF001
        check("没写 backend 的角色回到全局（zipvoice）", settings.tts.backend == "zipvoice",
              settings.tts.backend)
        loop._apply_character(loop.persona.get("typo"), "测试")  # noqa: SLF001
        check("★写错后端名不会把嘴弄哑（忽略并沿用当前的）★", settings.tts.backend == "zipvoice",
              settings.tts.backend)
    finally:
        loop.close()


def test_default_file() -> None:
    print("\n[5] 默认模板：删了也能重建，且自带两个角色")
    with tempfile.TemporaryDirectory(prefix="voiceloco_persona_") as d:
        path = Path(d) / "sub" / "characters.json"
        CharacterRegistry.write_default(path)
        check("会自动建目录并写模板", path.exists())
        reg = CharacterRegistry(path)
        check("模板自带凯尔希 + 阿米娅", {c.id for c in reg.all()} == {"kaltsit", "amiya"},
              str([c.name for c in reg.all()]))
        check("模板里凯尔希的别名是实测攒的（老猫在里面）",
              "老猫" in reg.get("kaltsit").aliases.get("凯尔希", []))
        check("模板里默认角色只有一个", sum(1 for c in reg.all() if c.default) == 1)


def test_split_layout() -> None:
    """索引 + 独立人格文件：挂谁算谁、没挂的不会被唤醒、改文件即时生效。"""
    print("\n[6] 索引 + 独立人格文件（一个角色一个文件）")
    from voice_loop.persona import DEFAULT_INDEX, PERSONA_DIR

    with tempfile.TemporaryDirectory(prefix="voiceloop_split_") as d:
        base = Path(d)
        personas = base / PERSONA_DIR
        personas.mkdir(parents=True)
        # 库里的三个人格：a、b 挂上，c 放着不挂
        (personas / "a.json").write_text(json.dumps({
            "id": "a", "name": "甲", "wake_words": ["甲甲"], "ack": "第一版应答",
            "user_title": "您", "background": "甲。",
        }, ensure_ascii=False), encoding="utf-8")
        (personas / "b.json").write_text(json.dumps({
            "id": "b", "name": "乙", "wake_words": ["乙乙"], "background": "乙。",
        }, ensure_ascii=False), encoding="utf-8")
        (personas / "c.json").write_text(json.dumps({
            "id": "c", "name": "丙", "wake_words": ["丙丙"], "background": "丙。",
        }, ensure_ascii=False), encoding="utf-8")
        index = base / "characters.json"
        index.write_text(json.dumps({
            "default": "b",
            "characters": [
                {"id": "a", "file": f"{PERSONA_DIR}/a.json"},
                f"{PERSONA_DIR}/b.json",                      # 简写：只给路径
            ],
        }, ensure_ascii=False), encoding="utf-8")

        reg = CharacterRegistry(index)
        ids = [c.id for c in reg.all()]
        check("只加载索引里挂上的两个", ids == ["a", "b"], str(ids))
        check("id 可以省略（用文件名）", (reg.get("b") or Character("", "")).id == "b")
        check("记下了各自的文件", sorted(reg.files) == ["a", "b"], str(sorted(reg.files)))
        check("没挂上的记成「库里另有」", [p.name for p in reg.orphans] == ["c.json"],
              str([p.name for p in reg.orphans]))
        check("stats 里说清谁没挂上", "c" in reg.stats(), reg.stats())
        check("索引里的 default 生效", (reg.default() or Character("", "")).id == "b")
        check("没挂上的不会被唤醒",
              set(reg.wake_map()) == {"甲甲", "乙乙"}, str(sorted(reg.wake_map())))

        # 人格文件改了 → 热重载（索引没动）
        time.sleep(0.02)
        (personas / "a.json").write_text(json.dumps({
            "id": "a", "name": "甲", "wake_words": ["甲甲"], "ack": "第二版应答",
            "user_title": "您", "background": "甲。",
        }, ensure_ascii=False), encoding="utf-8")
        check("人格文件改动会被发现", reg.maybe_reload())
        check("改动即时生效（应答语变了）", (reg.get("a") or Character("", "")).ack == "第二版应答",
              (reg.get("a") or Character("", "")).ack)

        # 索引里停用某位：人格文件不动
        time.sleep(0.02)
        data = json.loads(index.read_text(encoding="utf-8"))
        data["characters"][0]["enabled"] = False
        index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        reg.maybe_reload()
        stopped = reg.get("a")
        check("索引里 enabled=false 能停用（人格文件没动）",
              stopped is not None and not stopped.enabled and "甲甲" not in reg.wake_map(),
              f"enabled={stopped.enabled if stopped else None}")

        # 少了人格文件 / 文件坏了：只跳这一个，别把整件事弄垮
        (personas / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
        data["characters"] = [
            {"id": "a", "file": f"{PERSONA_DIR}/a.json"},
            {"id": "gone", "file": f"{PERSONA_DIR}/missing.json"},
            {"id": "broken", "file": f"{PERSONA_DIR}/broken.json"},
        ]
        index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        reg.reload()
        left = [c.id for c in reg.all()]
        check("坏掉的/找不到的只跳过自己", left == ["a"], str(left))
        check("警告里说明白是哪个文件", any("missing.json" in w for w in reg.warnings),
              " / ".join(reg.warnings))

        # split_inline：旧版（角色写死在索引里）能一键拆出去
        legacy = base / "legacy.json"
        legacy.write_text(json.dumps({
            "characters": [{"id": "old", "name": "老", "wake_words": ["老老"],
                            "background": "老。", "default": True}],
        }, ensure_ascii=False), encoding="utf-8")
        reg2 = CharacterRegistry(legacy)
        old = reg2.get("old")
        check("旧版内联写法还能读（会给个迁移提示）",
              old is not None and old.name == "老" and any("--split" in w for w in reg2.warnings))
        created = reg2.split_inline()
        check("split 拆出人格文件", [p.name for p in created] == ["old.json"],
              str([p.name for p in created]))
        check("拆完还能读到同样的人", (reg2.get("old") or Character("", "")).name == "老")
        check("拆完索引指向文件", str(reg2.files.get("old", "")).endswith("old.json"),
              str(reg2.files.get("old")))
        again = reg2.split_inline()
        check("再拆一次不会重复写", again == [], str([p.name for p in again]))
        check("默认索引模板自带两个人格引用",
              [e["id"] for e in DEFAULT_INDEX["characters"]] == ["kaltsit", "amiya"],
              str([e["id"] for e in DEFAULT_INDEX["characters"]]))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("=" * 70)
    print(" 角色设定自测（结构化人设 / 多角色唤醒 / 切换 / 热重载）")
    print("=" * 70)
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_persona_"))
    try:
        test_registry(tmp)
        test_render()
        test_wake_attribution(tmp)
        test_pipeline(tmp)
        test_default_file()
        test_split_layout()
        test_original_default(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for f in _failures:
            print(f"   - {f}")
    else:
        print(" 全部通过 √")
    print("=" * 70)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
