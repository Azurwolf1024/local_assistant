"""加速设备选择的离线测试（不加载模型、不跑推理，秒级）。

为什么要测这些：设备选错不是「慢一点」，是**会崩或者悄悄降级**：
    - Whisper 这个动态形状的导出在 NPU 上连编译都过不了，而且就算固定形状，
      核显也比它快约 8 倍 → auto 绝不能选 NPU（数字见 scripts/probe_npu.py）
    - INFERENCE_NUM_THREADS 是 CPU 专属属性，塞给 GPU 会让编译失败、整条 Whisper 路径降级
    - 控制台是 cp936：报告里的字符必须能 GBK 编码，否则打印就炸
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile  # noqa: E402

from voice_loop.accel import (  # noqa: E402
    detect,
    llm_options,
    pick_whisper_devices,
    report,
)
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, expect=None, contains: str | None = None) -> None:
    ok = True
    if expect is not None:
        ok = got == expect
    elif contains is not None:
        ok = contains in str(got)
    print(f"  {PASS if ok else FAIL} {name}: {got!r}" + ("" if ok else f"   (期望 {expect or contains!r})"))
    if not ok:
        _failures.append(name)


def test_pick() -> None:
    print("\n[1] Whisper 设备选择")
    acc = detect()
    print(f"    本机 OpenVINO 设备：{list(acc.openvino)}")

    auto = pick_whisper_devices("auto")
    check("auto 一定带 CPU 兜底", auto[-1], "CPU")
    check("auto 绝不选 NPU", "NPU" in auto, False)
    if acc.has_intel_gpu:
        check("有核显时 auto 先选 GPU", auto[0], "GPU")
    else:
        check("没核显时 auto 就 CPU", auto, ["CPU"])
    check("大小写/空值也认", pick_whisper_devices("")[:1], auto[:1])
    check("写 CPU 就只试 CPU（不兜到别处）", pick_whisper_devices("CPU"), ["CPU"])
    check("写 GPU 时带 CPU 兜底", pick_whisper_devices("GPU")[-1], "CPU")
    check("写 NPU 时也带 CPU 兜底（它可能崩）", pick_whisper_devices("NPU")[-1], "CPU")
    check("写不存在的设备名时退回 CPU", "CPU" in pick_whisper_devices("XYZ"), True)
    check("不会重复同一个设备", len(set(auto)), len(auto))


def test_llm_options() -> None:
    print("\n[2] Ollama 加速选项透传")
    st = load_settings()
    cfg = st.llm
    check("默认一个都不传（保持 Ollama 自己判断）", llm_options(cfg), {})
    cfg.num_gpu = 99
    check("num_gpu 配了就传", llm_options(cfg), {"num_gpu": 99})
    cfg.num_thread = 8
    cfg.num_batch = 512
    check("三个都在", llm_options(cfg), {"num_gpu": 99, "num_thread": 8, "num_batch": 512})
    cfg.num_gpu = 0
    check("num_gpu=0 是合法值（=不要放显存）", llm_options(cfg).get("num_gpu"), 0)
    cfg.num_gpu = -1
    cfg.num_thread = 0
    cfg.num_batch = 0
    check("改回默认又不传了", llm_options(cfg), {})


def test_report() -> None:
    print("\n[3] 报告文本（控制台是 cp936，不能有编码不了的字符）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_accel_"))
    st = load_settings()
    st.skills.data_dir = str(tmp)
    lines = report(st)
    check("报告有内容", len(lines) > 2, True)
    joined = "\n".join(lines)
    check("提到 OpenVINO 设备", "设备" in joined, True)
    check("提到 Ollama 用 CPU 还是 GPU", "Ollama" in joined, True)
    try:
        joined.encode("gbk")
        check("全都能 GBK 编码（能直接打印）", True)
    except UnicodeEncodeError as exc:
        check("全都能 GBK 编码（能直接打印）", f"编码失败：{exc}", True)
    for line in lines:
        print(f"      {line}")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_no_crash_when_missing() -> None:
    print("\n[4] 探测本身不能把服务弄挂")
    from voice_loop.accel import Accelerators, ollama_usage

    a = Accelerators()
    check("空对象不报错", (a.has_intel_gpu, a.has_npu, a.has_cuda, a.has_dml),
          (False, False, False, False))
    vram, seen = ollama_usage(load_settings().llm)
    check("Ollama 探测不抛异常", isinstance(vram, dict) and isinstance(seen, bool), True)


def test_ollama_state() -> None:
    print("\n[5] 从 Ollama 日志判断它用没用核显（日志会轮转，要按时间戳取最新）")
    import tempfile as _tf

    from voice_loop import accel

    old = accel.OLLAMA_LOG
    tmp = Path(_tf.mkdtemp(prefix="voiceloop_ollog_"))
    try:
        # 造三个文件：server.log（当前）、server-1.log、server-2.log
        (tmp / "server.log").write_text(
            'time=2026-09-18T23:55:21.660+08:00 level=INFO source=runner.go:405 '
            'msg="dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1" '
            'id=0 library=Vulkan compute=0.0 name=Vulkan0 description="Intel(R) Arc(TM) 130T GPU (16GB)"\n'
            'time=2026-09-18T23:55:21.660+08:00 level=INFO source=types.go:50 '
            'msg="inference compute" id=cpu library=cpu compute="" name=cpu description=cpu\n'
            "[GIN] 2026/09/18 - 23:57:31 | 200 |    0s | 127.0.0.1 | GET  \"/api/ps\"\n",
            encoding="utf-8",
        )
        (tmp / "server-1.log").write_text(
            'time=2026-09-18T23:30:00.000+08:00 level=INFO source=runner.go:405 '
            'msg="dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1" '
            'library=Vulkan name=Vulkan0 description="Intel(R) Arc(TM) 130T GPU (16GB)"\n'
            'time=2026-09-18T23:40:00.000+08:00 level=INFO source=types.go:32 '
            'msg="inference compute" id=0 library=Vulkan compute=0.0 name=Vulkan0 '
            'description="Intel(R) Arc(TM) 130T GPU (16GB)" type=iGPU total="18.0 GiB"\n',
            encoding="utf-8",
        )
        accel.OLLAMA_LOG = tmp / "server.log"
        st = accel.ollama_gpu_state()
        check("认得出核显被丢掉了", st["dropped_igpu"], True)
        check(
            "丢弃那条更新时，旧日志里的设备名不算「在用核显」",
            bool(st["device"]) and not st["dropped_igpu"],
            False,
        )

        # 现在把「启用了核显」的更新日志追加进去（比丢弃那条更新）
        with open(tmp / "server.log", "a", encoding="utf-8") as f:
            f.write(
                'time=2026-09-18T23:55:36.441+08:00 level=INFO source=types.go:32 '
                'msg="inference compute" id=0 filter_id=0 library=Vulkan compute=0.0 '
                'name=Vulkan0 description="Intel(R) Arc(TM) 130T GPU (16GB)" '
                'libdirs=ollama,vulkan type=iGPU total="18.0 GiB" available="17.2 GiB"\n'
            )
        st = accel.ollama_gpu_state()
        check("新增一条更晚的「启用了核显」后不再算丢弃", st["dropped_igpu"], False)
        check("认得出设备名", "Arc" in st["device"], True)
        check("认得出后端是 Vulkan", st["library"], "Vulkan")

        # 只有旧文件里有记录（当前文件是空的）也要能读到
        (tmp / "server.log").write_text("[GIN] 2026/09/18 - 23:59:00 | 200 | 0s | GET \"/\"\n",
                                        encoding="utf-8")
        st = accel.ollama_gpu_state()
        check("当前日志没有记录时，会去轮转文件里找", "Arc" in st["device"], True)
    finally:
        accel.OLLAMA_LOG = old
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 66)
    print(" 加速设备选择测试")
    print("=" * 66)
    test_pick()
    test_llm_options()
    test_report()
    test_no_crash_when_missing()
    test_ollama_state()
    print("\n" + "=" * 66)
    if _failures:
        print(f" 失败 {len(_failures)} 项：{_failures}")
        print("=" * 66)
        return 1
    print(" 全部通过 √")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
