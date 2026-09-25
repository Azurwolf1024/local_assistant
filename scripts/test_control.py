"""控制台 ⇄ 服务 命令通道的自测（纯离线：不需要麦克风、扬声器、模型）。

要钉住的都是「以后最容易悄悄坏掉」的地方：

    §2 认领是原子的 —— 两个「服务」同时抢，同一张请求只能被执行一次（不然会念两遍）
    §3 太旧的请求不执行 —— 服务重启时**绝不补念**十分钟前那句试听
    §4 崩在半路的请求要判失败 —— 否则控制台会一直等一个永远不来的回执
    §5 命令表本身 —— say / ask / character / toast / ping 都对假服务生效，
       而且**任何异常都变成失败回执**，不能带崩服务

    python scripts/test_control.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import control as ct  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


class FakeChar:
    def __init__(self, cid: str, name: str) -> None:
        self.id, self.name, self.title = cid, name, "测试角色"


class FakeSession:
    active = False


class FakeTts:
    loaded = True


class FakeToast:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def show(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class FakeLoop:
    """只实现 control.execute 会用到的那些接口（这就是「不 import pipeline」的好处）。"""

    def __init__(self) -> None:
        self.character = FakeChar("amiya", "阿米娅")
        self.session = FakeSession()
        self.tts = FakeTts()
        self.toast = FakeToast()
        self.spoken: list[str] = []
        self.asked: list[str] = []
        self.switched: list[str] = []
        self.boom = False

    def _tts_label(self) -> str:
        return "ZipVoice(阿米娅)"

    def speak_text(self, text: str, wait: bool = True, fresh: bool = False) -> float:
        self.spoken.append(text)
        return 1.234

    def respond(self, text: str):
        if self.boom:
            raise RuntimeError("假装 LLM 挂了")
        self.asked.append(text)

        class Stats:
            answer = "我在，博士。"
            user_text = text
            total_seconds = 2.5
            first_audio = 0.6
            extra: dict = {}

        return Stats()

    def _switch_character(self, cid: str):
        if cid == "kaltsit":
            self.switched.append(cid)
            self.character = FakeChar("kaltsit", "凯尔希")
            return self.character
        return None


def section_1(tmp: Path) -> ct.ControlChannel:
    print("\n[1] 基本回路：投一张请求 → 服务做掉 → 控制台拿到回执")
    ch = ct.ControlChannel(tmp / "box")
    loop = FakeLoop()
    rid = ch.submit("say", text="测试一下")
    check("请求已落到 inbox", len(list(ch.inbox.glob('*.json'))), 1)
    check("还没有回执（不能凭空造）", ch.reply_of(rid) is None)
    n = ct.serve_once(loop, ch)
    got = ch.reply_of(rid)
    check("服务处理了 1 条", n, 1)
    check("回执 ok", got is not None and got.ok)
    check("话被念了", loop.spoken, ["测试一下"])
    check("busy 已清空（不留残渣）", len(list(ch.busy.glob('*.json'))), 0)
    check("inbox 已清空", len(list(ch.inbox.glob('*.json'))), 0)
    return ch


def section_2(tmp: Path) -> None:
    print("\n[2] ★认领是原子的★：两个服务同时抢，同一张请求只能做一次")
    ch = ct.ControlChannel(tmp / "race")
    ch.ensure()
    ch.submit("say", text="只能念一遍")
    a = ct.claim(ch)
    b = ct.claim(ch)
    check("第一个抢到了", a is not None, detail=str(a.cmd if a else None))
    check("第二个什么也没抢到（不会被念两遍）", b is None)
    check("请求在 busy 里", len(list(ch.busy.glob('*.json'))), 1)


def section_3(tmp: Path) -> None:
    print("\n[3] ★太旧的请求不执行★：服务重启不补念旧话")
    ch = ct.ControlChannel(tmp / "stale")
    ch.ensure()
    rid = ch.submit("say", text="十分钟前那句")
    # 把请求的 at 改成很久以前（模拟「服务没跑，控制台留下的」）
    path = next(ch.inbox.glob("*.json"))
    req = ct.Request.from_dict(ct._read_json(path) or {})
    ct._write_json(path, ct.Request(id=req.id, cmd=req.cmd, args=req.args,
                                    at=time.time() - ct.ANSWER_AFTER_SEC - 60).to_dict())
    loop = FakeLoop()
    n = ct.serve_once(loop, ch)
    got = ch.reply_of(rid)
    check("没有真正执行（返回 0 = 一条命令都没跑）", n, 0)
    check("★关键★：一个字都没念出来", loop.spoken, [])
    check("给了失败回执（控制台不用干等）", got is not None and not got.ok)
    check("回执里说明了原因", got is not None and "太旧" in got.error, detail=(got.error if got else ""))


def section_4(tmp: Path) -> None:
    print("\n[4] ★崩在半路★：busy 里的陈年请求要判失败，不能永远挂着")
    ch = ct.ControlChannel(tmp / "crash")
    ch.ensure()
    rid = ch.submit("say", text="服务做一半崩了")
    claimed = ct.claim(ch)
    check("已被认领", claimed is not None)
    # 刚才那轮服务「死了」：busy 文件假装是很久以前留下的
    busy = next(ch.busy.glob("*.json"))
    import os

    old = time.time() - ct.BUSY_STALE_SEC - 30
    os.utime(busy, (old, old))
    check("还没清之前没有回执", ch.reply_of(rid) is None)
    n = ct.reap(ch)
    got = ch.reply_of(rid)
    check("判失败了 1 条", n, 1)
    check("回执 ok=False", got is not None and not got.ok)
    check("busy 清掉了", len(list(ch.busy.glob('*.json'))), 0)

    # 新的 busy（还在做）不能被误判
    rid2 = ch.submit("say", text="正在做")
    ct.claim(ch)
    check("刚认领的不算崩（不会被误判）", ct.reap(ch), 0)
    check("它还没回执", ch.reply_of(rid2) is None)


def section_5(tmp: Path) -> None:
    print("\n[5] 命令表：ping / say / ask / character / toast 都生效，异常变回执")
    ch = ct.ControlChannel(tmp / "cmd")
    loop = FakeLoop()

    def run(cmd: str, **args):
        rid = ch.submit(cmd, **args)
        ct.serve_once(loop, ch)
        return ch.reply_of(rid)

    ping = run("ping")
    check("ping 有回执", ping is not None and ping.ok)
    check("ping 报告了角色与声线", ping is not None and ping.data.get("character_name"), "阿米娅")
    check("ping 报告了 TTS 标签", ping is not None and ping.data.get("tts"), "ZipVoice(阿米娅)")

    ask = run("ask", text="现在几点了")
    check("ask 走了一轮问答", loop.asked, ["现在几点了"])
    check("ask 把答案带回来了", ask is not None and ask.text, "我在，博士。")
    check("ask 带回了耗时", ask is not None and ask.data.get("total_seconds"), 2.5)

    who = run("character", id="kaltsit")
    check("切角色成功", who is not None and who.ok and who.text, "凯尔希")
    bad = run("character", id="不存在的角色")
    check("不存在的角色 → 失败回执（不是异常）", bad is not None and not bad.ok)

    toast = run("toast", title="控制台", text="测试")
    check("弹窗被调用了", loop.toast.shown, [("控制台", "测试")])

    empty = run("say", text="   ")
    check("空文本 → 失败回执", empty is not None and not empty.ok)
    unknown = run("drink_coffee")
    check("不认识的命令 → 失败回执", unknown is not None and not unknown.ok)

    loop.boom = True
    died = run("ask", text="这句会炸")
    check("★命令炸了也只是一张失败回执★（服务不能带崩）", died is not None and not died.ok)
    check("回执里带上了异常原因", died is not None and "RuntimeError" in died.error,
          detail=(died.error if died else ""))


def section_6(tmp: Path) -> None:
    print("\n[6] 服务没在跑：call() 超时返回失败回执（界面能直接显示）")
    ch = ct.ControlChannel(tmp / "noserver")
    t0 = time.perf_counter()
    got = ch.call("say", text="没人接", timeout=0.4)
    cost = time.perf_counter() - t0
    check("没有抛异常", got is not None)
    check("ok=False", not got.ok)
    check("说明了「没有回执」", "没有回执" in got.error, detail=got.error)
    check("按超时返回（不是秒回也不是卡死）", 0.4 <= cost < 3.0, detail=f"{cost:.2f}s")
    check("status() 能看到这张没被处理的请求", ch.status()["pending"], 1)


def section_7(tmp: Path) -> None:
    print("\n[7] ★命令里的角色参数★：在**同一条命令**里先切再干活")
    ch = ct.ControlChannel(tmp / "who")
    loop = FakeLoop()
    order: list[str] = []
    origin_switch = loop._switch_character

    def spy_switch(cid):
        order.append(f"switch:{cid}")
        return origin_switch(cid)

    loop._switch_character = spy_switch  # type: ignore[assignment]
    origin_ask = loop.respond

    def spy_ask(text):
        order.append(f"ask:{text}")
        return origin_ask(text)

    loop.respond = spy_ask  # type: ignore[assignment]

    rid = ch.submit("ask", text="现在几点", character="kaltsit")
    ct.serve_once(loop, ch)
    got = ch.reply_of(rid)
    check("先切再问（顺序对）", order, ["switch:kaltsit", "ask:现在几点"])
    check("回答里带上了角色", got.data.get("character_name"), "凯尔希")

    odd = ch.submit("say", text="念一句", character="kaltsit")
    ct.serve_once(loop, ch)
    check("say 也支持角色参数", ch.reply_of(odd).ok, True)
    check("这次话也念了", "念一句" in loop.spoken, True, detail=str(loop.spoken))

    bad = ch.submit("ask", text="喂", character="不存在的角色")
    ct.serve_once(loop, ch)
    rep = ch.reply_of(bad)
    check("角色不存在 → 失败回执（不是默默用别人回答）", rep.ok, False)
    check("说明了原因", "没有角色" in rep.error, detail=rep.error)

    # 不带参数就不该碰角色（保持「服务当前是谁就是谁」）
    order.clear()
    plain = ch.submit("ask", text="在吗")
    ct.serve_once(loop, ch)
    check("不带角色参数就不切角色", order, ["ask:在吗"])


def section_8(tmp: Path) -> None:
    print("\n[8] 实时通道能收摊（`bye`）——这是「终端里 Ctrl+C 退不出」的根）")
    import asyncio  # noqa: PLC0415

    from voice_loop.console.bus import EventBus  # noqa: PLC0415

    async def run() -> list[str]:
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        got: list[str] = []

        async def consume():
            async for chunk in bus.subscribe():
                got.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        bus.publish("log", {"line": "hello", "level": "info"})
        await asyncio.sleep(0.1)
        bus.close()                      # ← 关了之后订阅者必须自己退出来
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            got.append("TIMEOUT")
        return got

    chunks = asyncio.run(run())
    check("收到了那条日志", any("hello" in c for c in chunks), True)
    check("★关闭后订阅者自己结束（不会把 uvicorn 拖住）★", "TIMEOUT" not in chunks, True)
    check("收到了 bye", any('"bye"' in c for c in chunks), True)


def main() -> int:
    print("=" * 70)
    print(" 控制台命令通道自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        section_1(tmp)
        section_2(tmp)
        section_3(tmp)
        section_4(tmp)
        section_5(tmp)
        section_6(tmp)
        section_7(tmp)
        section_8(tmp)
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
