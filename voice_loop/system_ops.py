"""系统级操作：关闭 / 打开显示器。

三个平台各写各的，但守同一条规矩：**只关屏幕，不进睡眠**——
系统仍停留在正常工作状态，麦克风、CPU、网络都不受影响。
按任意键或动一下鼠标就会亮回来。

| 平台 | 关屏 | 亮屏 | 依赖 |
|---|---|---|---|
| Windows | ``SC_MONITORPOWER`` 广播给所有顶层窗口 | 模拟一次鼠标微动 + 广播 | 无（ctypes 直接调 user32） |
| Linux/X11 | ``xset dpms force off`` | ``xset dpms force on`` | ``xset``（x11-xserver-utils） |
| macOS | ``pmset displaysleepnow`` | ``caffeinate -u -t 1`` | 系统自带 |

Wayland 下 ``xset`` 基本无效（合成器自己管电源），这时会**明确报错并说清楚要按什么**，
而不是假装成功——「说了要关屏却没关」比「说了做不到」更让人迷惑。

调用方（``voice_loop/skills.py``）只看 ``(ok, 说明)`` 这个二元组，
所以任何一种「不支持」都只是换一句说明，不会把语音链路弄挂。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_ON = -1
MONITOR_OFF = 2
MONITOR_STANDBY = 1

# xset 不在时的提示（Wayland 会话里也会走到这里）
_LINUX_HINT = "需要 xset：Debian/Ubuntu 用 sudo apt install x11-xserver-utils；Wayland 请用桌面自己的快捷键"


def platform_name() -> str:
    """``windows`` / ``macos`` / ``linux`` / 其它（自检报告里会打印这个）。"""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform or "unknown"


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    """跑一条外部命令，返回 ``(成功?, 说明)``。命令不存在不算异常。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"没装 {cmd[0]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{cmd[0]} 执行失败：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[0] if detail else f"{cmd[0]} 退出码 {proc.returncode}"
    return True, "ok"


def supported() -> bool:
    """这台机器上能不能程序化关屏（不能也只是「这条语音指令无效」）。"""
    name = platform_name()
    if name == "windows":
        return True
    if name == "macos":
        return shutil.which("pmset") is not None
    if name == "linux":
        return bool(os.environ.get("DISPLAY")) and shutil.which("xset") is not None
    return False


def monitor_off() -> tuple[bool, str]:
    """关闭显示器（系统继续运行，不是睡眠）。"""
    name = platform_name()
    if name == "windows":
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
    if name == "macos":
        return _run(["pmset", "displaysleepnow"])
    if name == "linux":
        if not os.environ.get("DISPLAY"):
            return False, "当前是无图形/Wayland 会话，程序化关屏做不到：" + _LINUX_HINT
        ok, why = _run(["xset", "dpms", "force", "off"])
        return (True, "ok") if ok else (False, f"关屏失败（{why}）：" + _LINUX_HINT)
    return False, f"{name} 上暂不支持关屏"


def monitor_on() -> tuple[bool, str]:
    """把显示器点亮。

    Windows 上程序化地模拟一次鼠标微小移动——合成的输入事件同样能让屏幕亮起来，
    而且只会把指针挪一个像素，不会影响正在编辑的内容。
    """
    name = platform_name()
    if name == "windows":
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
    if name == "macos":
        # -u = 声明一次用户活动唤醒显示器；-t 1 = 只持续 1 秒，不留下常驻进程
        return _run(["caffeinate", "-u", "-t", "1"])
    if name == "linux":
        if not os.environ.get("DISPLAY"):
            return False, "当前是无图形/Wayland 会话，程序化亮屏做不到：" + _LINUX_HINT
        return _run(["xset", "dpms", "force", "on"])
    return False, f"{name} 上暂不支持"


def power_info() -> str:
    """返回一句人类可读的电源方案说明，用于自检。"""
    name = platform_name()
    if name == "windows":
        try:
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
    if name == "macos":
        ok, out = _run(["pmset", "-g", "batt"])
        if not ok:  # 读不到就算了，自检不该因为它失败
            return f"未知（{out}）"
        # 第一行是型号信息，第二行才是「现在用什么电 / 还剩多少」
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return lines[1] if len(lines) > 1 else (lines[0] if lines else "未知")
    if name == "linux":
        parts: list[str] = []
        base = "/sys/class/power_supply"
        try:
            for entry in sorted(os.listdir(base)):
                path = os.path.join(base, entry)
                if entry.startswith("AC") and os.path.exists(f"{path}/online"):
                    with open(f"{path}/online", encoding="ascii") as fh:
                        parts.append("交流电" if fh.read().strip() == "1" else "电池")
                elif entry.startswith("BAT") and os.path.exists(f"{path}/capacity"):
                    with open(f"{path}/capacity", encoding="ascii") as fh:
                        parts.append(f"电量 {fh.read().strip()}%")
        except Exception:  # noqa: BLE001
            return "未知"
        return "，".join(parts) or "台式机（没有电池）"
    return "未知"


if __name__ == "__main__":  # 手动测试：python -m voice_loop.system_ops off
    action = sys.argv[1] if len(sys.argv) > 1 else "off"
    if action == "info":
        print(f"平台：{platform_name()}　可关屏：{supported()}　{power_info()}")
    elif action == "on":
        print(monitor_on())
    else:
        print(monitor_off())
