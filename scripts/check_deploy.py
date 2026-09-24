"""搬家自检：把这份项目搬到另一台机器（或另一个系统）之前，先跑一遍这个。

它是**只读**的：不改配置、不下载模型、不加载模型（所以几秒就完），
只回答一个问题——「在这儿跑，哪些会立刻坏、哪些只是少个功能」。

    python scripts/check_deploy.py            # 人看
    python scripts/check_deploy.py --json     # 给脚本/CI 看
    python scripts/check_deploy.py --strict   # 把「可选缺失」也算失败（部署镜像自检用）

检查七类东西（每类都会说清「为什么」和「怎么办」）：

    1 环境      OS / Python 版本 / 路径里有空格或非 ASCII（原生库最容易栽这儿）
    2 依赖      必需（缺了主流程起不来）/ 可选（只在某个后端下需要）
    3 配置      config.toml 里每个路径是否真的存在、有没有写死绝对路径
    4 模型      models/ 下的文件在不在、体积对不对
    5 服务      Ollama 通不通、模型拉过没；麦克风/扬声器是否可见
    6 平台能力  关屏 / 字幕点穿 / 后台服务 / iGPU 开关 —— 换系统后哪些会退化
    7 磁盘      写权限、可用空间（模型动辄几个 GB）

退出码：0 = 没有「必需」级别的失败；1 = 有。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_MARK = {OK: "[ OK ]", WARN: "[警告]", FAIL: "[失败]", INFO: "[提示]"}


@dataclass
class Item:
    section: str
    level: str
    name: str
    detail: str = ""


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)

    def add(self, section: str, level: str, name: str, detail: str = "") -> None:
        self.items.append(Item(section, level, name, detail))

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if i.level == FAIL]

    def render(self) -> str:
        lines: list[str] = []
        section = ""
        for item in self.items:
            if item.section != section:
                section = item.section
                lines.append("")
                lines.append(f"── {section} " + "─" * max(0, 56 - len(section)))
            line = f"{_MARK[item.level]} {item.name}"
            if item.detail:
                line += f"　{item.detail}"
            lines.append(line)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1 环境
# --------------------------------------------------------------------------- #
def check_env(rep: Report) -> None:
    rep.add("1 环境", INFO, f"{platform.system()} {platform.release()}（{platform.machine()}）")
    ver = sys.version_info
    ok = ver >= (3, 11)  # tomllib 是 3.11 才进标准库的，settings.py 直接用了
    rep.add(
        "1 环境",
        OK if ok else FAIL,
        f"Python {ver.major}.{ver.minor}.{ver.micro}",
        "settings.py 用标准库 tomllib，需要 ≥3.11" if not ok else str(Path(sys.executable)),
    )
    if ver >= (3, 13):
        rep.add(
            "1 环境",
            INFO,
            "Python 3.13",
            "本项目的参考环境（OpenVINO / sherpa-onnx 的轮子在这个版本最齐）",
        )

    # 路径里带空格 / 非 ASCII：原生库（onnxruntime、espeak-ng）偶发加载失败
    text = str(ROOT)
    if " " in text:
        rep.add("1 环境", WARN, "项目路径里有空格", f"{text}（少数原生库会栽在这儿）")
    if not text.isascii():
        rep.add("1 环境", WARN, "项目路径里有非 ASCII 字符", f"{text}（同上）")

    # 控制台编码：Windows 默认 GBK，中文输出会变码（本项目一律要求 UTF-8）
    enc = (sys.stdout.encoding or "").lower()
    if os.name == "nt" and "utf" not in enc:
        rep.add(
            "1 环境",
            WARN,
            f"控制台编码是 {sys.stdout.encoding}",
            "先跑 chcp 65001，或设 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8",
        )

    rep.add("1 环境", INFO, "工作目录", str(Path.cwd()))


# --------------------------------------------------------------------------- #
# 2 依赖
# --------------------------------------------------------------------------- #
# (import 名, pip 名, 哪条链路需要它, 缺失时多严重)
#   级别 "fail" = 主流程起不来（默认配置就会用到）
#        "warn" = 某个非默认后端 / 某个功能不可用
#        "info" = 纯附加小功能（requirements.txt 里注释掉的那种），别占用「要处理」的注意力
NEEDED = [
    ("numpy", "numpy", "到处", "fail"),
    ("requests", "requests", "LLM / 看图（HTTP）", "fail"),
    ("sounddevice", "sounddevice", "麦克风与扬声器（PortAudio）", "fail"),
    ("sherpa_onnx", "sherpa-onnx", "SenseVoice ASR + ZipVoice 克隆音色", "fail"),
    ("openvino", "openvino", "Whisper 加速（whisper_device=auto 时）", "warn"),
    ("optimum.intel", "optimum-intel[openvino]", "Whisper OpenVINO 导出", "warn"),
    ("transformers", "transformers", "Whisper 分词", "warn"),
    ("piper", "piper-tts", "backend = piper", "warn"),
    ("pykakasi", "pykakasi", "参考音频是日语时转罗马字", "info"),
    ("PIL", "pillow", "截屏 / 看图", "warn"),
    ("cv2", "opencv-python", "摄像头取帧", "info"),
    ("pypdf", "pypdf", "读 .pdf", "info"),
    ("docx", "python-docx", "读 .docx", "info"),
]
_LEVEL_OF = {"fail": FAIL, "warn": WARN, "info": INFO}


def _version_of(dist: str) -> str:
    try:
        return importlib.metadata.version(dist.split("[")[0])
    except Exception:  # noqa: BLE001
        return ""


def check_deps(rep: Report) -> None:
    for module, dist, why, level in NEEDED:
        try:
            importlib.import_module(module)
            ver = _version_of(dist)
            rep.add("2 依赖", OK, f"{dist}{(' ' + ver) if ver else ''}", why)
        except Exception as exc:  # noqa: BLE001
            rep.add(
                "2 依赖",
                _LEVEL_OF[level],
                f"{dist} 缺失",
                f"{why} —— pip install {dist}（原始错误：{type(exc).__name__}）",
            )


# --------------------------------------------------------------------------- #
# 3 配置
# --------------------------------------------------------------------------- #
def _walk_paths(obj, prefix: str = "") -> list[tuple[str, str]]:
    """把配置里所有「看起来是路径」的字符串值摊平成 (键, 值)。"""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.extend(_walk_paths(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            out.extend(_walk_paths(value, f"{prefix}[{i}]"))
    elif isinstance(obj, str):
        value = obj.strip()
        if not value or "://" in value:  # 空串和 URL 不是路径
            return out
        looks_like_path = ("/" in value or "\\" in value) and not value.endswith("。")
        if looks_like_path:
            out.append((prefix, value))
    return out


# 这些路径不存在是正常的：程序自己会建 / 要用户自己放素材
_AUTO_CREATED = ("sessions/", "data/")
_DOWNLOADABLE = ("models/",)


def check_config(rep: Report) -> None:
    cfg_path = ROOT / "config.toml"
    if not cfg_path.exists():
        rep.add("3 配置", FAIL, "config.toml 不存在", f"应该在 {cfg_path}")
        return
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        rep.add("3 配置", FAIL, "config.toml 解析失败", str(exc))
        return
    rep.add("3 配置", OK, "config.toml 能解析", f"{len(raw)} 个段落")

    root = Path(raw.get("app", {}).get("project_root", ".")).expanduser()
    base = (cfg_path.parent / root).resolve() if not root.is_absolute() else root

    missing_models: list[str] = []
    absolute: list[str] = []
    for key, value in _walk_paths(raw):
        p = Path(value).expanduser()
        if p.is_absolute():
            absolute.append(f"{key}={value}")
            continue
        resolved = (base / p).resolve()
        if resolved.exists():
            continue
        norm = value.replace("\\", "/")
        if norm.startswith(_DOWNLOADABLE):
            missing_models.append(f"{key}={value}")
        elif norm.startswith(_AUTO_CREATED):
            rep.add("3 配置", INFO, f"{key} 还没生成", f"{value}（首次运行 / 首次用到时自动创建）")
        else:
            rep.add("3 配置", WARN, f"{key} 指向的文件不在", value)

    if absolute:
        rep.add(
            "3 配置",
            WARN,
            f"有 {len(absolute)} 处写了绝对路径",
            "换机器/换盘符后会失效：" + "、".join(absolute[:3]) + ("…" if len(absolute) > 3 else ""),
        )
    if missing_models:
        rep.add(
            "3 配置",
            FAIL,
            f"有 {len(missing_models)} 个模型路径不存在",
            "先跑 python scripts/download_models.py；明细：" + "、".join(missing_models[:4]),
        )


# --------------------------------------------------------------------------- #
# 4 模型
# --------------------------------------------------------------------------- #
MODEL_WANTED = [
    ("VAD（Silero）", "models/vad/silero_vad.onnx", 0.1),
    ("SenseVoice ASR", "models/asr/sensevoice-small/model.int8.onnx", 100),
    ("Piper 中文音色", "models/tts/piper/zh_CN-huayan-medium.onnx", 10),
    ("ZipVoice 克隆模型", "models/tts/zipvoice/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia/encoder.int8.onnx", 1),
    ("ZipVoice 声码器", "models/tts/zipvoice/vocos_24khz.onnx", 10),
    # ★量体积要看 .bin 而不是 .xml★：OpenVINO 的 .xml 只是计算图定义（一两 MB），
    # 权重全在旁边的 .bin 里；拿 .xml 去比大小会把好模型误判成「没下完」（踩过）。
    ("Whisper（可选）", "models/asr/whisper-large-v3-turbo-int8-ov/openvino_encoder_model.bin", 100),
]


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


def check_models(rep: Report) -> None:
    for label, rel, min_mb in MODEL_WANTED:
        path = ROOT / rel
        if not path.exists():
            rep.add(
                "4 模型",
                WARN if "可选" in label else FAIL,
                f"{label} 缺失",
                f"{rel} —— python scripts/download_models.py",
            )
            continue
        size = path.stat().st_size
        if size < min_mb * 1e6:
            rep.add("4 模型", FAIL, f"{label} 体积异常", f"{rel} 只有 {_human(size)}（像是没下完）")
        else:
            rep.add("4 模型", OK, label, f"{_human(size)}")

    personas = ROOT / "models" / "tts" / "zipvoice" / "personas"
    if personas.is_dir():
        for d in sorted(p for p in personas.iterdir() if p.is_dir()):
            have = [n for n in ("encoder.int8.onnx", "decoder.int8.onnx", "encoder.onnx", "decoder.onnx") if (d / n).is_file()]
            total = sum((d / n).stat().st_size for n in have) if have else 0
            rep.add(
                "4 模型",
                OK if have else FAIL,
                f"角色声线 {d.name}",
                ("、".join(have) + f"（{_human(total)}）") if have else "目录里没有任何 onnx",
            )


# --------------------------------------------------------------------------- #
# 5 外部服务
# --------------------------------------------------------------------------- #
def check_services(rep: Report) -> None:
    try:
        from voice_loop.settings import load_settings

        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        rep.add("5 服务", FAIL, "配置加载失败", f"{type(exc).__name__}: {exc}")
        return

    import requests

    host = settings.llm.host.rstrip("/")
    try:
        r = requests.get(f"{host}/api/tags", timeout=5)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        rep.add("5 服务", OK, f"Ollama 可达（{host}）", f"{len(names)} 个模型")
        for want in {settings.llm.model, settings.vision.model}:
            if want and want not in names and f"{want}:latest" not in names:
                rep.add("5 服务", WARN, f"模型 {want} 没拉过", f"ollama pull {want}")
    except Exception as exc:  # noqa: BLE001
        rep.add("5 服务", FAIL, f"Ollama 连不上（{host}）", f"{type(exc).__name__}：先启动 Ollama")

    try:
        import sounddevice as sd

        devices = sd.query_devices()
        ins = [d for d in devices if d.get("max_input_channels", 0) > 0]
        outs = [d for d in devices if d.get("max_output_channels", 0) > 0]
        rep.add("5 服务", OK if ins else FAIL, "音频输入设备", f"{len(ins)} 个" + (f"，默认 {sd.default.device[0]}" if ins else "（没有麦克风）"))
        rep.add("5 服务", OK if outs else FAIL, "音频输出设备", f"{len(outs)} 个" + (f"，默认 {sd.default.device[1]}" if outs else "（没有扬声器）"))
    except Exception as exc:  # noqa: BLE001
        rep.add("5 服务", FAIL, "音频设备枚举失败", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 6 平台能力
# --------------------------------------------------------------------------- #
def check_platform(rep: Report) -> None:
    from voice_loop import system_ops

    name = system_ops.platform_name()
    rep.add("6 平台能力", INFO, f"当前系统：{name}", f"{system_ops.power_info()}")

    if system_ops.supported():
        rep.add("6 平台能力", OK, "「关屏幕」这条语音指令可用", system_ops.power_info())
    else:
        rep.add("6 平台能力", WARN, "「关屏幕」这条语音指令不可用", "说「关掉屏幕」会回一句做不到，别的功能不受影响")

    # 字幕：Windows 用 Win32 做到「点得穿 + 避开任务栏」，其它系统只能退一步
    if os.name == "nt":
        rep.add("6 平台能力", OK, "字幕条：点得穿 + 自动避开任务栏")
    else:
        rep.add(
            "6 平台能力",
            WARN,
            "字幕条：不会点穿，位置按屏幕高度估算",
            "非 Windows 上拿不到工作区矩形，也不改窗口扩展样式；字幕仍会显示，只是可能压住任务栏",
        )

    if os.name == "nt":
        rep.add("6 平台能力", OK, "后台服务用 pythonw.exe（无控制台窗口）")
    else:
        rep.add("6 平台能力", OK, "后台服务用 start_new_session（关掉终端也不会被带走）")

    from voice_loop import accel

    if os.name == "nt":
        rep.add("6 平台能力", OK, "Ollama 核显开关（--enable-igpu）可用")
    else:
        rep.add("6 平台能力", INFO, "Ollama 核显开关不适用", "只有 Windows 版 Ollama 会丢核显；Linux/macOS 自己会枚举")

    if os.name != "nt" and platform.system() != "Darwin":
        rep.add("6 平台能力", WARN, "读剪贴板图片不可用", "Pillow 只在 Windows/macOS 实现了 grabclipboard；截屏和摄像头可用")
    if platform.system() == "Linux":
        rep.add(
            "6 平台能力",
            INFO,
            "Linux 注意点",
            "Wayland 下截屏可能全黑、xset 关屏无效；[vision] file_roots 里的「桌面/下载/文档」要改成实际目录名",
        )

    hotkey_ok = sys.stdin is not None and sys.stdin.isatty()
    rep.add(
        "6 平台能力",
        OK if hotkey_ok else INFO,
        "说话时按 Esc 打断",
        "需要一个真终端（当前 stdin 不是 tty，前台跑时才有）" if not hotkey_ok else "",
    )


# --------------------------------------------------------------------------- #
# 7 磁盘 / 写权限
# --------------------------------------------------------------------------- #
def check_disk(rep: Report) -> None:
    for rel in ("data", "sessions", "models"):
        d = ROOT / rel
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            rep.add("7 磁盘", OK, f"{rel}/ 可写")
        except Exception as exc:  # noqa: BLE001
            rep.add("7 磁盘", FAIL, f"{rel}/ 不可写", f"{type(exc).__name__}: {exc}")

    try:
        free = shutil.disk_usage(ROOT).free
        need = 8 * 1024**3  # 模型 + 会话 + 一点余量
        rep.add(
            "7 磁盘",
            OK if free > need else WARN,
            f"可用空间 {_human(free)}",
            "" if free > need else "建议至少留 8 GB（模型 + 会话 + 微调时还有 checkpoint）",
        )
    except Exception as exc:  # noqa: BLE001
        rep.add("7 磁盘", INFO, "磁盘空间未知", str(exc))


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="搬家前的自检（只读，几秒跑完）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="警告也算失败（部署镜像自检用）")
    args = ap.parse_args()

    rep = Report()
    for fn in (check_env, check_deps, check_config, check_models, check_services, check_platform, check_disk):
        try:
            fn(rep)
        except Exception as exc:  # noqa: BLE001 - 单项崩了不该让整份报告没了
            rep.add(fn.__name__, FAIL, f"{fn.__doc__ or fn.__name__} 这步自己崩了", f"{type(exc).__name__}: {exc}")

    bad = rep.failed + ([i for i in rep.items if i.level == WARN] if args.strict else [])

    if args.json:
        print(json.dumps(
            {
                "root": str(ROOT),
                "failed": [i.__dict__ for i in bad],
                "items": [i.__dict__ for i in rep.items],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("=" * 66)
        print(" 搬家自检（只读）")
        print("=" * 66)
        print(rep.render())
        print("")
        print("=" * 66)
        if bad:
            print(f" 结论：{len(bad)} 项要处理（失败 {len(rep.failed)}）")
            for item in bad[:12]:
                print(f"   - {item.name}：{item.detail}")
        else:
            print(" 结论：没发现问题，可以跑：python main.py listen")
        print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
