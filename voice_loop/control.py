"""控制台 ⇄ 唤醒服务的跨进程命令通道（文件信箱，零依赖）。

## 为什么需要它

控制台 UI 是**独立进程**（用户选的方案），改日程那种事读写同一个 JSON 就够了，
但有些事只有**服务进程**能做：

    「点一下试听」  → 得用服务里已经加载好的 TTS 念出来（控制台自己加载模型要好几秒 + 几 GB）
    「切角色」      → 声线/参考音频挂在服务的引擎上
    「文字对话」    → LLM 在服务里，答完还要出声

以前唯一的跨进程通道是 ``sessions/listen.stop``（一个文件，只能表达「停」）。
这里补一条**有请求、有回执**的通道。

## 为什么用文件而不是 socket / 命名管道

    - 服务侧不需要开线程、不需要事件循环：主循环每轮顺手看一眼就行（跟 stop 文件同一个位置）；
    - 崩了不会留下「半死不活的连接」：一份请求就是一个文件，状态一眼可见（inbox/busy/done）；
    - 符合本项目的习惯（能 `dir` 出来看明白），也不需要新依赖。

## 三目录协议

    sessions/console/inbox/<id>.json    控制台放请求（原子写：先 .tmp 再 replace）
    sessions/console/busy/<id>.json     服务**改名**过来 = 认领（改名是原子的，不会两个进程都抢到）
    sessions/console/done/<id>.json     服务放回执，控制台轮询它

    ★「太旧的请求不执行」★：服务刚起来时绝不补念十分钟前那句（`answer_after`），
    否则你会听到一句莫名其妙的「测试一下」在夜里响起来。超时的请求会被回一张
    「太旧，已忽略」的回执，免得控制台一直干等。
    ★崩在半路的请求★：busy 里的文件超过 `BUSY_STALE_SEC` 说明服务当时死了，
    下一轮把它记成失败回执（不然控制台会等到天荒地老）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DIR_INBOX = "inbox"
DIR_BUSY = "busy"
DIR_DONE = "done"
DIR_DROP = "dropped"

ANSWER_AFTER_SEC = 30.0      # 请求比这个还旧就不执行了（服务刚起时不补念旧话）
BUSY_STALE_SEC = 60.0        # busy 超过这么久 = 服务当时崩了，判失败
KEEP_DONE_SEC = 900.0        # 回执保留多久（控制台最多等十几秒，留 15 分钟够查）
TMP_PREFIX = ".tmp-"         # 临时文件前缀：小圆点开头，不会被当成请求扫到

# 命令表（服务侧实现见 voice_loop/pipeline.py 的 _serve_console）
COMMANDS = ("ping", "say", "ask", "character", "toast")


@dataclass
class Request:
    id: str
    cmd: str
    args: dict = field(default_factory=dict)
    at: float = 0.0

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.at)

    def to_dict(self) -> dict:
        return {"id": self.id, "cmd": self.cmd, "args": self.args, "at": self.at}

    @classmethod
    def from_dict(cls, raw: dict) -> Request:
        return cls(
            id=str(raw.get("id") or ""),
            cmd=str(raw.get("cmd") or ""),
            args=dict(raw.get("args") or {}),
            at=float(raw.get("at") or 0.0),
        )


@dataclass
class Reply:
    id: str
    ok: bool
    text: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    at: float = 0.0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ok": self.ok,
            "text": self.text,
            "data": self.data,
            "error": self.error,
            "at": self.at,
            "seconds": self.seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Reply:
        return cls(
            id=str(raw.get("id") or ""),
            ok=bool(raw.get("ok")),
            text=str(raw.get("text") or ""),
            data=dict(raw.get("data") or {}),
            error=str(raw.get("error") or ""),
            at=float(raw.get("at") or 0.0),
            seconds=float(raw.get("seconds") or 0.0),
        )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(TMP_PREFIX + path.name)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class ControlChannel:
    """控制台这一侧（也可以被测试当成服务侧用，两侧共用同一套路径约定）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.inbox = self.root / DIR_INBOX
        self.busy = self.root / DIR_BUSY
        self.done = self.root / DIR_DONE
        self.dropped = self.root / DIR_DROP

    # ------------------------------------------------------------------ 基础
    def ensure(self) -> None:
        for d in (self.inbox, self.busy, self.done, self.dropped):
            d.mkdir(parents=True, exist_ok=True)

    def _new_id(self) -> str:
        # 时间戳在前 → 按文件名排序就是按时间排序；带上 pid 与随机后缀避免撞名
        return f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def _requests(self) -> list[Path]:
        if not self.inbox.exists():
            return []
        return sorted(p for p in self.inbox.glob("*.json") if not p.name.startswith("."))

    def _replies(self) -> list[Path]:
        if not self.done.exists():
            return []
        return sorted(p for p in self.done.glob("*.json") if not p.name.startswith("."))

    # ------------------------------------------------------------------ 控制台侧
    def submit(self, cmd: str, **args: Any) -> str:
        """投一张请求，返回它的 id。"""
        self.ensure()
        rid = self._new_id()
        req = Request(id=rid, cmd=cmd, args=dict(args), at=time.time())
        _write_json(self.inbox / f"{rid}.json", req.to_dict())
        return rid

    def reply_of(self, rid: str) -> Reply | None:
        raw = _read_json(self.done / f"{rid}.json")
        return Reply.from_dict(raw) if isinstance(raw, dict) else None

    def call(self, cmd: str, *, timeout: float = 15.0, **args: Any) -> Reply:
        """投一张请求并等回执。**超时不抛异常**，返回 ok=False 的回执（界面直接显示）。"""
        t0 = time.time()
        rid = self.submit(cmd, **args)
        deadline = t0 + max(0.1, float(timeout))
        while time.time() < deadline:
            got = self.reply_of(rid)
            if got is not None:
                got.seconds = time.time() - t0
                return got
            time.sleep(0.15)
        return Reply(
            id=rid,
            ok=False,
            error=f"服务 {timeout:g}s 内没有回执（服务没在跑？还是正忙？）",
            at=time.time(),
            seconds=time.time() - t0,
        )

    def status(self) -> dict:
        """队列现状（给界面显示「服务还有几条在做」）。"""
        self.ensure()
        pending = self._requests()
        ages = [max(0.0, time.time() - p.stat().st_mtime) for p in pending]
        return {
            "root": str(self.root),
            "pending": len(pending),
            "busy": len(list(self.busy.glob("*.json"))) if self.busy.exists() else 0,
            "done": len(self._replies()),
            "oldest_pending_seconds": max(ages) if ages else None,
        }

    def prune(self) -> None:
        """清掉过期回执与临时残留（控制台启动时调一次即可）。"""
        self.ensure()
        now = time.time()
        for path in self._replies():
            if now - path.stat().st_mtime > KEEP_DONE_SEC:
                path.unlink(missing_ok=True)
        for path in self.inbox.glob(TMP_PREFIX + "*"):
            if now - path.stat().st_mtime > 300:
                path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 服务侧（认领 + 执行）


def claim(channel: ControlChannel, *, now: float | None = None) -> Request | None:
    """认领一张最早的请求（改名进 busy）。返回 None = 队列空。

    改名的**原子性**就是互斥：两个进程同时来也只有一个能把文件挪走。
    """
    channel.ensure()
    now = now if now is not None else time.time()
    for path in channel._requests():
        rid = path.stem
        target = channel.busy / path.name
        try:
            path.replace(target)                      # 原子认领
        except OSError:
            continue                                  # 别人抢走了，看下一张
        req = Request.from_dict(_read_json(target) or {})
        if not req.id:
            target.unlink(missing_ok=True)
            continue
        if now - req.at > ANSWER_AFTER_SEC:
            _reply(channel, Reply(
                id=rid, ok=False, error="这张请求太旧了，已忽略（服务重启不补念旧话）",
                at=now,
            ))
            target.unlink(missing_ok=True)
            continue
        return req
    return None


def _reply(channel: ControlChannel, reply: Reply, *, drop_from: Path | None = None) -> None:
    channel.done.mkdir(parents=True, exist_ok=True)
    _write_json(channel.done / f"{reply.id}.json", reply.to_dict())
    if drop_from is not None:
        drop_from.unlink(missing_ok=True)


def finish(channel: ControlChannel, reply: Reply) -> None:
    """写回执 + 清 busy（执行方调完就调它）。"""
    _reply(channel, reply, drop_from=channel.busy / f"{reply.id}.json")


def reap(channel: ControlChannel) -> int:
    """把「服务崩在半路」的 busy 记成失败。返回处理条数（服务启动时调一次）。"""
    channel.ensure()
    now = time.time()
    n = 0
    for path in sorted(channel.busy.glob("*.json")):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < BUSY_STALE_SEC:
            continue
        _reply(channel, Reply(
            id=path.stem, ok=False, error="服务在这张请求处理到一半时退出了", at=now
        ), drop_from=path)
        n += 1
    return n


# ---------------------------------------------------------------- 服务侧：命令实现
#
# ★为什么实现放在这里而不是 pipeline 里★：命令表与协议是一件事，放在相邻的地方
# 才不会「加了命令忘了登记」。执行对象（唤醒服务主对象）由调用方传进来，
# 所以本模块**不 import pipeline**——控制台那边也 import 本模块，不能给它带上音频依赖。


def _describe(loop: Any) -> dict:
    """服务现状（给 ping 用）。一律用 getattr 兜底：控制台不该因为某个属性改名就崩。"""
    char = getattr(loop, "character", None)
    label = ""
    tts_label = getattr(loop, "_tts_label", None)
    if callable(tts_label):
        try:
            label = str(tts_label())
        except Exception:  # noqa: BLE001 - 报告用，不能因为取不到就失败
            label = ""
    session = getattr(loop, "session", None)
    tts = getattr(loop, "tts", None)
    return {
        "character_id": getattr(char, "id", "") or "",
        "character_name": getattr(char, "name", "") or "",
        "character_title": getattr(char, "title", "") or "",
        "tts": label,
        "tts_loaded": bool(getattr(tts, "loaded", False)),
        "in_session": bool(getattr(session, "active", False)),
        "pid": os.getpid(),
    }


def execute(loop: Any, req: Request) -> Reply:
    """执行一条命令。★永不抛异常★：服务侧炸了要变成一张失败回执，不能带崩语音服务。"""
    cmd, args = req.cmd, dict(req.args or {})
    t0 = time.time()
    try:
        if cmd == "ping":
            return Reply(id=req.id, ok=True, text="pong", data=_describe(loop), at=t0)

        if cmd == "say":
            text = str(args.get("text") or "").strip()
            if not text:
                return Reply(id=req.id, ok=False, error="say 少了 text", at=t0)
            spoken = float(loop.speak_text(text, wait=True, fresh=True))
            return Reply(
                id=req.id, ok=True, text=text, at=t0,
                data={**_describe(loop), "spoken_seconds": spoken},
            )

        if cmd == "ask":
            # 完整走一轮：技能/工具 + LLM + 念出来。★不吃麦克风★，文字进去、声音出来。
            text = str(args.get("text") or "").strip()
            if not text:
                return Reply(id=req.id, ok=False, error="ask 少了 text", at=t0)
            stats = loop.respond(text)
            answer = str(getattr(stats, "answer", "") or "").strip()
            return Reply(
                id=req.id, ok=True, text=answer, at=t0,
                data={
                    **_describe(loop),
                    "user_text": str(getattr(stats, "user_text", "") or ""),
                    "total_seconds": float(getattr(stats, "total_seconds", 0.0) or 0.0),
                    "first_audio": float(getattr(stats, "first_audio", 0.0) or 0.0),
                    "extra": dict(getattr(stats, "extra", {}) or {}),
                },
            )

        if cmd == "character":
            cid = str(args.get("id") or "").strip()
            switch = getattr(loop, "_switch_character", None)
            if not callable(switch):
                return Reply(id=req.id, ok=False, error="这个服务实例不支持切角色", at=t0)
            char = switch(cid)
            if char is None:
                return Reply(id=req.id, ok=False, error=f"没有角色 {cid!r}", at=t0)
            return Reply(id=req.id, ok=True, text=getattr(char, "name", cid), at=t0, data=_describe(loop))

        if cmd == "toast":
            notifier = getattr(loop, "toast", None)
            if notifier is None:
                return Reply(id=req.id, ok=False, error="画面提醒没开着（[skills] visual 关了？）", at=t0)
            notifier.show(str(args.get("title") or "控制台"), str(args.get("text") or ""))
            return Reply(id=req.id, ok=True, text="已弹出提醒", at=t0)

        return Reply(id=req.id, ok=False, error=f"不认识的命令：{cmd!r}", at=t0)
    except Exception as exc:  # noqa: BLE001 - 一条命令失败不能影响服务
        return Reply(id=req.id, ok=False, error=f"{type(exc).__name__}: {exc}", at=t0)


def serve_once(loop: Any, channel: ControlChannel, *, limit: int = 4) -> int:
    """服务主循环每轮调一次：把信箱里的命令做掉。

    返回**真正执行掉的**命令条数：被拒绝的（太旧、不认识、执行失败）不算——
    回执早就给出去了，所以调用方拿这个数只能当「这轮干了多少活」看，
    ★不能当成「信箱清空了」★（清没清得看 ``channel.status()['pending']``）。

    ``limit`` 是防止「积压 50 条念白」把主循环占死——剩下的下一轮再说。
    """
    n = 0
    while n < max(1, limit):
        req = claim(channel)
        if req is None:
            break
        reply = execute(loop, req)
        reply.seconds = max(0.0, time.time() - reply.at)
        finish(channel, reply)
        n += 1
    return n
