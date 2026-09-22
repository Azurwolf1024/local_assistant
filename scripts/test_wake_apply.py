"""唤醒词 --apply 的自测：确认它写进「对应角色」的人格文件。

跑法：
    python scripts/test_wake_apply.py

★为什么要有这个测试★：`test_wake.py --apply` 原来不管有没有角色都写
`data/wakewords.json`，但多角色时运行期的匹配表是用**人格文件**里的 aliases 建的
（`WakeWordMatcher.set_characters`），全局那份会被忽略——于是「写成功了」却完全不起作用。

全程在临时目录里造角色文件与全局 wakewords.json，不动你自己的 data/，也不碰麦克风。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_wake import WakeTarget, collect_targets, looks_like, write_alias  # noqa: E402
from voice_loop.persona import CharacterRegistry  # noqa: E402
from voice_loop.wake import WakeWordMatcher  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


class Shim:
    """只带 collect_targets / load_registry 用得到的那几个字段的 settings 替身。"""

    def __init__(self, wake_file: Path, persona_file: Path) -> None:
        self.wake = type("W", (), {"file": str(wake_file), "enabled": True})()
        self.persona = type("P", (), {"file": str(persona_file), "enabled": True})()

    @staticmethod
    def resolve(path):
        return Path(path)


def make_chars(path: Path) -> None:
    """索引照真实 data/characters.json 的写法：只写 {id, file} 指向人格文件。

    （把角色直接内联在索引里也支持，但那种情况唤醒词得改索引本身，本工具会明确跳过。）
    """
    chars = [
        {"id": "kaltsit", "file": "personas/kaltsit.json", "default": True},
        {"id": "amiya", "file": "personas/amiya.json"},
    ]
    path.write_text(
        json.dumps({"default": "kaltsit", "characters": chars}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wake_apply_"))
    personas = tmp / "personas"
    personas.mkdir()
    chars_file = tmp / "characters.json"
    make_chars(chars_file)
    # 角色们的人格文件（唤醒词、别名都住在这里）
    (personas / "kaltsit.json").write_text(
        json.dumps({"id": "kaltsit", "name": "凯尔希", "wake_words": ["凯尔希"],
                    "aliases": {"凯尔希": ["凯尔西", "老猫"]}},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (personas / "amiya.json").write_text(
        json.dumps({"id": "amiya", "name": "阿米娅", "wake_words": ["阿米娅", "阿米娅小姐"],
                    "aliases": {"阿米娅": ["阿米亚"]}},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 全局兜底文件：一个和角色重名的词（应被角色那份盖掉）+ 一个全局独有的词
    wake_file = tmp / "wakewords.json"
    wake_file.write_text(
        json.dumps({"words": ["凯尔希", "小助手"], "aliases": {"凯尔希": ["开尔希"]}},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    settings = Shim(wake_file, chars_file)
    registry = CharacterRegistry(chars_file)
    registry.load()
    matcher = WakeWordMatcher(wake_file)
    matcher.set_characters(registry.all(only_enabled=True))

    print("\n[1] 目标清单：每个词该写到哪个文件")
    targets = collect_targets(settings, matcher, registry)
    plan = {t.word: (t.label, t.file.name, t.owner) for t in targets}
    check("列出 4 个词（角色 3 个 + 全局独有 1 个）", len(targets) == 4, str(sorted(plan)))
    check("「阿米娅」归到 amiya 的人格文件",
          plan.get("阿米娅", ("", "", ""))[1] == "amiya.json"
          and plan["阿米娅"][2] == "amiya",
          str(plan.get("阿米娅")))
    check("「阿米娅小姐」也是 amiya 的", plan.get("阿米娅小姐", ("", "", ""))[2] == "amiya")
    check("「凯尔希」归到 kaltsit 的人格文件（不是全局）",
          plan.get("凯尔希", ("", "", ""))[1] == "kaltsit.json", str(plan.get("凯尔希")))
    check("全局独有的词仍在计划里（写全局文件）",
          plan.get("小助手", ("", "", ""))[1] == "wakewords.json"
          and plan["小助手"][2] == "",
          str(plan.get("小助手")))
    check("现有别名被读出来（凯尔希 2 个）",
          {t.word: len(t.aliases) for t in targets}.get("凯尔希") == 2)

    print("\n[2] --char 只挑那个角色")
    one = collect_targets(settings, matcher, registry, only="amiya")
    check("--char amiya 只给阿米娅的两个词",
          sorted(t.word for t in one) == ["阿米娅", "阿米娅小姐"], str([t.word for t in one]))
    check("--char 写错名字 → 空（不会静默退回全局）",
          collect_targets(settings, matcher, registry, only="不存在") == [])
    check("--char 也认中文名", sorted(t.word for t in collect_targets(
        settings, matcher, registry, only="阿米娅")) == ["阿米娅", "阿米娅小姐"])

    print("\n[3] 写别名：进人格文件，不碰全局")
    amiya = next(t for t in targets if t.word == "阿米娅")
    before_global = wake_file.read_text(encoding="utf-8")
    result = write_alias(amiya, "阿米呀")
    check("写入成功", result.startswith("已写入"), result)
    after = read(personas / "amiya.json")
    check("人格文件里多了「阿米呀」", "阿米呀" in after["aliases"]["阿米娅"],
          str(after["aliases"]))
    check("原有别名没丢", "阿米亚" in after["aliases"]["阿米娅"])
    check("全局文件一个字没动", wake_file.read_text(encoding="utf-8") == before_global)
    check("留了 .bak 备份", (personas / "amiya.json.bak").exists())
    check("JSON 仍然合法", read(personas / "amiya.json")["id"] == "amiya")

    print("\n[4] 写完之后真的能唤醒（这才是重点）")
    matcher2 = WakeWordMatcher(wake_file)
    reg2 = CharacterRegistry(chars_file)
    reg2.load()
    matcher2.set_characters(reg2.all(only_enabled=True))
    hit = matcher2.match("阿米呀")
    check("命中「阿米娅小姐」/「阿米娅」那一条", hit is not None, str(hit))
    check("归属是 amiya（不是凯尔希、也不是空）",
          bool(hit) and hit.character == "amiya", getattr(hit, "character", ""))

    print("\n[5] 防呆：杂音不写、重复不写")
    check("差太远的说法被挡下", write_alias(amiya, "今天天气不错").startswith("已跳过"))
    check("重复写入报「已存在」", write_alias(amiya, "阿米呀").startswith("已存在"))
    check("空字符串被挡下", write_alias(amiya, "  ").startswith("已跳过"))
    check("looks_like：凯尔希 ↔ 海尔西 收",
          looks_like("海尔西", "凯尔希") and not looks_like("今天天气不错", "凯尔希"))
    check("全局目标也能写（没有角色时的那条路）",
          write_alias(next(t for t in targets if t.word == "小助手"), "小助受").startswith("已写入"))
    check("写进了全局文件", "小助受" in read(wake_file)["aliases"]["小助手"])

    print("\n[6] 没有角色时退回全局文件")
    empty = tmp / "empty.json"
    empty.write_text(json.dumps({"characters": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    reg3 = CharacterRegistry(empty)
    reg3.load()
    falls = collect_targets(settings, matcher, reg3)
    check("无角色 → 目标全在全局文件",
          bool(falls) and all(t.file == wake_file for t in falls),
          str([(t.word, t.file.name) for t in falls]))

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：" + "；".join(_failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
