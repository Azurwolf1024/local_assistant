"""声纹尺子自测：向量/余弦/质心/阈值 + 真模型上的分辨力验证。

为什么要专门守这个（2026-09-30）：这把尺子是**新加的裁判** ——
以后「换参考有没有更像她」「换精度会不会掉音色」都会拿它下结论。
裁判自己错的话，后面所有结论都跟着错（工程日志 §19 那条「尺子比结论更重要」）。
所以这里既测纯数学，也在真模型上验一遍**同人 > 跨人**。

    python scripts\\test_speaker.py
"""

from __future__ import annotations

import itertools
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.spk_check import collect, label          # noqa: E402
from voice_loop.settings import load_settings          # noqa: E402
from voice_loop.tts import pacing, speaker, zipvoice_tts  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()      # ★哨兵★：want=None 是「期望就是 None」，不等于「没给期望」


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    shown = detail or f"{got!r}"
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{shown}{tail}")
    if not ok:
        _failures.append(name)


def make_wav(path: Path, seconds: float = 2.0, rate: int = 16000, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * seconds)
    tone = (np.sin(2 * np.pi * 220 * np.arange(frames) / rate) * 8000).astype("<i2")
    data = np.repeat(tone, channels).tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data)
    return path


# --------------------------------------------------------------------------- #
def test_read_audio(tmp: Path) -> None:
    print("\n[1] 读音频：单声道/立体声/读不了")
    mono = make_wav(tmp / "mono.wav", 1.0, 16000, 1)
    x, rate = speaker.read_audio(mono)
    check("单声道 16k", (x.size, rate), (16000, 16000))
    check("幅度在 ±1 里", (float(x.min()) >= -1.0, float(x.max()) <= 1.0), (True, True))
    stereo = make_wav(tmp / "stereo.wav", 1.0, 16000, 2)
    y, rate2 = speaker.read_audio(stereo)
    check("立体声压成单声道（不是取一半样本）", (y.size, rate2), (16000, 16000))
    check("44.1k 也能读", speaker.read_audio(make_wav(tmp / "hi.wav", 0.5, 44100))[1], 44100)
    empty, rate3 = speaker.read_audio(tmp / "nope.wav")
    check("读不了就返回空 + 0（不抛）", (empty.size, rate3), (0, 0))


def test_cosine() -> None:
    print("\n[2] 余弦：边界情况不能抛")
    a = np.array([1.0, 2.0, 3.0])
    check("自己跟自己 = 1", round(speaker.cosine(a, a), 6), 1.0)
    check("取反 = -1", round(speaker.cosine(a, -a), 6), -1.0)
    check("正交 = 0", speaker.cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
    check("零向量 = 0（不是 nan）", speaker.cosine([0.0, 0.0], a), 0.0)
    check("长度不等 = 0（不是报错）", speaker.cosine([1.0], [1.0, 2.0]), 0.0)
    check("空 = 0", speaker.cosine([], []), 0.0)
    check("长度不影响（只看方向）", round(speaker.cosine(a, a * 7.0), 6), 1.0)


def test_centroid() -> None:
    print("\n[3] 质心：一组向量 → 归一化的「这个人」")
    vs = [np.array([1.0, 0.0]), np.array([0.9, 0.1]), np.array([1.0, -0.1])]
    c = speaker.centroid(vs)
    check("维度对", c.size, 2)
    check("已归一化（模长 1）", round(float(np.linalg.norm(c)), 6), 1.0)
    check("跟成员都算「同人」（>0.9）", all(speaker.cosine(v, c) > 0.9 for v in vs), True)
    check("空输入 → 空数组（不抛）", speaker.centroid([]).size, 0)
    check("忽略空向量", speaker.centroid([np.zeros(0), vs[0]]).size, 2)


def test_thresholds() -> None:
    print("\n[4] 判读阈值（量出来的：同人 0.635~0.977 / 跨人 0.313~0.535）")
    check("0.90 → 同人", speaker.verdict(0.90), "同人")
    check("0.65 → 同人（同人最低那档）", speaker.verdict(0.65), "同人")
    check("0.55 → 存疑（跨人最高那档）", speaker.verdict(0.55), "存疑")
    check("0.44 → 不像（跨人中位）", speaker.verdict(0.44), "不像")
    check("阈值写成常量、不是散在代码里",
          (speaker.SAME_SPEAKER, speaker.UNSURE_SPEAKER), (0.60, 0.50))


def test_missing_model(tmp: Path) -> None:
    print("\n[5] 模型不在时：给人话，别装作能用")
    settings = load_settings()
    settings.tts.speaker_model = str(tmp / "不存在.onnx")
    text = speaker.missing(settings)
    check("提示里带下载命令", "download_models.py --only speaker" in text, True)
    emb = speaker.Embedder(tmp / "不存在.onnx")
    check("ready() 为假", emb.ready(), False)
    check("problem 说清为什么", bool(emb.problem), True)
    check("embedding 返回 None（不抛）", emb.embedding(np.zeros(8000, np.float32), 16000), None)
    check("load() 也返回 None", speaker.load(settings), None)


def test_resample_single_source() -> None:
    print("\n[6] 重采样只有一份实现（别在第二个地方又抄一遍）")
    up = pacing.resample(np.arange(100, dtype=np.float32), 8000, 16000)
    check("8k→16k 样本数翻倍", up.size, 200)
    check("同采样率不动", pacing.resample(np.ones(10, np.float32), 16000, 16000).size, 10)
    check("空输入不抛", pacing.resample(np.zeros(0, np.float32), 8000, 16000).size, 0)
    check("zipvoice_tts._resample 走的是同一份（旧名字保留给测试）",
          np.allclose(zipvoice_tts._resample(np.arange(50, dtype=np.float32), 8000, 16000),
                      pacing.resample(np.arange(50, dtype=np.float32), 8000, 16000)), True)


def test_real_model(tmp: Path) -> None:
    print("\n[7] 真模型分辨力：同人必须 > 跨人（没有素材/模型就跳过）")
    settings = load_settings()
    if speaker.missing(settings):
        print(f"    · 跳过：{speaker.missing(settings).strip()}")
        return
    kal = ROOT / "data" / "personas" / "kaltsit"
    ami = ROOT / "data" / "personas" / "amiya"
    kal_files = sorted(kal.glob("*.wav"))[:4]
    ami_files = sorted(ami.glob("*.wav"))[:4]
    if len(kal_files) < 2 or len(ami_files) < 2:
        print("    · 跳过：本机没有素材音频（*.wav 不入库）")
        return
    emb = speaker.load(settings)
    check("模型能建起来", emb is not None, True)
    if emb is None:
        return
    kv = [v for v in (emb.embedding_file(p) for p in kal_files) if v is not None]
    av = [v for v in (emb.embedding_file(p) for p in ami_files) if v is not None]
    check("素材都能算出向量", (len(kv), len(av)), (len(kal_files), len(ami_files)))
    if not kv or not av:
        return
    check("自比 = 1.0", round(speaker.cosine(kv[0], kv[0]), 6), 1.0)
    same = [speaker.cosine(a, b) for a, b in itertools.combinations(kv, 2)]
    cross = [speaker.cosine(a, b) for a in kv for b in av]
    check("同人相似度全部 > 0.60（阈值那一档）", all(s > speaker.SAME_SPEAKER for s in same), True,
          detail=f"最低 {min(same):.3f}")
    check("跨人相似度全部 < 0.60", all(s < speaker.SAME_SPEAKER for s in cross), True,
          detail=f"最高 {max(cross):.3f}")
    check("同人 > 跨人（分得开）", min(same) > max(cross), True)
    # 质心也当成「像不像她」的基准用：每个成员对质心都应该落在同人档
    cen = speaker.centroid(kv)
    check("成员对自己角色的质心都在同人档",
          all(speaker.cosine(v, cen) >= speaker.SAME_SPEAKER for v in kv), True)
    # 合成输出（有就跑）—— 这是真正的正题：合出来的像不像她
    samples = sorted((ROOT / "sessions" / "tts_clone_probe").glob("spk_base_*.wav"))
    if samples:
        got = [round(speaker.cosine(emb.embedding_file(p), cen), 3)
               for p in samples if emb.embedding_file(p) is not None]
        if got:
            print(f"    · 现有合成样本 vs 凯尔希素材质心：{got}（内部两两中位约 0.82）")
            check("合成样本也算「同人」", all(s >= speaker.SAME_SPEAKER for s in got), True)
    else:
        print("    · 没有 sessions/tts_clone_probe/spk_base_*.wav（先合两句再量）")


def test_cli_helpers(tmp: Path) -> None:
    print("\n[8] 命令行的小工具函数")
    room = tmp / "room"
    make_wav(room / "a.wav", 1.0)
    (room / "note.txt").write_text("不是音频", encoding="utf-8")
    files = collect([str(room)])
    check("目录只收音频", [p.name for p in files], ["a.wav"])
    check("找不到的路径不炸", collect([str(tmp / "没有这个")]), [])
    check("重复路径去重", len(collect([str(room / "a.wav"), str(room / "a.wav")])), 1)
    check("相对路径显示成正斜杠（好复制）",
          label(ROOT / "data" / "personas", ROOT), "data/personas")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="speaker_test_"))
    try:
        test_read_audio(tmp / "a")
        test_cosine()
        test_centroid()
        test_thresholds()
        test_missing_model(tmp / "b")
        test_resample_single_source()
        test_real_model(tmp / "c")
        test_cli_helpers(tmp / "d")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 声纹尺子自测全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
