"""模型精度（int8 / fp32）与平台适配的离线测试。

要守住的底线：
1. **精度是音质取舍，不是功能开关**——配置写了 fp32 但目录里只有 int8 时，
   必须退回 int8 继续出声，不能报错、更不能哑；
2. 缺文件检查（missing_files / pipeline / voice_data）要跟运行时**用同一套判断**，
   否则会出现「报告说缺文件、实际能跑」这种自相矛盾；
3. 精度文件名不能互相串（`text_encoder.onnx` 的 glob 不能把 int8 那份也匹配进来）；
4. 平台相关的代码在**别的系统上也不能抛异常**（这里用替换 os.name 的方式模拟）。

    python scripts/test_precision.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import system_ops  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.tts import precision as prec  # noqa: E402
from voice_loop.tts import zipvoice_tts as zv  # noqa: E402
from voice_loop.voice_data import VoiceData  # noqa: E402

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


def fake_persona_dir(base: Path, precision: str) -> Path:
    """造一个只装了某一种精度的角色模型目录。"""
    d = base / f"persona_{precision}"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("tokens.txt", "lexicon.txt"):
        (d / name).write_text("x", encoding="utf-8")
    (d / "espeak-ng-data").mkdir(exist_ok=True)
    for name in zv.PRECISION_FILES[precision]:
        (d / name).write_bytes(b"x")
    return d


def test_mapping() -> None:
    print("\n[1] 精度 → 文件名的映射")
    check(zv.PRECISION_FILES["int8"], ("encoder.int8.onnx", "decoder.int8.onnx"), "int8 文件名")
    check(zv.PRECISION_FILES["fp32"], ("encoder.onnx", "decoder.onnx"), "fp32 文件名")
    check(zv.DEFAULT_PRECISION, "int8", "默认精度是 int8（当前验证过的那个）")

    # 导出目录里的 glob 不能互相串
    from scripts.finetune_zipvoice import PRECISION_FILES as EXPORT

    names = [target for _pattern, target in EXPORT["int8"]] + [target for _pattern, target in EXPORT["fp32"]]
    check(sorted(names), sorted(["encoder.int8.onnx", "decoder.int8.onnx", "encoder.onnx", "decoder.onnx"]), "导出两侧名字不重复")
    fp32_patterns = [p for p, _t in EXPORT["fp32"]]
    check_true(all("int8" not in p for p in fp32_patterns), "fp32 的 glob 里不含 int8 字样")


def test_want_precision() -> None:
    print("\n[2] 配置解析：认不出来的值当 int8")
    settings = load_settings()
    for raw, expect in (("int8", "int8"), ("fp32", "fp32"), ("FP32", "fp32"), (" fp32 ", "fp32"),
                        ("", "int8"), ("float16", "int8"), ("none", "int8")):
        settings.tts.clone_precision = raw
        check(zv.want_precision(settings), expect, f"clone_precision={raw!r}")


def test_fallback() -> None:
    print("\n[3] 要的精度不在 → 退回 int8（不能哑）")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        only_int8 = fake_persona_dir(base, "int8")
        check(zv.model_names(only_int8, "fp32"), zv.PRECISION_FILES["int8"], "只有 int8 却要 fp32 → 用 int8")
        check(zv.model_names(only_int8, "int8"), zv.PRECISION_FILES["int8"], "要 int8 → 用 int8")

        only_fp32 = fake_persona_dir(base, "fp32")
        check(zv.model_names(only_fp32, "fp32"), zv.PRECISION_FILES["fp32"], "只有 fp32 也能用")
        check(
            zv.model_names(only_fp32, "int8"),
            zv.PRECISION_FILES["fp32"],
            "只有 fp32 而配置写 int8 → 就用 fp32（不能因为配置写错就判死）",
        )

        both = base / "both"
        both.mkdir()
        for names in zv.PRECISION_FILES.values():
            for name in names:
                (both / name).write_bytes(b"x")
        check(zv.model_names(both, "fp32"), zv.PRECISION_FILES["fp32"], "两份都在：fp32 优先按配置")
        check(zv.model_names(both, "int8"), zv.PRECISION_FILES["int8"], "两份都在：int8 也认")


def test_missing_files() -> None:
    print("\n[4] 缺文件检查跟着精度走（和运行时同一套判断）")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        d = fake_persona_dir(base, "int8")
        settings = load_settings()
        # 声码器用真的（否则永远报缺），临时把路径指到仓库里存在的那个
        settings.tts.clone_dir = str(d)
        vocoder = ROOT / "models" / "tts" / "zipvoice" / "vocos_24khz.onnx"
        settings.tts.clone_vocoder = str(vocoder)

        settings.tts.clone_precision = "int8"
        check(zv.missing_files(settings), [], "int8 齐了 → 不缺")

        settings.tts.clone_precision = "fp32"
        check(zv.missing_files(settings), [], "要 fp32 而只有 int8 → 也不缺（自动用 int8，由运行时提醒一句）")

        # 两份都没有才报缺，而且报的是**配置里那个精度**的名字
        for name in zv.PRECISION_FILES["int8"]:
            (d / name).unlink()
        missing = [p.name for p in zv.missing_files(settings)]
        check(sorted(missing), ["decoder.onnx", "encoder.onnx"], "两份都没有 → 报缺配置的那个精度")

        # 只有 fp32 时，配置写 int8 也得能用（不能因为「默认是 int8」就把文件齐全的目录判死）
        for name in zv.PRECISION_FILES["fp32"]:
            (d / name).write_bytes(b"x")
        settings.tts.clone_precision = "int8"
        check(zv.model_names(d, "int8"), zv.PRECISION_FILES["fp32"], "只有 fp32 而配置写 int8 → 就用 fp32")
        check(zv.missing_files(settings), [], "只有 fp32 而配置写 int8 → 不算缺")

        # voice_data 的 trained：两种精度任一套算数
        item = VoiceData(id="t", name="测试", audio_dir=d, model_dir=d)
        check_true(item.trained, "voice_data.trained 认 fp32（当前只有 fp32）")
        for name in zv.PRECISION_FILES["int8"]:
            (d / name).write_bytes(b"x")
        check_true(item.trained, "两份都在也算已训")
        for name in zv.PRECISION_FILES["int8"]:
            (d / name).unlink()
        for name in zv.PRECISION_FILES["fp32"]:
            (d / name).unlink()
        check_true(not item.trained, "两份都没有 → 不算已训")


def test_pipeline_missing() -> None:
    print("\n[5] 切角色时的模型完整性检查（pipeline）")
    from voice_loop.pipeline import VoiceLoop

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        loop = object.__new__(VoiceLoop)
        loop.settings = load_settings()
        for precision in ("int8", "fp32"):
            d = fake_persona_dir(base, precision)
            check(loop._missing_voice_model(d), [], f"只有 {precision} 的目录 → 不算缺")
        empty = base / "empty"
        empty.mkdir()
        missing = loop._missing_voice_model(empty)
        check_true(len(missing) == 3, f"空目录 → 报缺 tokens/lexicon/espeak（得到 {len(missing)} 项）")
        half = base / "half"
        half.mkdir()
        for name in ("tokens.txt", "lexicon.txt"):
            (half / name).write_text("x", encoding="utf-8")
        (half / "espeak-ng-data").mkdir()
        check_true(bool(loop._missing_voice_model(half)), "文件都在但没有 onnx → 算缺")


def test_platform_names() -> None:
    print("\n[6] 平台判断与降级（不能抛异常）")
    name = system_ops.platform_name()
    check_true(name in ("windows", "macos", "linux") or sys.platform in name, f"platform_name() = {name!r}")
    check_true(isinstance(system_ops.supported(), bool), "supported() 返回 bool")
    ok, why = system_ops.monitor_off()
    check_true(isinstance(ok, bool) and isinstance(why, str), f"monitor_off() 返回 (bool, str) —— {why}")
    ok, why = system_ops.monitor_on()
    check_true(isinstance(ok, bool) and isinstance(why, str), f"monitor_on() 返回 (bool, str) —— {why}")
    check_true(isinstance(system_ops.power_info(), str), f"power_info() 返回 строку —— {system_ops.power_info()}")

    # 非 Windows 上「关屏」应该老实说不支持，而不是假装成功
    saved = system_ops.platform_name
    try:
        system_ops.platform_name = lambda: "plan9"  # type: ignore[assignment]
        ok, why = system_ops.monitor_off()
        check(ok, False, "未知平台 → monitor_off 明确返回失败")
        check_true("暂不支持" in why, f"说明里说清了原因：{why}")
        check(system_ops.supported(), False, "未知平台 → supported() 为假")
    finally:
        system_ops.platform_name = saved  # type: ignore[assignment]


def test_single_source() -> None:
    print("\n[7] 精度定义只有一份（四个调用方共用）")
    check_true(zv.PRECISION_FILES is prec.PRECISION_FILES, "zipvoice_tts 转发的是 precision 里那一份")
    check_true(zv.want_precision is prec.want_precision, "want_precision 同一个函数")
    check_true(zv.model_names is prec.model_names, "model_names 同一个函数")
    hits: list[str] = []
    for path in sorted((ROOT / "voice_loop").rglob("*.py")):
        if path.name == "precision.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "encoder.int8.onnx" in line or "decoder.int8.onnx" in line:
                hits.append(f"{path.relative_to(ROOT)}:{lineno}")
    check(hits, [], "除了 precision.py，voice_loop/ 里没有第二份 int8 文件名")


def main() -> int:
    test_mapping()
    test_want_precision()
    test_fallback()
    test_missing_files()
    test_pipeline_missing()
    test_platform_names()
    test_single_source()
    print(f"\n{'=' * 60}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
