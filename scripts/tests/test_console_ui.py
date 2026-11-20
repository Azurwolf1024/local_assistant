"""控制台前端自测：class 有没有样式 / 提示条还活着吗 / 配色跟桌面端对不对得上。

为什么值得一条：控制台**没有构建步骤**，样式表和面板脚本全靠手写字符串拼，
最容易出的就是「JS 里加了个 class，CSS 忘了写」——页面不报错，只是那块没样式，
而且只有点进那个面板才看得见。这里把静态关系钉死。

    python scripts\\tests\\test_console_ui.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop import ui_kit  # noqa: E402

STATIC = ROOT / "voice_loop" / "console" / "static"

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    ok = bool(got) if want is None else got == want
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}")
    if not ok:
        _failures.append(name)


def _js_files() -> list[Path]:
    return [STATIC / "app.js", STATIC / "ui.js", *sorted((STATIC / "panels").glob("*.js"))]


def _used_classes() -> set[str]:
    """从 index.html 和面板脚本里抠出所有写死的 class 名（拼字符串的跳过）。"""
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in _js_files():
        js = f.read_text(encoding="utf-8")
        js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
        text += "\n" + js
    raw: list[str] = []
    raw += re.findall(r'class="([^"]*)"', text)
    raw += re.findall(r'class:\s*"([^"]*)"', text)
    raw += re.findall(r'cls:\s*"([^"]*)"', text)
    raw += re.findall(r'className\s*=\s*"([^"]*)"', text)
    raw += re.findall(r'"class",\s*"([^"]*)"', text)
    out: set[str] = set()
    for value in raw:
        for token in value.split():
            # 动态拼接（"btn " + x、"l-" + level）里的变量部分和裸前缀不算
            if re.fullmatch(r"[A-Za-z][\w-]*", token) and not token.endswith("-"):
                out.add(token)
    return out


def _defined_classes() -> set[str]:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return set(re.findall(r"\.([A-Za-z][\w-]*)", css))


def test_classes_have_styles() -> None:
    print("\n[1] 面板用到的每个 class 在样式表里都有定义")
    used, defined = _used_classes(), _defined_classes()
    missing = sorted(used - defined)
    check("没有裸奔的 class", not missing, True, f"缺 {len(missing)} 个：{missing}" if missing else f"{len(used)} 个都找到了")
    check("样式表不是空的", len(defined) > 40, True, f"{len(defined)} 条规则")


def test_toast_is_floating() -> None:
    print("\n[2] 提示条（toast）走右下角浮层，不是顶部横幅")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    body = app[app.find("function toast"): app.find("function toast") + 900]
    check("toast 自己建浮层容器", '"toasts"' in body)
    check("不再往 #banner 里塞文字", '#banner' not in body and "$(\"#banner\")" not in body)
    check("容器是 fixed", "position: fixed" in css and "#toasts" in css)
    check("三种颜色都有左边条", all(f".toast.{k}" in css for k in ("ok", "err", "warn")))


def test_tokens_match_desktop() -> None:
    print("\n[3] 配色/圆角和桌面端 ui_kit 是同一套（改一边必须改另一边）")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    dark = css[: css.find("@media")]
    light = css[css.find("@media") :]
    # 深色：页面底色 / 卡片 / 描边 / 正文 / 次要文字 / 点缀色，逐个对上
    pairs = [
        ("--bg:", ui_kit.DARK["bg"], dark),
        ("--bg-soft:", ui_kit.DARK["bg_soft"], dark),
        ("--line:", ui_kit.DARK["border"], dark),
        ("--fg:", ui_kit.DARK["fg"], dark),
        ("--fg-dim:", ui_kit.DARK["fg_mute"], dark),
        ("--accent:", ui_kit.DARK["accent"], dark),
        ("--accent-hover:", ui_kit.DARK["accent_hover"], dark),
        ("--ok:", ui_kit.DARK["ok"], dark),
        ("--warn:", ui_kit.DARK["warn"], dark),
        ("--err:", ui_kit.DARK["danger"], dark),
    ]
    for name, want, block in pairs:
        got = re.search(re.escape(name) + r"\s*([^;]+);", block)
        check(f"深色 {name}", got.group(1).strip() if got else None, want)
    # 浅色：只对有明确语义的几项（页面底色在网页里是"纸"，桌面端是"卡"，故意不同）
    for name, key in (("--line:", "border"), ("--fg:", "fg"), ("--fg-dim:", "fg_mute"), ("--err:", "danger")):
        got = re.search(re.escape(name) + r"\s*([^;]+);", light)
        check(f"浅色 {name}", got.group(1).strip() if got else None, ui_kit.LIGHT[key])
    check("圆角跟 RADIUS 一致", "--radius: 12px" in css and "--radius-sm: 8px" in css, True,
          f"ui_kit RADIUS={ui_kit.RADIUS}/{ui_kit.RADIUS_SM}")
    check("字体也是 ui_kit 那套", ui_kit.FONT in css)


def test_layout_guards() -> None:
    print("\n[4] 几条踩过的坑：别让长路径撑破、小窗别碎掉、操作按钮别折成四行")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    check("网格里的卡片允许收缩（min-width: 0）", ".grid > .card { min-width: 0; }" in css)
    check("长路径可以断行", "overflow-wrap: anywhere" in css)
    check("操作列按钮不折行", "td.act .row { flex-wrap: nowrap" in css)
    check("有窄窗口断点", "@media (max-width: 760px)" in css)
    check("周视图列宽可压缩", "repeat(7, minmax(0, 1fr))" in css)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("=" * 70)
    print(" 控制台前端自测（class / 提示条 / 配色 / 布局）")
    print("=" * 70)
    test_classes_have_styles()
    test_toast_is_floating()
    test_tokens_match_desktop()
    test_layout_guards()
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
