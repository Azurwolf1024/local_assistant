"""可视提醒试跑：在右下角弹两个提醒窗，8 秒后自动消失。

    python scripts/test_toast.py            # 默认 8 秒
    python scripts/test_toast.py --keep     # 一直留着，按 Ctrl+C 退出

用来确认弹窗能正常出现、能叠放、点了「知道了」或按 Esc 就能关掉。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.toast import VisualNotifier  # noqa: E402


def main() -> int:
    keep = "--keep" in sys.argv
    timeout = 600.0 if keep else 8.0
    n = VisualNotifier(enabled=True, timeout=timeout)
    if not n.start():
        print("× 弹窗线程没能启动")
        return 1

    n.show("提醒", "时间到了，喝水。")
    time.sleep(1.0)
    n.show("日程提醒", "提醒你：十四分钟后，也就是 09:00，有 AIAA3102 机器学习，地点教学楼 A302。")
    print("√ 已发出两条提醒，请看屏幕右下角")
    print("  点「知道了」或按 Esc 可以立刻关掉")

    if keep:
        print("  按 Ctrl+C 结束")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        time.sleep(timeout + 1.0)

    n.stop()
    print("√ 已关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
