"""加速设备探测与选择（Intel Arc iGPU / NPU / CUDA）。

本机实测（Core Ultra 5 225H + Arc 130T 16GB + AI Boost NPU，见 README 第 12 节）：

    Whisper large-v3-turbo int8，同一段 9.6 秒音频
        CPU   热态 3.79s   RTF 0.393
        GPU   热态 1.05s   RTF 0.109   ← 3.6 倍
        NPU   **进程直接崩**（vpux-compiler: Channels count ... != 128）

所以这里有两条硬规矩：
    1. ``auto`` 只选 GPU，**绝不自动选 NPU**（那个导出在 NPU 上会崩进程，不是抛异常）；
       用户明确写 NPU 也允许，但失败顺序里一定带 CPU 兜底。
    2. 报错要能兜住：GPU 编译失败就回退 CPU，不能把整条 ASR 链路弄没了。

Ollama 那一边（LLM / 视觉模型）在 Windows + Intel 核显上仍然是 100% CPU：
想让它用上 Arc，得换 IPEX-LLM 版的 ollama（README 里有说明）。这里只负责把
``num_gpu`` 这类开关透传给 Ollama，装了能用、没装不报错。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .settings import LlmConfig, Settings


@dataclass
class Accelerators:
    """这台机器上能用的加速器。"""

    openvino: dict[str, str] = field(default_factory=dict)   # {"GPU": "Intel(R) Arc(TM) 130T ..."}
    onnx_providers: list[str] = field(default_factory=list)
    torch_cuda: bool = False
    ollama_vram: dict[str, int] = field(default_factory=dict)  # 模型名 -> 显存占用字节
    ollama_seen: bool = True

    @property
    def has_intel_gpu(self) -> bool:
        return "GPU" in self.openvino

    @property
    def has_npu(self) -> bool:
        return "NPU" in self.openvino

    @property
    def has_cuda(self) -> bool:
        return "CUDAExecutionProvider" in self.onnx_providers or self.torch_cuda

    @property
    def has_dml(self) -> bool:
        return any("Dml" in p or "DirectML" in p for p in self.onnx_providers)


def detect(llm_cfg: LlmConfig | None = None) -> Accelerators:
    """探测加速器。任何一步失败都不抛异常（探测本身不能把服务弄挂）。"""
    acc = Accelerators()

    try:
        import openvino as ov

        core = ov.Core()
        for dev in core.available_devices:
            try:
                acc.openvino[dev] = str(core.get_property(dev, "FULL_DEVICE_NAME"))
            except Exception:  # noqa: BLE001
                acc.openvino[dev] = ""
    except Exception:  # noqa: BLE001
        pass

    try:
        import onnxruntime as ort

        acc.onnx_providers = list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        pass

    try:
        import torch

        acc.torch_cuda = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        acc.torch_cuda = False

    if llm_cfg is not None:
        acc.ollama_vram, acc.ollama_seen = ollama_usage(llm_cfg)
    return acc


def ollama_usage(cfg: LlmConfig) -> tuple[dict[str, int], bool]:
    """问 Ollama：哪些模型正驻留、各占多少显存（0 = 其实在用 CPU）。"""
    try:
        r = requests.get(f"{cfg.host.rstrip('/')}/api/ps", timeout=4)
        r.raise_for_status()
        out: dict[str, int] = {}
        for m in r.json().get("models", []):
            out[m.get("name", "")] = int(m.get("size_vram") or 0)
        return out, True
    except Exception:  # noqa: BLE001
        return {}, False


# --------------------------------------------------------------------------- #
# Ollama 用不用核显（Windows 上默认**丢掉**核显，要 OLLAMA_IGPU_ENABLE=1）
#
# 这块是 **Windows + Intel 核显专属**：
#   - Windows 的 Ollama 默认把集显当不能用的设备丢掉（要手动开开关）；
#   - Linux 上 Ollama 会自己枚举 iGPU（走 sycl/rocm），没有这个开关；
#   - macOS 只有 Metal，也没有这个开关。
# 所以下面两条先判平台，非 Windows 直接返回一句说明，不做任何事。
# --------------------------------------------------------------------------- #
IGPU_ENV = "OLLAMA_IGPU_ENABLE"


def ollama_log_path() -> Path:
    """Ollama 自己的 server 日志在哪（分平台）。"""
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "Ollama" / "server.log"
    # Linux: ~/.ollama/logs/server.log；macOS: ~/Library/Logs/Ollama/server.log
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Ollama" / "server.log"
    return Path.home() / ".ollama" / "logs" / "server.log"


OLLAMA_LOG = ollama_log_path()


def _ollama_log_files() -> list[Path]:
    """server.log + 轮转出来的 server-1.log、server-2.log …（新的在前）。"""
    d = OLLAMA_LOG.parent
    files: list[Path] = []
    if OLLAMA_LOG.exists():
        files.append(OLLAMA_LOG)
    for f in sorted(d.glob("server-*.log"), key=lambda p: p.name):
        files.append(f)
    return files


_LOG_TS = re.compile(r"^time=([0-9T:.+\-]+)\s")
_IGPU_COMPUTE_LINE = re.compile(
    r'library=(\w+).*?name=(\w+).*?description="([^"]*)"'
)


def ollama_gpu_state() -> dict:
    """Ollama 最后选了哪个设备（从它自己的日志里读）。

    为什么要读日志：没加载模型时 ``/api/ps`` 是空的，判断不了；日志里每次启动都会写
    一行 ``inference compute``（选中的设备），核显被丢时则写
    ``dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1``。

    注意日志是**轮转**的（server.log / server-1.log / server-2.log…），
    所以要跨文件按时间戳取最新那条，不能只看当前文件的尾巴。
    """
    out: dict = {"dropped_igpu": False, "device": "", "library": "", "log": str(OLLAMA_LOG)}
    newest_drop = ""
    newest_compute = ""
    for f in _ollama_log_files():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "dropping integrated" not in line and "inference compute" not in line:
                continue
            m = _LOG_TS.match(line)
            ts = m.group(1) if m else ""
            if "dropping integrated" in line:
                if ts >= newest_drop:
                    newest_drop = ts
                continue
            if "inference compute" not in line or "library=cpu" in line:
                continue
            if ts >= newest_compute:
                m2 = _IGPU_COMPUTE_LINE.search(line)
                if m2:
                    newest_compute = ts
                    out["library"] = m2.group(1)
                    out["device"] = m2.group(3)
    # 丢掉核显那条比选中设备那条更「新」，说明现在还在丢
    out["dropped_igpu"] = bool(newest_drop) and newest_drop > newest_compute
    return out


def enable_igpu(persist: bool = True) -> list[str]:
    """把 ``OLLAMA_IGPU_ENABLE=1`` 设到用户环境变量（新起的进程才读得到）。

    只有 Windows + Intel 核显需要这步；其它平台直接说明原因。
    不会重启 Ollama —— 托盘程序得由调用方重启（``restart_ollama()``）。
    """
    notes: list[str] = []
    if os.name != "nt":
        return ["只有 Windows 上的 Ollama 会丢掉核显（需要这个开关），当前系统跳过"]
    if persist:
        try:
            import winreg  # type: ignore

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(key, IGPU_ENV, 0, winreg.REG_SZ, "1")
            winreg.CloseKey(key)
            notes.append(f"已写入用户环境变量 {IGPU_ENV}=1（新开进程/重启 Ollama 后生效）")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"写用户环境变量失败：{exc}（可以在「编辑用户环境变量」里手动加）")
    return notes


def restart_ollama() -> list[str]:
    """重启 Ollama（托盘程序 + 服务），并把 IGPU_ENABLE 注入到新进程里（仅 Windows）。"""
    import time

    notes: list[str] = []
    if os.name != "nt":
        return ["这是 Windows 专属操作；POSIX 上请自己重启 ollama：sudo systemctl restart ollama"]
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "ollama app.exe", "/IM", "ollama.exe"],
            capture_output=True, timeout=20,
        )
        notes.append("已停掉旧的 Ollama 进程")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"停止 Ollama 失败：{exc}")
    if not exe.exists():
        notes.append(f"没找到 {exe}：请从开始菜单重新打开 Ollama")
        return notes
    env = dict(os.environ)
    env[IGPU_ENV] = "1"
    try:
        subprocess.Popen([str(exe)], env=env, close_fds=True)
        time.sleep(6)
        notes.append("已用 OLLAMA_IGPU_ENABLE=1 重新拉起 Ollama（等它起完约 5~10 秒）")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"拉起 Ollama 失败：{exc}，请手动打开")
    return notes



def pick_whisper_devices(want: str) -> list[str]:
    """Whisper 要按什么顺序尝试哪些设备。

    ``auto`` -> GPU（有的话）然后 CPU；显式写 NPU 也允许，但一定会带 CPU 兜底，
    因为这个模型在 NPU 上会把进程搞崩（不是异常，是崩）。
    """
    want = (want or "auto").strip()
    devs = detect().openvino
    upper = want.upper()

    if want == "" or upper in ("AUTO", "自动"):
        order = ["GPU", "CPU"] if "GPU" in devs else ["CPU"]
    elif upper == "NPU":
        order = ["NPU", "CPU"]
    else:
        order = [upper if upper in devs else want, "CPU"]

    out: list[str] = []
    for d in order:
        if d in devs and d not in out:
            out.append(d)
    if "CPU" not in out:
        out.append("CPU")
    return out


def llm_options(cfg: LlmConfig) -> dict:
    """透传给 Ollama 的加速相关选项（没配就不传，保持 Ollama 默认）。"""
    out: dict = {}
    try:
        if int(getattr(cfg, "num_gpu", -1)) >= 0:
            out["num_gpu"] = int(cfg.num_gpu)          # 99 = 尽量全放显存
        if int(getattr(cfg, "num_thread", 0)) > 0:
            out["num_thread"] = int(cfg.num_thread)    # 纯 CPU 时线程数
        if int(getattr(cfg, "num_batch", 0)) > 0:
            out["num_batch"] = int(cfg.num_batch)      # 批大小（预填速度）
    except Exception:  # noqa: BLE001
        return {}
    return out


def report(settings: Settings, logger: logging.Logger | None = None) -> list[str]:
    """给人看的一份报告（`python main.py gpu` 用它）。"""
    acc = detect(settings.llm)
    asr_plan = pick_whisper_devices(settings.asr.whisper_device)
    lines: list[str] = []

    if acc.openvino:
        pretty = "、".join(f"{k}({v})" if v else k for k, v in acc.openvino.items())
        lines.append(f"OpenVINO 可用设备：{pretty}")
    else:
        lines.append("OpenVINO 不可用（ASR/Whisper 只能走默认）")

    lines.append(
        "  显卡加速："
        + ("Intel Arc 核显（GPU）√" if acc.has_intel_gpu else "× 没有 Intel 核显")
        + ("；NPU √（注意：Whisper 在 NPU 上会崩进程，别选）" if acc.has_npu else "")
    )
    lines.append(
        "  onnxruntime provider："
        + ("、".join(acc.onnx_providers) or "（无）")
        + ("" if (acc.has_cuda or acc.has_dml) else "  → TTS 只能跑 CPU（Piper RTF 0.04，够快）")
    )
    if bool(getattr(settings.tts, "use_cuda", False)) and not acc.has_cuda:
        lines.append("  ! [tts] use_cuda = true，但本机没有 CUDA 的 onnxruntime：Piper 仍在 CPU 跑")

    dev = str(settings.asr.whisper_device)
    lines.append(f"Whisper 设备配置 = {dev}  →  实际尝试顺序：{' → '.join(asr_plan)}")
    if dev.strip().upper() in ("NPU",):
        lines.append("  ! 你把 NPU 写死了：这个 Whisper 导出在 NPU 上会崩进程，崩溃就回不到 CPU 了")

    if not acc.ollama_seen:
        lines.append("Ollama：连不上（模型只能跑 CPU 或没启动）")
    else:
        state = ollama_gpu_state()
        if state["dropped_igpu"] and state["library"].lower() != "vulkan":
            lines.append(f"Ollama：{IGPU_ENV} 没开，核显被丢掉了")
        if state["device"]:
            lines.append(
                f"Ollama 选中设备：{state['device']}（{state['library']}，{'核显' if 'vulkan' in state['library'].lower() else state['library']}）"
            )
        if state["dropped_igpu"] and "iGPU" not in state.get("device", ""):
            lines.append(
                f"  → 想用核显：{IGPU_ENV}=1 后重启 Ollama（一条命令：python main.py gpu --enable-igpu）"
            )
        if not acc.ollama_vram:
            lines.append("Ollama：当前没有模型驻留（问一句它就加载了，那时再看 `ollama ps`）")
        else:
            for name, vram in acc.ollama_vram.items():
                where = "核显/显存" if vram > 0 else "CPU"
                lines.append(f"Ollama/{name}：占用 {vram / 2**30:.1f} GiB → {where}")
    return lines


__all__ = [
    "Accelerators",
    "IGPU_ENV",
    "detect",
    "enable_igpu",
    "llm_options",
    "ollama_gpu_state",
    "ollama_usage",
    "pick_whisper_devices",
    "report",
    "restart_ollama",
]
