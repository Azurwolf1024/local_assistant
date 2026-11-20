"""UI 设计件自测：圆角几何 / 颜色混合 / 主题 / 按钮文案截断。

为什么值得一条：`ui_kit` 被**弹窗、字幕、审批卡**三处共用，
它算错一个颜色或圆角，三个界面一起变形，而且**在无图形环境里看不出来**。
所以这里只测纯函数（不建窗口，Tk 那部分靠真机截图看）。

    python scripts\\tests\\test_ui_kit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop import ui_kit  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    ok = bool(got) if want is None else got == want
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}")
    if not ok:
        _failures.append(name)


def test_mix() -> None:
    print("\n[1] 颜色混合（阴影/悬停都靠它，算错就整片发脏）")
    check("全黑混全白取中间值", ui_kit._mix("#000000", "#ffffff", 0.5), "#808080")
    check("alpha=0 就是底色", ui_kit._mix("#ff0000", "#123456", 0.0), "#123456")
    check("alpha=1 就是前景色", ui_kit._mix("#ff0000", "#123456", 1.0), "#ff0000")
    check("越界 alpha 不会炸", ui_kit._mix("#ff0000", "#000000", 2.0), "#ff0000")
    check("坏颜色原样返回（不抛异常）", ui_kit._mix("nope", "#000000", 0.5), "nope")


def test_geometry() -> None:
    print("\n[2] 圆角矩形顶点")
    pts = ui_kit.rounded_points(0, 0, 100, 40, 12)
    check("顶点是成对的坐标", len(pts) % 2, 0)
    check("x 不超出范围", (min(pts[0::2]), max(pts[0::2])), (0, 100))
    check("y 不超出范围", (min(pts[1::2]), max(pts[1::2])), (0, 40))
    tiny = ui_kit.rounded_points(0, 0, 10, 6, 999)
    check("圆角超过一半时自动收敛（不出现负半径）",
          (min(tiny[0::2]), max(tiny[0::2])), (0, 10))


def test_theme() -> None:
    print("\n[3] 主题")
    dark = ui_kit.theme("dark")
    light = ui_kit.theme("light")
    check("深色底是暗的", dark["bg"], ui_kit.DARK["bg"])
    check("浅色底是白的", light["bg"], ui_kit.LIGHT["bg"])
    check("两套 token 键一样（少一个就会 KeyError）",
          sorted(dark.colors), sorted(light.colors))
    check("auto 会给出其中一套", ui_kit.theme("auto").name in ("dark", "light"), True)
    check("认不出的值退回深色", ui_kit.theme("随便写的").name, "dark")


def test_ellipsize() -> None:
    print("\n[4] 文案截断")
    check("短的不动", ui_kit.ellipsize("你好", 10), "你好")
    check("长的加省略号且不超长", len(ui_kit.ellipsize("一二三四五六七八九十", 5)), 5)
    check("换行压成空格", ui_kit.ellipsize("第一行\n第二行", 20), "第一行 第二行")
    check("空串安全", ui_kit.ellipsize("", 5), "")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("=" * 70)
    print(" UI 设计件自测（圆角 / 颜色 / 主题 / 截断）")
    print("=" * 70)
    test_mix()
    test_geometry()
    test_theme()
    test_ellipsize()
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
