"""风格控制自测：标签解析/剥除、voice_refs 取档、管线按风格换参考。

为什么要一条条钉住（2026-09-30，工程日志 §50）：
「按风格换参考」动的是**正在用的声线**，写错档位或者标签没剥干净，
要么角色突然变成另一个人的声音，要么把 `<style=calm>` 念出来 —— 两种都很显眼但很难查。

    python scripts\\test_style.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.persona import Character, CharacterRegistry   # noqa: E402
from voice_loop.settings import load_settings                 # noqa: E402
from voice_loop.tts import style as stylekit                  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


CHAR = {"id": "kaltsit", "name": "凯尔希",
        "voice_ref": "data/personas/kaltsit/干员报到.wav",
        "voice_ref_text": "博士，请坐。",
        "voice_refs": {"calm": "data/personas/kaltsit/完成高难行动.wav",
                       "催促": {"ref": "data/personas/kaltsit/精英化晋升1.wav",
                                "text": "文件在这里，自己看。"},
                       "空的": ""}}


def test_parse_tag() -> None:
    print("\n[1] 标签解析：只认开头，剥干净")
    check("标准写法", stylekit.parse_tag("<style=calm>今天没什么事。"),
          ("calm", "今天没什么事。"))
    check("方括号 + 前后空格也认", stylekit.parse_tag("  [style= 催促 ]  快点。"),
          ("催促", "快点。"))
    check("大小写不敏感", stylekit.parse_tag("<STYLE=Calm>x")[0], "Calm")
    check("中文键名", stylekit.parse_tag("<style=生气>x")[0], "生气")
    check("没有标签就原样返回", stylekit.parse_tag("今天没什么事。"),
          ("", "今天没什么事。"))
    check("★正文中间的标签不动它★（那是内容，不是指令）",
          stylekit.parse_tag("我说过 <style=calm> 这个写法。"),
          ("", "我说过 <style=calm> 这个写法。"))
    check("空文本不炸", stylekit.parse_tag(""), ("", ""))


def test_stripper() -> None:
    print("\n[2] 流式剥除：标签被切成好几个 token 也要剥对")
    s = stylekit.StyleStripper()
    out = "".join(s.feed(x) for x in ["<sty", "le=ca", "lm>", "今天", "没什么事。"])
    check("逐 token 喂 → 标签没了、正文完整", out, "今天没什么事。")
    check("记下了风格键", s.style, "calm")

    s2 = stylekit.StyleStripper()
    out2 = "".join(s2.feed(x) for x in ["今", "天很", "好。"])
    check("普通回答一个字不丢（不压开头）", out2, "今天很好。")
    check("普通回答没有风格键", s2.style, "")

    s3 = stylekit.StyleStripper()
    check("只说「<」时先攒着", s3.feed("<"), "")
    check("发现不是标签就立刻放行", s3.feed("3 米长"), "<3 米长")
    s4 = stylekit.StyleStripper()
    check("流结束要把攒着的吐出来（短回答别被吃掉）",
          s4.feed("<style=c"), "")
    check("finish 吐出没吃完的部分", s4.finish(), "<style=c")
    s5 = stylekit.StyleStripper()
    body = "".join([s5.feed("<style=x>"), s5.feed("好的"), s5.finish()])
    check("标签后紧跟正文 + finish 不重复", body, "好的")
    s6 = stylekit.StyleStripper()      # 标签没闭合（模型被截断）：finish 要原样吐出来，别吞字
    check("没闭合的标签不会被吞掉", s6.feed("<style=x好的"), "")
    check("finish 把没闭合的原样还回来", s6.finish(), "<style=x好的")


def test_pick_ref() -> None:
    print("\n[3] 取档：有这一档就换，没有就退回 voice_ref（老行为）")
    char = Character.from_dict(CHAR)
    check("字符串写法", stylekit.pick_ref(char, "calm"),
          ("data/personas/kaltsit/完成高难行动.wav", ""))
    check("字典写法（带文本）", stylekit.pick_ref(char, "催促"),
          ("data/personas/kaltsit/精英化晋升1.wav", "文件在这里，自己看。"))
    check("★没这一档 → 退回 voice_ref★", stylekit.pick_ref(char, "不存在的档"),
          ("data/personas/kaltsit/干员报到.wav", "博士，请坐。"))
    check("键为空 → 退回 voice_ref", stylekit.pick_ref(char, ""),
          ("data/personas/kaltsit/干员报到.wav", "博士，请坐。"))
    check("空字符串的档被丢掉（不算一档）", "空的" in char.voice_refs, False)
    check("不存在的键 → 退回 voice_ref", stylekit.pick_ref(char, "空的"),
          ("data/personas/kaltsit/干员报到.wav", "博士，请坐。"))
    check("可用键按写的顺序列出来", stylekit.keys(char), ["calm", "催促"])
    check("没写 voice_refs 时 keys 为空、提示为空",
          (stylekit.keys(Character.from_dict({"id": "x", "name": "X"})),
           stylekit.hint(Character.from_dict({"id": "x", "name": "X"}))), ([], ""))
    check("提示里带上了键名与格式",
          ("<style=calm>" in stylekit.hint(char), "催促" in stylekit.hint(char)), (True, True))


def test_persona_field() -> None:
    print("\n[4] 人格解析：voice_refs 必须真的搬进来（白名单构造的坑）")
    char = Character.from_dict(CHAR)
    check("字段搬进来了", list(char.voice_refs), ["calm", "催促"])
    check("值也原样在", char.voice_refs["calm"], "data/personas/kaltsit/完成高难行动.wav")
    check("to_dict 能带出来", "voice_refs" in char.to_dict(), True)
    weird = Character.from_dict({"id": "x", "name": "X", "voice_refs": ["calm"]})
    check("写错类型（列表）不炸、当成空", weird.voice_refs, {})


def test_pipeline_switch(tmp: Path) -> None:
    print("\n[5] 管线：set_style 真的换参考（不加载模型也能验）")
    from voice_loop.pipeline import VoiceLoop  # noqa: PLC0415

    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "kaltsit.json").write_text(
        json.dumps(CHAR, ensure_ascii=False), encoding="utf-8")
    (tmp / "characters.json").write_text(json.dumps(
        {"default": "kaltsit", "characters": [{"id": "kaltsit", "file": "personas/kaltsit.json"}]},
        ensure_ascii=False), encoding="utf-8")

    settings = load_settings()
    settings.persona.file = str(tmp / "characters.json")
    registry = CharacterRegistry(tmp / "characters.json")
    loop = object.__new__(VoiceLoop)          # ★不要真建 VoiceLoop★（会开 Tk/加载模型）
    loop.settings = settings
    loop.character = registry.get("kaltsit")
    loop.tts = None                            # 引擎没加载：只改配置，下次加载自然生效
    loop._base_clone_audio = "data/personas/kaltsit/干员报到.wav"
    loop._base_clone_dir = settings.tts.clone_dir
    loop._base_backend = "zipvoice"
    loop._base_voice = "zh_CN-huayan-medium"
    loop.log = logging.getLogger("test_style")
    settings.tts.clone_audio = ""
    settings.tts.clone_text = ""

    check("初始风格为空", loop.style, "")
    check("切到 calm → 参考换成那一条",
          (loop.set_style("calm"), Path(settings.tts.clone_audio).name),
          ("calm", "完成高难行动.wav"))
    check("字符串档的文本是空的（由清单/自动转写去补）", settings.tts.clone_text, "")
    check("带文本的档也把文本带上",
          (loop.set_style("催促"), settings.tts.clone_text),
          ("催促", "文件在这里，自己看。"))
    check("★不知道的档 → 不动★（返回空串，保持当前）",
          (loop.set_style("不存在"), loop.style), ("", "催促"))
    check("切回默认（空键）→ 回到 voice_ref 那一档",
          (loop.set_style(""), Path(settings.tts.clone_audio).name),
          ("", "干员报到.wav"))
    check("★没写 voice_refs 的角色：set_style 是空操作★",
          (lambda: (setattr(loop, "character", Character.from_dict({"id": "x", "name": "X"})),
                    loop.set_style("calm"))[1])(), "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="style_test_"))
    try:
        test_parse_tag()
        test_stripper()
        test_pick_ref()
        test_persona_field()
        test_pipeline_switch(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 风格控制自测全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
