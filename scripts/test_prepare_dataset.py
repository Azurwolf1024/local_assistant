"""数据准备脚本（scripts/prepare_tts_dataset.py）的离线测试。

守住几件事：TSV 格式对、采样率一定是 24k、清单文本配得上音频、
train/dev 不重不漏、路径前缀能改（WSL 用）、非空输出目录不覆盖。

    python scripts/test_prepare_dataset.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_tts_dataset.py"

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


def run(*args: str) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def make_wav(path: Path, seconds: float, rate: int = 44100, lead_silence: float = 0.0) -> None:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = 0.5 * np.sin(2 * np.pi * 220 * t)
    if lead_silence > 0:
        pad = np.zeros(int(rate * lead_silence))
        x = np.concatenate([pad, x])
    sf.write(str(path), x.astype(np.float32), rate, subtype="PCM_16")


NAMES = ["任命助理", "交谈1", "交谈2", "交谈3", "信赖触摸"]

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    src = tmp_path / "src"
    src.mkdir()
    for i, name in enumerate(NAMES):
        # 第三条故意夹 1 秒开头静音，验证会被掐掉
        make_wav(src / f"{name}.wav", 1.0 + i * 0.5, lead_silence=1.0 if i == 2 else 0.0)
    make_wav(src / "太短.wav", 0.2)
    (src / "kaltsit.txt").write_text(
        "\n\n".join(f"{n}\n这是第{ i + 1 }条台词。" for i, n in enumerate(NAMES))
        + "\n\n太短\n嗯。\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    print("== 1. 试运行不写文件 ==")
    code, text = run("--dir", str(src), "--out", str(out))
    check(code, 0, "试运行退出码 0")
    check_true("[试运行]" in text, "提示这是试运行")
    check(out.exists(), False, "试运行确实没建目录")
    check_true("可用 5 对" in text, "识别出 5 对可用")

    print("\n== 2. 真写：TSV 与音频 ==")
    code, text = run("--dir", str(src), "--out", str(out), "--apply")
    check(code, 0, "--apply 退出码 0")
    train = (out / "custom_train.tsv").read_text(encoding="utf-8").strip().splitlines()
    dev = (out / "custom_dev.tsv").read_text(encoding="utf-8").strip().splitlines()
    check(len(train) + len(dev), 5, "train + dev = 5 行")
    check_true(len(dev) >= 1, f"验证集非空（{len(dev)} 行）")
    wavs = sorted(p.name for p in (out / "wavs").glob("*.wav"))
    check(len(wavs), 5, "写出 5 个 wav")
    check_true("太短.wav" not in wavs, "过短的没写出去")

    fields_ok = True
    ids = []
    for line in train + dev:
        parts = line.split("\t")
        if len(parts) != 3:
            fields_ok = False
            break
        ids.append(parts[0])
        if not parts[1].strip():
            fields_ok = False
        if not parts[2].endswith(".wav"):
            fields_ok = False
    check_true(fields_ok, "每行都是 id \\t 文本 \\t wav 路径")
    check(len(set(ids)), 5, "id 不重复")
    id_set = set(ids)
    check_true(all(n in id_set for n in NAMES), "5 条都在（按名字对上了音频）")

    print("\n== 3. 采样率与静音裁剪 ==")
    rates = set()
    secs = {}
    for p in (out / "wavs").glob("*.wav"):
        data, rate = sf.read(str(p))
        rates.add(rate)
        secs[p.stem] = data.size / rate
    check(sorted(rates), [24000], "全部重采样到 24000 Hz")
    # 交谈2 是「1s 静音 + 2s 正弦」= 3s，裁完应只剩 2s 出头
    check_true(1.9 < secs.get("交谈2", 0) < 2.3, f"开头 1s 静音被掐掉（交谈2 = {secs.get('交谈2', 0):.2f}s）")
    check_true(abs(secs.get("任命助理", 0) - 1.0) < 0.15, f"无静音的那条基本没变（{secs.get('任命助理', 0):.2f}s）")

    print("\n== 4. 文本内容 ==")
    all_text = {line.split("\t")[0]: line.split("\t")[1] for line in train + dev}
    check(all_text["交谈1"], "这是第2条台词。", "文本按名字正确配对")

    print("\n== 5. 非空目录保护 ==")
    code, _ = run("--dir", str(src), "--out", str(out), "--apply")
    check_true(code != 0, "非空输出目录会拒绝（要 --force）")
    code, _ = run("--dir", str(src), "--out", str(out), "--apply", "--force")
    check(code, 0, "--force 时能覆盖")

    print("\n== 6. 路径前缀（WSL 用） ==")
    out2 = tmp_path / "out2"
    code, _ = run(
        "--dir", str(src), "--out", str(out2), "--apply", "--path-prefix", "/mnt/d/local_AI/"
    )
    check(code, 0, "带前缀时退出码 0")
    first = (out2 / "custom_train.tsv").read_text(encoding="utf-8").strip().splitlines()[0]
    check_true(first.split("\t")[2].startswith("/mnt/d/local_AI/"), "TSV 路径带上了前缀")
    check_true("/" in first.split("\t")[2], "路径用正斜杠（Linux 侧能读）")

    print("\n== 7. 验证集条数可控 ==")
    out3 = tmp_path / "out3"
    code, _ = run("--dir", str(src), "--out", str(out3), "--apply", "--dev-count", "2")
    dev3 = (out3 / "custom_dev.tsv").read_text(encoding="utf-8").strip().splitlines()
    check(len(dev3), 2, "--dev-count 2 → 验证集 2 条")

    print("\n== 8. 没有文本的音频要报告 ==")
    src4 = tmp_path / "src4"
    src4.mkdir()
    make_wav(src4 / "有文本.wav", 1.5)
    make_wav(src4 / "没文本.wav", 1.5)
    (src4 / "m.txt").write_text("有文本\n一句话。\n", encoding="utf-8")
    code, text = run("--dir", str(src4), "--out", str(tmp_path / "out4"))
    check(code, 0, "缺文本也能继续")
    check_true("没文本.wav" in text, "明确报告跳过了谁")

print(f"\n通过 {PASS} / 失败 {FAIL}")
print(f"EXIT={0 if FAIL == 0 else 1}")
sys.exit(0 if FAIL == 0 else 1)
