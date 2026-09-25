"""后台跟随：日志尾巴 + 服务状态变化（跑在**一个**后台线程里）。

为什么要跟随日志：用户最常问的是「它为什么不理我」——答案基本都在
``sessions/listen.log`` 里（唤醒没匹配上、模型没加载好、工具报错）。
把这个文件读到网页上，比让用户去开终端、敲 `Get-Content -Wait` 强得多。

★只读★：服务是**独占写**这个文件的（`main.py listen` 会把 stdout 重定向进去）。
控制台一旦往里写，日志就会交错乱掉，所以这里只有 seek/read，没有 open(mode="a")。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from .. import service_ctl
from ..settings import Settings
from .bus import EventBus

# 日志格式：`HH:MM:SS L name | message`（L 是级别首字母，见 main.setup_logging）
_LINE_HEAD = re.compile(r"^\s*(\d{2}:\d{2}:\d{2})\s+([A-Z])\s")
_FAIL_WORDS = ("Traceback", "Error", "错误", "失败", "不能", "无法", "异常", "警告")
BACKLOG_LINES = 400              # 服务起来之前的历史行，最多回看这么多（别把界面拖慢）


def classify(line: str) -> str:
    """给一行日志定个调子（前端用它上色）：info / warn / error。"""
    head = _LINE_HEAD.match(line)
    if head:
        level = head.group(2)
        if level in ("E", "C"):
            return "error"
        if level == "W":
            return "warn"
    if "Traceback" in line or "错误" in line or "失败" in line:
        return "error"
    if any(w in line for w in _FAIL_WORDS):
        return "warn"
    return "info"


def read_tail(path: Path, lines: int = 200) -> list[str]:
    """读文件最后 N 行（给 /api/logs/tail 用；不依赖跟随线程的状态）。"""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        # 一行按 200 字节估：读太多行也没用，先切一段再数
        start = max(0, size - max(4096, lines * 256))
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if start > 0 and rows:
        rows = rows[1:]              # 第一行可能是半截，丢掉
    return rows[-lines:]


class Watcher:
    """一个后台线程干两件事：跟随日志 + 定期问一下服务状态有没有变。"""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        logger: logging.Logger | None = None,
        *,
        status_interval: float = 2.0,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.log = logger or logging.getLogger("voice_loop.console")
        self.log_file = settings.resolve(settings.wake.log_file)
        self._status_interval = max(0.5, float(status_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._ident: tuple[int, int] = (-1, -1)      # (设备号, inode)：用来发现日志被换了
        self._last_status: dict | None = None
        self._last_status_at = 0.0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="console-follow", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ 内部
    @property
    def log_path(self) -> Path:
        return self.log_file

    def _pump_log(self) -> None:
        if not self.log_file.exists():
            return
        try:
            st = self.log_file.stat()
            ident = (int(st.st_dev), int(st.st_ino))
            if ident != self._ident:
                # 第一次看到（或服务重启换了文件）：从末尾往前抽一点历史，
                # 让刚打开的页面不是一片空白；然后从当前末尾继续跟。
                self._ident = ident
                self._offset = st.st_size
                for line in read_tail(self.log_file, 200)[-40:]:
                    self.bus.publish("log", {"line": line, "level": classify(line)})
                return
            if st.st_size < self._offset:            # 被清空/轮转
                self._offset = 0
            if st.st_size == self._offset:
                return
            with self.log_file.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read(256 * 1024)           # 一轮最多读 256KB，别卡住线程
                self._offset = fh.tell()
        except OSError:
            return
        for line in data.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                self.bus.publish("log", {"line": line, "level": classify(line)})

    def _pump_status(self) -> None:
        now = time.time()
        if now - self._last_status_at < self._status_interval:
            return
        self._last_status_at = now
        try:
            status = service_ctl.status(self.settings).as_dict()
        except Exception as exc:  # noqa: BLE001 - 状态查不出来不该让跟随线程死掉
            self.bus.publish("status", {"error": f"{type(exc).__name__}: {exc}"})
            return
        if status != self._last_status:
            self._last_status = status
            self.bus.publish("status", status)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._pump_log()
            self._pump_status()
            self._stop.wait(0.35)
