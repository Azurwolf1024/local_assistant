"""字幕试跑 + 自检：屏幕底部居中的半透明字幕。

    python scripts/test_subtitle.py              # 演示一轮对话，约 18 秒
    python scripts/test_subtitle.py --check      # ★ 自动检查：可见 / 有内容 / 点得穿 / 会自己消失
    python scripts/test_subtitle.py --keep       # 一直留着，Ctrl+C 退出
    python scripts/test_subtitle.py --long       # 额外试一段超长回答（看 4 行截断）
    python scripts/test_subtitle.py --alpha 0.6  # 换个透明度看看
    python scripts/test_subtitle.py --no-user    # 不显示「你说：…」那一行
    python scripts/test_subtitle.py --shot a.png # 存一张截图

为什么要有这个脚本：字幕是「无边框 + 半透明 + 置顶 + 点得穿」的窗口，坑不少
（DPI 缩放、Tk 的 winfo_id 其实是子窗口、多 OR 一个 WS_EX_LAYERED 会让窗口变成全透明），
光看代码看不出来，必须真的画到屏幕上量一量。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.subtitle import SubtitleOverlay, work_area  # noqa: E402

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


# POINT 是**按值**传的 8 字节结构体。如果声明成 c_void_p 再传 byref，
# 就会变成传指针，WindowFromPoint 拿到垃圾坐标直接返回 NULL。
_user32.WindowFromPoint.argtypes = [_POINT]
_user32.WindowFromPoint.restype = wintypes.HWND


# --------------------------------------------------------------------------- #
# 屏幕检查工具
# --------------------------------------------------------------------------- #
def _grab():
    from PIL import ImageGrab

    # include_layered_windows=True 是必须的：字幕带 -alpha，
    # 在 Windows 上就是分层窗口，不打开这个参数截出来是空的
    return ImageGrab.grab(include_layered_windows=True, all_screens=True)


def _logical_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = _RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _window_at(x: int, y: int) -> tuple[int, str, int]:
    """返回 (hwnd, 类名, 进程号)。"""
    hwnd = _user32.WindowFromPoint(_POINT(x, y))
    if not hwnd:
        return 0, "", 0
    buf = ctypes.create_unicode_buffer(128)
    _user32.GetClassNameW(hwnd, buf, 128)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(hwnd), buf.value, int(pid.value)


def _diff(a, b, rect_phys: tuple[int, int, int, int]) -> float:
    import numpy as np

    ca = np.asarray(a.crop(rect_phys), dtype=int)
    cb = np.asarray(b.crop(rect_phys), dtype=int)
    return float(abs(ca - cb).mean())


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def _check(cfg, area) -> int:
    """自动量一遍：可见 / 画出了东西 / 点得穿 / 到点自动消失。"""
    print("\n[自检]")
    failures = 0

    def ok(name: str, cond: bool, extra: str = "") -> None:
        nonlocal failures
        print(f"  {'√' if cond else '×'} {name}" + (f"   {extra}" if extra else ""))
        if not cond:
            failures += 1

    # 物理像素 / 逻辑像素 的比例（200% 缩放的屏幕上就是 2.0）
    probe = _grab()
    logical_w = _user32.GetSystemMetrics(0)
    logical_h = _user32.GetSystemMetrics(1)
    scale = probe.size[0] / max(1, logical_w)
    print(f"  屏幕：逻辑 {logical_w}x{logical_h}，物理 {probe.size[0]}x{probe.size[1]}，"
          f"缩放 {scale:.2f}x")

    overlay = SubtitleOverlay(
        width=cfg.width, alpha=cfg.alpha, hold_seconds=cfg.hold_seconds,
        font_size=cfg.font_size, max_lines=cfg.max_lines,
        show_user_text=cfg.show_user_text, margin=cfg.margin,
    )
    overlay.start()
    time.sleep(1.0)
    before = _grab()          # 还没说话，字幕应该是隐藏的

    overlay.clear()
    overlay.show_user("现在几点了")
    overlay.update("现在是上午十点四十三分。")
    time.sleep(1.2)
    after = _grab()

    hwnd = overlay.hwnd
    ok("窗口已创建", hwnd != 0, f"hwnd={hwnd:#x}")
    ok("窗口可见", bool(_user32.IsWindowVisible(wintypes.HWND(hwnd))))

    left, top, right, bottom = _logical_rect(hwnd)
    phys = (int(left * scale), int(top * scale), int(right * scale), int(bottom * scale))
    print(f"  窗口（逻辑坐标）: ({left},{top})-({right},{bottom})")
    if area:
        left_gap = left - area[0]
        right_gap = area[2] - right
        ok("水平居中", abs(left_gap - right_gap) <= 4, f"左右留白 {left_gap}/{right_gap}px")
        gap = area[3] - bottom
        ok("贴在任务栏上方", 0 <= gap <= cfg.margin + 2, f"距工作区底边 {gap}px")

    diff = _diff(before, after, phys)
    ok("字幕真的画出来了", diff > 3.0, f"该区域平均像素差 {diff:.2f}")

    cx, cy = (left + right) // 2, (top + bottom) // 2
    _hit, hit_class, hit_pid = _window_at(cx, cy)
    ok("点得穿（鼠标落到底下的窗口）",
       hit_pid != _kernel32.GetCurrentProcessId(), f"中心点命中 {hit_class} pid={hit_pid}")

    print(f"  等 {cfg.hold_seconds:.0f} 秒看会不会自己消失…")
    time.sleep(cfg.hold_seconds + 2.0)
    gone = _grab()
    l2, t2, r2, b2 = _logical_rect(hwnd)
    if (l2, t2) != (left, top):
        phys = (int(l2 * scale), int(t2 * scale), int(r2 * scale), int(b2 * scale))
    ok("到点自动隐藏",
       not _user32.IsWindowVisible(wintypes.HWND(hwnd)) or _diff(before, gone, phys) < 3.0)

    # ★字幕跟声音同步★：还在说话时不能隐藏（用户报的 bug：话音未落、字幕先没了）
    print("\n  —— 同步检查：还在说话时不该隐藏 ——")
    speaking = True
    overlay.set_keepalive(lambda: speaking)
    overlay.show_user("字幕同步测试")
    overlay.update("这句话比较长，要念好一会儿，字幕必须一直留着，不能先说没就没。")
    time.sleep(1.2)
    _user32.IsWindowVisible(wintypes.HWND(hwnd))
    print(f"  假装还在说话，等 {cfg.hold_seconds + 4:.0f} 秒（超过 hold_seconds 也不该隐藏）…")
    time.sleep(cfg.hold_seconds + 4.0)
    still = bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))
    shown = _grab()
    l3, t3, r3, b3 = _logical_rect(hwnd)
    phys3 = (int(l3 * scale), int(t3 * scale), int(r3 * scale), int(b3 * scale))
    ok("还在说话 → 不隐藏", still and _diff(before, shown, phys3) > 3.0)

    speaking = False
    print(f"  说完之后再等 {cfg.hold_seconds + 2:.0f} 秒（这时才该隐藏）…")
    time.sleep(cfg.hold_seconds + 2.0)
    ok("说完之后 → 到点隐藏",
       not _user32.IsWindowVisible(wintypes.HWND(hwnd)))
    overlay.set_keepalive(None)

    overlay.stop()
    print("\n" + "=" * 66)
    print(" 全部通过 √" if not failures else f" {failures} 项未通过")
    print("=" * 66)
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="一直留着不自动关")
    ap.add_argument("--check", action="store_true", help="自动量一遍（推荐）")
    ap.add_argument("--long", action="store_true", help="再试一段很长的回答")
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--no-user", action="store_true")
    ap.add_argument("--shot", metavar="PNG", help="存一张截图，便于确认位置与观感")
    ap.add_argument("--log", action="store_true", help="打开调试日志（会打出实际坐标）")
    args = ap.parse_args()

    cfg = load_settings().subtitle
    if args.log:
        logging.basicConfig(level=logging.DEBUG,
                            format="  [%(levelname)s] %(message)s", stream=sys.stdout)

    area = work_area()
    if area:
        print(f"桌面工作区（已排除任务栏）: 左 {area[0]}, 上 {area[1]}, "
              f"右 {area[2]}, 下 {area[3]}")
    else:
        print("取不到工作区，会用屏幕高度 - 48 兜底")

    if args.check:
        return _check(cfg, area)

    overlay = SubtitleOverlay(
        enabled=True,
        width=args.width or cfg.width,
        alpha=args.alpha if args.alpha is not None else cfg.alpha,
        hold_seconds=600.0 if args.keep else cfg.hold_seconds,
        font_size=cfg.font_size,
        max_lines=cfg.max_lines,
        show_user_text=False if args.no_user else cfg.show_user_text,
        margin=cfg.margin,
    )
    if not overlay.start():
        print("× 字幕线程没能启动")
        return 1

    overlay.show_user("现在几点了")
    time.sleep(0.6)
    overlay.update("现在是上午十点四十三分。")
    print("→ 第 1 轮：技能式短回答")
    time.sleep(3.0)

    overlay.clear()
    overlay.show_user("用一句话介绍一下杭州")
    answer = "杭州是一座把湖光山色和现代都市揉在一起的城市，西湖南线走一圈就能看完大半。"
    print("→ 第 2 轮：流式追加（模拟 LLM 边生成边出字）")
    for ch in answer:
        overlay.append(ch)
        time.sleep(0.045)

    if args.shot:
        out = Path(args.shot)
        out.parent.mkdir(parents=True, exist_ok=True)
        time.sleep(0.6)
        _grab().save(out)
        print(f"→ 已截图：{out}")

    if args.long:
        time.sleep(2.0)
        overlay.clear()
        overlay.show_user("详细讲讲怎么调唤醒词")
        long_text = (
            "先把常驻服务停掉，跑 scripts/test_wake.py 说五次唤醒词，"
            "看它每次被听成什么；相似度在零点七五以上却没命中的，把 fuzzy_ratio 降到零点七试试；"
            "差得比较远的（比如「胎儿戏」这种）降阈值没用，必须把听错的说法填进 wakewords.json 的 "
            "aliases 里，保存后几秒就会自动重新加载，不用重启服务。"
            "如果还是不稳，换音节更长的说法，比如「凯尔希医生」。"
        )
        print("→ 第 3 轮：超长回答（看 max_lines 截断）")
        for ch in long_text:
            overlay.append(ch)
            time.sleep(0.012)

    if args.keep:
        print("字幕会一直留着，按 Ctrl+C 结束")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        print(f"等 {cfg.hold_seconds:.0f} 秒后应该自动消失…")
        time.sleep(cfg.hold_seconds + 1.5)

    overlay.stop()
    print("√ 已关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
