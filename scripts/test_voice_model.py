"""角色专属声线的自测：字段解析 / 素材盘点 / 切换路由（不加载任何模型）。

跑法：
    python scripts/test_voice_model.py

三件事必须守住：
1. 人格文件里的 ``voice_model`` / ``voice_dir`` 能读出来；
2. 盘点能分清「缺音频 / 缺文本 / 素材偏少 / 可训练 / 已训模型」；
3. **切换角色时模型与参考音频都要跟着换，而且能切回默认**——
   否则一个角色换过之后，其他角色会沿用她的声音；模型目录不完整时**不许切**
   （宁可音色不对，也不能把嘴弄哑）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.persona import Character, CharacterRegistry  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.voice_data import inspect, inspect_all, render_report  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else '×'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def make_wav(path: Path, seconds: float, rate: int = 44100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 220 * np.arange(n) / rate)).astype(np.float32), rate)


# --------------------------------------------------------------------------- #
print("\n[1] 人格字段解析")
with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "amestris.json").write_text(
        json.dumps(
            {
                "id": "amestris",
                "name": "测试角色",
                "wake_words": ["测试"],
                "voice_ref": "data/personas/amestris/ref.wav",
                "voice_ref_text": "这是一句参考文本。",
                "voice_model": "models/tts/zipvoice/personas/amestris",
                "voice_dir": "somewhere/audio",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "characters.json").write_text(
        json.dumps({"characters": [{"id": "amestris", "file": "personas/amestris.json"}]}),
        encoding="utf-8",
    )
    registry = CharacterRegistry(tmp_path / "characters.json")
    registry.load()
    char = registry.get("amestris")
    check("voice_model 读出来了", char.voice_model == "models/tts/zipvoice/personas/amestris", char.voice_model)
    check("voice_dir 读出来了", char.voice_dir == "somewhere/audio", char.voice_dir)
    check("voice_ref 没受影响", char.voice_ref.endswith("ref.wav"))
    check("没配时是空串", Character.from_dict({"id": "x", "name": "X"}).voice_model == "")
    check("不会写进提示词", "voice_model" not in __import__("voice_loop.persona", fromlist=["x"]).render_system_prompt(char))

    print("\n[2] 素材盘点")
    with tempfile.TemporaryDirectory() as tmp2:
        root = Path(tmp2)
        pdir = root / "data" / "personas"
        pdir.mkdir(parents=True)
        pjson = pdir / "kaltsit.json"
        pjson.write_text(json.dumps({"id": "kaltsit", "name": "凯尔希"}), encoding="utf-8")
        audio = pdir / "kaltsit"

        # ① 没音频
        item = inspect(pjson, "kaltsit", "凯尔希", root=root)
        check("没目录 → 缺音频", item.status == "缺音频", item.status)
        check("没目录 → 不是可训练", not item.ready)

        # ② 有音频没文本
        for i in range(6):
            make_wav(audio / f"clip{i}.wav", 12.0)
        item = inspect(pjson, "kaltsit", "凯尔希", root=root)
        check("有音频没文本 → 缺文本", item.status.startswith("缺文本"), item.status)
        check("时长统计正确（6×12=72s）", abs(item.seconds - 72.0) < 0.5, f"{item.seconds:.1f}s")

        # ③ 文本覆盖够 → 可训练
        (audio / "kaltsit.txt").write_text(
            "\n\n".join(f"clip{i}\n这是第 {i} 条台词。" for i in range(6)), encoding="utf-8"
        )
        item = inspect(pjson, "kaltsit", "凯尔希", root=root)
        check("文本齐了 → 可训练", item.status == "可训练", item.status)
        check("ready 为真", item.ready)

        # ④ 素材偏少
        (audio / "clip5.wav").unlink()
        (audio / "clip4.wav").unlink()
        item = inspect(pjson, "kaltsit", "凯尔希", root=root)
        check("只剩 4 条 → 素材偏少", item.status.startswith("素材偏少"), item.status)
        check("偏少时不算 ready", not item.ready)

        # ⑤ 已训模型（目录齐全才算）
        model_dir = root / "models" / "tts" / "zipvoice" / "personas" / "kaltsit"
        model_dir.mkdir(parents=True)
        (model_dir / "tokens.txt").write_text("x", encoding="utf-8")
        check("模型目录不全（只有 tokens.txt）→ 不算已训", not inspect(pjson, "kaltsit", "凯尔希", root=root).trained)
        for name in ("encoder.int8.onnx", "decoder.int8.onnx", "lexicon.txt"):
            (model_dir / name).write_text("x", encoding="utf-8")
        check("还缺 espeak-ng-data → 仍不算已训", not inspect(pjson, "kaltsit", "凯尔希", root=root).trained)
        (model_dir / "espeak-ng-data").mkdir()
        for i in range(4):
            make_wav(audio / f"add{i}.wav", 12.0)
        (audio / "kaltsit.txt").write_text(
            "\n\n".join(
                f"{p.stem}\n这是第 {i} 条台词。"
                for i, p in enumerate(sorted(audio.glob("*.wav")))
            ),
            encoding="utf-8",
        )
        item = inspect(pjson, "kaltsit", "凯尔希", root=root)
        check("模型目录齐全 → 已训模型", item.status == "已训模型", item.status)
        check("trained 为真", item.trained)

        report = render_report([item], root)
        check("报告里有角色名", "凯尔希" in report)
        check("报告里有条数", str(len(item.clips)) in report)
        check("报告说明了门槛", "秒 /" in report or "门槛" in report)

    print("\n[3] 切角色的声线路由（不加载真模型）")

    class FakeTts:
        """记录「谁被改过」，并假装自己是懒加载的。"""

        def __init__(self) -> None:
            self.unloaded = 0
            self.reference_calls: list[tuple[str, str]] = []

        def unload(self) -> None:
            self.unloaded += 1

        def configure(self, fn) -> None:
            class _Engine:
                def set_reference(inner_self, want, text):  # noqa: N805
                    self.reference_calls.append((want, text))

            fn(_Engine())

    settings = load_settings()
    settings.tts.backend = "zipvoice"
    settings.tts.clone_dir = "BASE_MODEL"
    settings.tts.clone_audio = "BASE_REF.wav"

    from voice_loop.pipeline import VoiceLoop

    loop = object.__new__(VoiceLoop)
    loop.settings = settings
    import logging

    loop.log = logging.getLogger("voice_loop.test")
    loop._base_voice = ""
    loop._base_clone_dir = "BASE_MODEL"
    loop._base_clone_audio = "BASE_REF.wav"
    loop._base_clone_text = ""
    loop.tts = FakeTts()

    # 造两个角色的模型目录：一个完整、一个缺文件
    with tempfile.TemporaryDirectory() as tmp3:
        root = Path(tmp3)
        good = root / "good_model"
        good.mkdir()
        for name in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "lexicon.txt"):
            (good / name).write_text("x", encoding="utf-8")
        (good / "espeak-ng-data").mkdir()
        bad = root / "bad_model"
        bad.mkdir()
        (bad / "tokens.txt").write_text("x", encoding="utf-8")
        ref = root / "ref.wav"
        make_wav(ref, 3.0)

        tuned = Character(
            id="tuned", name="配好模型的", voice_model=str(good), voice_ref=str(ref), voice_ref_text="参考"
        )
        broken = Character(id="broken", name="模型不全的", voice_model=str(bad), voice_ref=str(ref))
        plain = Character(id="plain", name="没配模型的")

        loop._apply_reference(tuned, "测试")
        check("配了模型 → clone_dir 换成角色目录", settings.tts.clone_dir == str(good), settings.tts.clone_dir)
        check("配了模型 → 卸载了引擎（下次用新模型加载）", loop.tts.unloaded == 1, str(loop.tts.unloaded))
        check("参考音频也换了", settings.tts.clone_audio == str(ref), settings.tts.clone_audio)

        loop._apply_reference(plain, "测试")
        check("角色没配模型 → 切回默认模型", settings.tts.clone_dir == "BASE_MODEL", settings.tts.clone_dir)
        check("模型换了就再卸载一次", loop.tts.unloaded == 2, str(loop.tts.unloaded))

        unloads_before = loop.tts.unloaded
        loop._apply_reference(broken, "测试")
        check("模型目录不全 → 不切（继续用默认）", settings.tts.clone_dir == "BASE_MODEL", settings.tts.clone_dir)
        check("模型目录不全 → 不卸载", loop.tts.unloaded == unloads_before, str(loop.tts.unloaded))

        # 只换参考音频（模型目录相同）时不该触发重载
        before = loop.tts.unloaded
        other_ref = root / "ref2.wav"
        make_wav(other_ref, 3.0)
        loop._apply_reference(
            Character(id="r2", name="只换参考", voice_ref=str(other_ref), voice_ref_text=""), "测试"
        )
        check("只换参考音频 → 不重载", loop.tts.unloaded == before, str(loop.tts.unloaded))
        check("只换参考音频 → 当场生效（configure）", loop.tts.reference_calls[-1][0] == str(other_ref))
        check("默认模型没被弄坏", settings.tts.clone_dir == "BASE_MODEL")

print()
if _failures:
    print(f"★ {len(_failures)} 项未通过：{_failures}")
    sys.exit(1)
print("全部通过 √")
sys.exit(0)
