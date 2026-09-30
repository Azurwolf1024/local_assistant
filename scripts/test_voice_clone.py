"""「只用一条语音就能克隆」的离线自测：体检 / 挑选 / 写人格文件 / 控制台接口。

为什么值得一条条钉住：克隆参考写的是**人格文件**（唯一真相），
写错字段或覆盖掉别的键，角色的声线、称呼、台词就一起坏 —— 而且不一定当场报错。
所以这里全程用**临时目录**当角色索引，绝不碰真实 data/personas/。

    python scripts\\test_voice_clone.py
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.console import clone  # noqa: E402
from voice_loop.persona import CharacterRegistry  # noqa: E402
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


def make_wav(path: Path, seconds: float = 1.5, rate: int = 24000) -> Path:
    """写一个真的（静音）wav，好让体检读到时长/采样率。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


# 沙盒用的角色：**故意不写 voice_dir** —— 素材目录要走约定（<人格文件目录>/<id>/），
# 能天使人格里就是这样，之前只从 voice_ref 目录列候选 → 面板显示「没找到音频」。
FIXTURE = {
    "id": "exusiai", "name": "能天使", "user_title": "老板",
    "wake_words": ["能天使"],
    "lines": [{"scene": "问候", "text": "在的在的，老板！"},
              {"scene": "长句", "text": "这是一条明显更长的台词，用来验证试听挑的是最短那句。"}],
    "notes": "别把我弄丢",
}

# 素材清单（名字行紧接正文行，之后空行 —— 与真实素材同排版）
MANIFEST = """交谈1
老板，这单我送完了 —— 要不要现在就去吃点什么？

戳一下
欸，干吗。
"""
CLIP_TEXT = "老板，这单我送完了 —— 要不要现在就去吃点什么？"

# 钉住：整个测试跑完，真实仓库里这个文件的字节数一个不能变
REAL_PERSONA = ROOT / "data" / "personas" / "exusiai.json"


def setup(tmp: Path):
    """造一个「临时角色索引 + 一个临时角色」的沙盒（install 只会写进这里）。"""
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    room = personas / "exusiai"
    room.mkdir()
    for name, seconds in (("交谈1.wav", 2.0), ("戳一下.wav", 1.2), ("太短.wav", 0.3)):
        make_wav(room / name, seconds)
    (room / "exusiai.txt").write_text(MANIFEST, encoding="utf-8")   # 素材清单（带文本）
    (personas / "exusiai.json").write_text(
        json.dumps(FIXTURE, ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp / "characters.json").write_text(json.dumps({
        "default": "exusiai",
        "characters": [{"id": "exusiai", "file": "personas/exusiai.json"}],
    }, ensure_ascii=False), encoding="utf-8")
    settings = load_settings()
    settings.persona.file = str(tmp / "characters.json")
    return settings, CharacterRegistry(tmp / "characters.json")


def test_inspect(tmp: Path) -> None:
    print("\n[1] 体检：什么样的音频能当参考")
    good = make_wav(tmp / "good.wav", 1.5, 24000)
    info = clone.inspect(good)
    check("正常音频通过", (info.ok, round(info.seconds, 1), info.samplerate), (True, 1.5, 24000))
    short = clone.inspect(make_wav(tmp / "short.wav", 0.3))
    check("太短的会被挡（并说清楚原因）", (short.ok, "太短" in short.problem), (False, True))
    check("不存在的文件", clone.inspect(tmp / "nope.wav").ok, False)
    junk = tmp / "junk.wav"
    junk.write_bytes(b"not a wav at all")
    check("坏文件不会当成通过", clone.inspect(junk).ok, False)


def test_candidates(tmp: Path) -> None:
    print("\n[2] 候选：从她的素材目录里挑（没写 voice_dir 也找得到）")
    settings, registry = setup(tmp)
    char = registry.get("exusiai")
    room = tmp / "personas" / "exusiai"
    check("素材目录走约定：<人格文件目录>/<id>/", clone.material_dir(settings, char), room)

    rows = clone.candidates(settings, char)
    names = sorted(r["name"] for r in rows)
    check("列全了三种音频（不含清单 txt）", names, ["交谈1.wav", "太短.wav", "戳一下.wav"])
    check("逐条带体检结果（太短的标出来）",
          [r["ok"] for r in rows if r["name"] == "太短.wav"], [False])
    check("候选自带对应文本（按文件名对清单）",
          [(r["name"], r["text"]) for r in rows if r["text"]],
          [("交谈1.wav", CLIP_TEXT), ("戳一下.wav", "欸，干吗。")])
    check("文本来源标出来了", [r["text_source"] for r in rows if r["text"]], ["清单", "清单"])
    check("没文本的那条如实留空", [r["text"] for r in rows if r["name"] == "太短.wav"], [""])
    check("前端查表（传文件时按文件名查）", clone.ui_index(settings, char, rows).get("交谈1"), CLIP_TEXT)

    here = str(room / "交谈1.wav")
    check("素材在项目根外面时给绝对路径（不瞎猜相对路径）",
          any(r["path"] == here for r in rows), True)

    # 项目里 → 相对路径（人格文件里存的就是这个，好读也好搬）
    inside = ROOT / "sessions" / "tmp_clone_probe" / "reference.wav"
    try:
        make_wav(inside, 1.0)
        rel = clone.candidates(settings, voice_dir="sessions/tmp_clone_probe")
        check("素材在项目里时给相对路径", rel and rel[0]["path"], "sessions/tmp_clone_probe/reference.wav")
    finally:
        shutil.rmtree(inside.parent, ignore_errors=True)
    check("试听挑的是最短那句台词（她真会说的话）", clone.preview_sentence(char), "在的在的，老板！")

    # 目录不存在时不报错、返回空表（UI 会提示「就传一条」）
    empty = clone.candidates(settings, char, limit=0)
    check("limit=0 → 空表（不抛）", empty, [])


def test_install(tmp: Path) -> None:
    print("\n[3] 安装：写进人格文件（带备份、不碰别的键、文本自动填）")
    settings, registry = setup(tmp)
    char = registry.get("exusiai")
    clip = tmp / "personas" / "exusiai" / "交谈1.wav"

    plan = clone.install(settings, char, clip=str(clip), dry_run=True)
    check("试运行不写文件", Path(plan["file"]).exists(), False)
    check("试运行也会告诉你会改什么",
          sorted(plan["changed"]), ["backend", "voice_ref", "voice_ref_text"])
    check("★没手填也会自动带上参考文本★", (plan["ref_text"], plan["text_source"]),
          (CLIP_TEXT, "清单"))

    got = clone.install(settings, char, clip=str(clip), ref_text="在的，老板！", dry_run=False)
    target = tmp / "personas" / "exusiai.json"
    saved = json.loads(target.read_text(encoding="utf-8"))
    check("voice_ref 写进去了", Path(saved["voice_ref"]).name, "交谈1.wav")
    check("手填的文本优先（不被清单覆盖）", saved["voice_ref_text"], "在的，老板！")
    check("backend 补成 zipvoice（单条克隆走的就是它）", saved["backend"], "zipvoice")
    check("别的键一个没动", (saved["notes"], len(saved["lines"])), ("别把我弄丢", 2))
    check("留了备份", Path(got["backup"]).is_file(), True)

    again = clone.install(settings, char, clip=str(clip), ref_text="在的，老板！", dry_run=False)
    check("重复设置同一份 → 无变化（幂等）", again["changed"], {})

    # ★换了音频却没文本 → 把上一段那行清掉★（留着就是「声不对词」，工程日志 §21）
    quiet = make_wav(tmp / "personas" / "exusiai" / "没有文本.wav", 2.0)
    moved = clone.install(settings, char, clip=str(quiet), dry_run=True)
    check("换成没文本的音频 → 试运行里就看得到会清空",
          (moved["ref_text"], moved["changed"].get("voice_ref_text"), moved["cleared_text"]),
          ("", "", True))
    real = clone.install(settings, char, clip=str(quiet), dry_run=False)
    after = json.loads(target.read_text(encoding="utf-8"))
    check("真写下去时旧文本也真清掉了", (after["voice_ref_text"], real["cleared_text"]), ("", True))


def test_install_upload(tmp: Path) -> None:
    print("\n[4] 上传：把一条语音存进她的素材目录")
    settings, registry = setup(tmp)
    char = registry.get("exusiai")
    blob = base64.b64encode(make_wav(tmp / "mine.wav", 2.0).read_bytes()).decode()
    got = clone.install(settings, char, data_base64=blob, filename="我的录音.wav", dry_run=False)
    saved = json.loads((tmp / "personas" / "exusiai.json").read_text(encoding="utf-8"))
    ref = Path(saved["voice_ref"])
    check("存在角色目录下（跟着人格文件走，不是跟着项目根）",
          (ref.name, ref.parent.name, ref.parent.parent == tmp / "personas"),
          ("clone-ref.wav", "exusiai", True))
    check("文件真的在（字节一个不差）", (ref.is_file(), ref.stat().st_size),
          (True, len(base64.b64decode(blob))))
    check("报告里标了 uploaded", got["uploaded"], True)

    # 传上来的文件名能对上清单 → 文本自动带上（不用手打）
    named = clone.install(settings, char, data_base64=blob, filename="交谈1.wav", dry_run=True)
    check("上传也能按文件名对上文本", (named["ref_text"], named["text_source"]), (CLIP_TEXT, "清单"))
    odd = clone.install(settings, char, data_base64=blob, filename="我的录音.wav", dry_run=True)
    check("对不上就留空（不乱填别人的文本）", (odd["ref_text"], odd["text_source"]), ("", ""))

    dry = clone.install(settings, char, data_base64=blob, filename="x.wav", dry_run=True)
    check("试运行不上传（只说会怎么做）", (dry["dry_run"], dry["uploaded"]), (True, False))
    check("太大的上传会被挡",
          _raises(lambda: clone.install(settings, char, data_base64="A" * 64, dry_run=False)), True)
    check("两个来源都不给时会报错",
          _raises(lambda: clone.install(settings, char, dry_run=False)), True)

def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def test_console_endpoint(tmp: Path) -> None:
    print("\n[5] 控制台接口：dry_run 能跑通（真走 FastAPI 路由）")
    settings, registry = setup(tmp)
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001
        print(f"    · 没装 fastapi/httpx，跳过（{exc}）")
        return
    from voice_loop.console.panels import voices as panel

    class Ctx:
        root = tmp

        def __init__(self) -> None:
            self.settings = settings
            self.channel = type("Ch", (), {"status": lambda _self: {"pending": 0}})()

        def characters(self):
            return registry

        def call_service(self, cmd, **kw):       # 试运行不会走到这里
            raise AssertionError("dry_run 不该去调服务")

    app = FastAPI()
    panel.register(app, Ctx())
    client = TestClient(app)

    listing = client.get("/api/voices/clone", params={"cid": "exusiai"})
    got = listing.json()
    check("GET /api/voices/clone 列候选", (listing.status_code, len(got["candidates"])), (200, 3))
    check("GET 也带文本（前端直接能用）",
          (got["ref_index"].get("交谈1"), got["materials_with_text"]), (CLIP_TEXT, 2))
    clip = tmp / "personas" / "exusiai" / "交谈1.wav"
    r = client.post("/api/voices/clone", json={"id": "exusiai", "path": str(clip), "dry_run": True})
    check("POST dry_run 告诉我们改什么（含自动填的文本）",
          (r.status_code, sorted(r.json()["changed"])),
          (200, ["backend", "voice_ref", "voice_ref_text"]))
    check("dry_run 时不会去动服务", r.json()["dry_run"], True)
    bad = client.post("/api/voices/clone", json={"id": "exusiai", "path": str(tmp / "nope.wav")})
    check("坏输入返回 400 + 人话", (bad.status_code, "不存在" in bad.json()["detail"]), (400, True))
    miss = client.post("/api/voices/clone", json={"id": "不存在的人", "dry_run": True})
    check("没有这个角色 → 404", miss.status_code, 404)


def main() -> int:
    before = REAL_PERSONA.stat().st_size if REAL_PERSONA.is_file() else -1
    tmp = Path(tempfile.mkdtemp(prefix="voice_clone_"))
    try:
        test_inspect(tmp / "a")
        test_candidates(tmp / "b")
        test_install(tmp / "c")
        test_install_upload(tmp / "d")
        test_console_endpoint(tmp / "e")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    after = REAL_PERSONA.stat().st_size if REAL_PERSONA.is_file() else -1
    print("\n[6] 闸门：真实仓库没被动过")
    check("data/personas/exusiai.json 字节数没变", after, before,
          detail=f"{before} → {after}")
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 单条语音克隆自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
