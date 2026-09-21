"""音色克隆后端（zipvoice）的离线测试。

不依赖模型文件也能跑的部分（默认就跑这些）：
    - 后端分发：backend 名字写错要报错，不能静默退回 piper
    - 护栏：克隆模型没装全 → 自动退回 Piper 出声（绝不把嘴弄哑）
    - 参考音频预处理：掐静音、截断上限、单声道
    - 参考文本：同名 .txt 优先；日语自动转罗马字；没有 pykakasi 也不能崩
    - 角色字段：voice_ref / voice_ref_text 能读进来

加 `--full` 会真的加载 ZipVoice 跑一句（需要先下载模型，约 1 分钟）。

    python scripts/test_tts_clone.py
    python scripts/test_tts_clone.py --full
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_loop.persona import Character  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.tts import create_tts  # noqa: E402
from voice_loop.tts.zipvoice_tts import missing_files  # noqa: E402

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


# --------------------------------------------------------------------------- #
def test_backend_dispatch(settings) -> None:
    print("\n[1] 后端分发")
    try:
        settings.tts.backend = "no-such-backend"
        create_tts(settings, lazy=False)
        check("没报错", "应该报错", "未知 backend 必须报错")
    except ValueError as exc:
        check_true("no-such-backend" in str(exc), f"未知 backend 报错清晰（{exc}）")
    finally:
        settings.tts.backend = "piper"

    engine = create_tts(settings, lazy=True)
    check_true(hasattr(engine, "unload"), "懒加载壳有 unload()")
    check_true(hasattr(engine, "configure"), "懒加载壳有 configure()")
    check(engine.loaded, False, "刚创建时没有加载模型")


    print("    · 克隆模型没装全时不能把嘴弄哑")
    real_dir = settings.tts.clone_dir
    try:
        settings.tts.backend = "zipvoice"
        settings.tts.clone_dir = "models/tts/zipvoice/不存在的模型目录"
        check(len(missing_files(settings)) > 0, True, "缺文件能报出来（给下载脚本提示用）")
        fallback = create_tts(settings, lazy=False)
        check(fallback.name, "piper", "模型没装全 → 自动退回 Piper 出声（不报错、不哑）")
    finally:
        settings.tts.clone_dir = real_dir
        settings.tts.backend = "piper"
    check(missing_files(settings), [], "模型真装好了（缺文件列表为空）")


def test_reference_prep() -> None:
    print("\n[2] 参考音频预处理（不需要模型：只调纯函数）")
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts, _resample

    # 用 __new__ 拿一个不初始化模型的实例，只测纯函数
    stub = ZipVoiceTts.__new__(ZipVoiceTts)

    rate = 16000
    quiet = np.zeros(int(rate * 0.5), dtype=np.float32)
    tone = (0.3 * np.sin(2 * np.pi * 220 * np.arange(int(rate * 1.0)) / rate)).astype(np.float32)
    tail = np.zeros(int(rate * 0.5), dtype=np.float32)
    audio = np.concatenate([quiet, tone, tail])
    trimmed = stub._trim(rate, audio)
    check_true(abs(trimmed.size / rate - 1.1) < 0.2, f"掐掉首尾静音（{audio.size / rate:.2f}s → {trimmed.size / rate:.2f}s）")
    check_true(float(np.max(np.abs(trimmed))) > 0.2, "掐静音没把声音掐掉")

    silence = np.zeros(rate, dtype=np.float32)
    check(silence.size, stub._trim(rate, silence).size, "整段静音时原样返回（不崩）")

    up = _resample(np.arange(100, dtype=np.float32), 8000, 16000)
    check(up.size, 200, "重采样 8k→16k 长度翻倍")
    check(_resample(np.ones(10, dtype=np.float32), 16000, 16000).size, 10, "同采样率不动")


def test_reference_text(settings) -> None:
    print("\n[3] 参考文本（同名 .txt 优先 / 日语转罗马字）")
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    stub = ZipVoiceTts.__new__(ZipVoiceTts)
    stub.settings = settings

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "ref.wav"
        wav.write_bytes(b"")  # 只用来定位同名 .txt
        sidecar = Path(tmp) / "ref.txt"

        sidecar.write_text("这是一段参考文本。\n", encoding="utf-8")
        check(stub._text_for(wav, np.zeros(16, np.float32), 16000), "这是一段参考文本。", "同名 .txt 被读到")

        sidecar.write_text("", encoding="utf-8")
        settings.tts.clone_autotext = False
        check(stub._text_for(wav, np.zeros(16, np.float32), 16000), "", "关掉自动转写且 .txt 为空 → 返回空")

    # 罗马字
    jp = "ドクター、今日の仕事も真面目に。"
    out = stub._normalize_text(jp, quiet=True)
    if out == jp:
        print("  [--]   没装 pykakasi，跳过日语转罗马字（pip install pykakasi）")
    else:
        check_true(not any("\u3041" <= ch <= "\u30ff" for ch in out), f"假名已被转掉（{out[:40]}）")
        check_true(out.isascii(), "罗马字结果全是 ASCII（前端才认）")

    zh = "我在，博士。罗德岛的日程已经排好了。"
    check(stub._normalize_text(zh, quiet=True), zh, "中文参考文本原样保留（不误转）")

    settings.tts.clone_romanize = False
    check(stub._normalize_text(jp, quiet=True), jp, "关掉 romanize 后不动原文")
    settings.tts.clone_romanize = True


def test_persona_fields(settings) -> None:
    print("\n[4] 角色字段")
    char = Character.from_dict(
        {"id": "t", "name": "T", "voice_ref": "a/b.wav", "voice_ref_text": "x"}
    )
    check(char.voice_ref, "a/b.wav", "voice_ref 读得进")
    check(char.voice_ref_text, "x", "voice_ref_text 读得进")
    check_true("voice_ref" in char.to_dict(), "voice_ref 会写回 json")

    from voice_loop.persona import CharacterRegistry

    reg = CharacterRegistry(settings.resolve("data/characters.json"))
    kaltsit = reg.get("kaltsit")
    if kaltsit is None:
        print("  [--]   索引里没有 kaltsit，跳过")
    else:
        check_true(bool(kaltsit.voice_ref), f"kaltsit 配了参考音频（{kaltsit.voice_ref}）")
        ref = settings.resolve(kaltsit.voice_ref)
        check_true(ref.exists(), f"参考音频文件真的在：{ref.name}")


def test_full(settings) -> None:
    print("\n[5] 真的跑一句（--full）")
    from voice_loop.tts.zipvoice_tts import ZipVoiceTts

    settings.tts.backend = "zipvoice"
    settings.tts.clone_audio = "data/personas/kalsit/任命助理.wav"
    settings.tts.clone_text = ""
    engine = ZipVoiceTts(settings)
    check(engine.sample_rate, 24000, "输出采样率 24000 Hz")
    check_true(bool(engine.reference_text), f"拿到参考文本（{engine.reference_text[:24]}…）")
    text = "我在，博士。"
    t0 = time.perf_counter()
    parts = [pcm for _r, pcm in engine.synth(text)]
    elapsed = time.perf_counter() - t0
    pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    audio_s = pcm.size / float(engine.sample_rate)
    check_true(audio_s > 0.3, f"出音了（{audio_s:.2f}s）")
    # 「我在，博士。」大约 1~3 秒；超过 8 秒说明退化成复读乱语了
    check_true(audio_s < 8.0, f"时长没失控（{audio_s:.2f}s，失控会 >10s）")
    check_true(float(np.max(np.abs(pcm))) > 1000, "有实际波形（不是静音）")
    print(f"         RTF {elapsed / audio_s:.2f}（首次含预热，1~2 属正常）")

    print("\n[6] 参考音频不可用时退回 Piper（不报错）")
    settings.tts.clone_audio = "data/personas/kalsit/没有这个文件.wav"
    settings.tts.clone_text = ""
    bare = ZipVoiceTts(settings)
    check(bare.reference, "（未设参考音频）", "参考音频没读到时不报错，只是没设上")
    pieces = [(r, p) for r, p in bare.synth("我在，博士。")]
    rates = {r for r, _ in pieces}
    check(22050 in rates, True, f"改用了 Piper 的采样率出声（{sorted(rates)}）")
    check_true(sum(p.size for _r, p in pieces) > 4000, "确实出音了（不是静音）")


def main() -> int:
    ap = argparse.ArgumentParser(description="音色克隆后端测试")
    ap.add_argument("--full", action="store_true", help="真的加载模型跑一句")
    ap.add_argument("--config", default="", help="配置文件（默认 config.toml）")
    args = ap.parse_args()

    settings = load_settings(args.config or None)
    settings.tts.backend = "piper"  # 先按 piper 测分发，别真加载克隆模型
    test_backend_dispatch(settings)
    test_reference_prep()
    test_reference_text(settings)
    test_persona_fields(settings)
    if args.full:
        test_full(settings)
    else:
        print("\n[5] 跳过真机测试（加 --full 才跑）")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    print("EXIT=" + ("0" if FAIL == 0 else "1"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
