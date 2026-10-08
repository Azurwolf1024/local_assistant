"""本地语音对话链路。

Whisper / SenseVoice  →  Ollama(qwen3.5，自带视觉)  →  Piper(中文女声)

★版本号只有一个地方写★：仓库根目录的 `VERSION` 文件（一行，如 `1.1.0`）。
理由：版本号要同时出现在「命令行 --version」「发布包 zip 名」「latest.json 更新清单」
「README 的升级说明」四处 —— 写四遍迟早对不上。要发版就改 `VERSION` 这一个文件，
然后 `python scripts/make_release.py`（它会用同一个号生成制品与清单）。
"""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    """读根目录的 VERSION；读不到就如实说「不知道」，绝不瞎编一个号。"""
    try:
        text = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0+unknown"
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return "0.0.0+unknown"


__version__ = _read_version()
