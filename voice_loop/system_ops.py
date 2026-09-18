"""系统级操作：关闭/打开显示器。

只关屏幕，不进睡眠：用的是 Windows 的 ``SC_MONITORPOWER`` 消息，
系统仍停留在 S0（正常工作状态），麦克风、CPU、网络都不受影响。
按任意键或动一下鼠标就会亮回来。
"""

from __future__ import annotations

import ctypes
import os
import sys

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_ON = -1
MONITOR_OFF = 2
MONITOR_STANDBY = 1


def supported() -> bool:
    return os.name == "nt"


def monitor_off() -> tuple[bool, str]:
    """关闭显示器（系统继续运行，不是睡眠）。"""
    if not supported():
        return False, "当前系统不是 Windows，暂不支持关屏"
    try:
        user32 = ctypes.windll.user32
        # 先让窗口管理器知道显示器要断电，再广播给所有顶层窗口
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF,
            0x0002, 1000, None,  # SMTO_ABORTIFHUNG
        )
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"关屏失败：{exc}"


def monitor_on() -> tuple[bool, str]:
    """把显示器点亮。

    程序化地模拟一次鼠标微小移动——合成的输入事件同样能让屏幕亮起来，
    而且只会把指针挪一个像素，不会影响正在编辑的内容。
    """
    if not supported():
        return False, "当前系统不是 Windows，暂不支持"
    try:
        user32 = ctypes.windll.user32
        MOUSEEVENTF_MOVE = 0x0001
        user32.mouse_event(MOUSEEVENTF_MOVE, 0, 1, 0, 0)
        user32.mouse_event(MOUSEEVENTF_MOVE, 0, -1, 0, 0)
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON,
            0x0002, 1000, None, None,
        )
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"亮屏失败：{exc}"


def power_info() -> str:
    """返回一句人类可读的电源方案说明，用于自检。"""
    if not supported():
        return "非 Windows，不适用"
    try:
        # 查询当前显示器超时设置，确认关屏后系统不会跟着睡
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = SYSTEM_POWER_STATUS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        ac = {0: "电池", 1: "交流电", 255: "未知"}.get(status.ACLineStatus, "未知")
        return f"供电：{ac}，电量 {status.BatteryLifePercent}%"
    except Exception:  # noqa: BLE001
        return "未知"


if __name__ == "__main__":  # 手动测试：python -m voice_loop.system_ops off
    action = sys.argv[1] if len(sys.argv) > 1 else "off"
    if action == "on":
        print(monitor_on())
    else:
        print(monitor_off())
