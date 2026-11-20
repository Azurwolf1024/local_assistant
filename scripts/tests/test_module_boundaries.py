"""模块边界自测：asr / tts / memory 三个「可单独拿走」的子系统不许互相缠住。

为什么要有这条
    解耦最容易悄悄回退：某天为了省事在图省事在 `tts/` 里 `from ..asr import Hearing`，
    于是"只装 TTS"装不起来、两边也没法各自优化 —— 而**什么都不会报错**。
    这条自测用 AST 扫 import（不是 grep：docstring 里为了讲清楚会**提到**这些名字），
    谁越界就把文件名和行号打出来。

规则（允许的依赖都是"叶子工具"）
    允许：settings / paths / store / text / audio / phonetic / names / accel / persona
          （memory 另外允许 events、llm、harness —— 它就是干"整理对话"这件事的）
    禁止：
        tts   ✗ asr / memory / skills / pipeline / tools / vision / wake
        asr   ✗ tts / memory / skills / pipeline / tools / vision / wake
        memory✗ tts / asr / skills / pipeline / tools / vision / wake

    python scripts\\tests\\test_module_boundaries.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = "√", "×"
_failures: list[str] = []

#: 每个子系统**不许**出现的依赖
FORBIDDEN = {
    "tts": {"asr", "memory", "skills", "pipeline", "tools", "vision", "wake", "mcp"},
    "asr": {"tts", "memory", "skills", "pipeline", "tools", "vision", "wake", "mcp"},
    "memory": {"tts", "asr", "skills", "pipeline", "tools", "vision", "wake", "mcp"},
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"：{detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def _target(node: ast.ImportFrom) -> str:
    """`from ..asr import X` → "asr"；`from .spec import X`（子包内）→ ""。"""
    if node.level >= 2:
        return (node.module or "").split(".")[0]
    return ""


def scan(module: str) -> list[str]:
    """扫一个子系统里所有 `.py`，返回越界的 (文件:行 依赖) 列表。"""
    room = ROOT / "voice_loop" / module
    banned = FORBIDDEN[module]
    out: list[str] = []
    for path in sorted(room.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:  # 语法错本身由别的测试负责，这里只提示
            out.append(f"{path.name}: 解析失败（{exc}）")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                name = _target(node)
                if name in banned:
                    out.append(f"{path.relative_to(ROOT)}:{node.lineno} → from ..{name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if len(parts) >= 2 and parts[0] == "voice_loop" and parts[1] in banned:
                        out.append(f"{path.relative_to(ROOT)}:{node.lineno} → import {alias.name}")
    return out


def test_boundaries() -> None:
    print("\n[1] 三个子系统的 import 边界")
    for module in ("tts", "asr", "memory"):
        bad = scan(module)
        check(f"{module}/ 没有越界依赖", not bad,
              "；".join(bad[:4]) + (f" 等 {len(bad)} 处" if len(bad) > 4 else ""))


def test_leaf_contracts() -> None:
    print("\n[2] 叶子契约：能被单独 import（不拉起重依赖）")
    import subprocess

    # ★用子进程★：主进程早就 import 过 pipeline 了，量不出来"能不能单独装"
    for stmt in ("import voice_loop.tts", "import voice_loop.asr"):
        got = subprocess.run([sys.executable, "-c",
                              f"import sys; sys.path.insert(0, r'{ROOT}'); {stmt}"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        check(f"{stmt} 能单独跑", got.returncode == 0, (got.stderr or "")[-160:])


def test_tts_hooks() -> None:
    print("\n[3] TTS 的转写能力来自注入（不是 import）")
    import numpy as np

    from voice_loop.tts import hooks

    hooks.set_transcriber(None)
    check("没注入时返回空串（不是抛异常）",
          hooks.transcribe(np.zeros(160, "float32"), 16000) == "", "返回了空串")
    hooks.set_transcriber(lambda a, r: "  参考文本  ", source="self-test")
    check("注入后用的就是注入的那个",
          hooks.transcribe(np.zeros(160, "float32"), 16000) == "参考文本")
    check("来源能被日志读出来", hooks.transcriber_source() == "self-test",
          hooks.transcriber_source())

    def boom(a, r):
        raise RuntimeError("转写炸了")

    hooks.set_transcriber(boom)
    check("注入的实现炸了也不影响出声",
          hooks.transcribe(np.zeros(160, "float32"), 16000) == "", "吞掉异常并返回空串")
    hooks.set_transcriber(None)


def main() -> int:
    print("=" * 72)
    print(" 模块边界自测：asr / tts / memory 能各自独立")
    print("=" * 72)
    test_boundaries()
    test_leaf_contracts()
    test_tts_hooks()
    print()
    if _failures:
        print(f"× {len(_failures)} 项没通过：")
        for name in _failures:
            print(f"  · {name}")
        return 1
    print("√ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
