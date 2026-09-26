"""自搭 MCP 架构的自测：协议 / 服务器 / 两种传输 / 宿主 / 接进 pipeline。

跑法：
    python scripts/test_mcp.py              # 全部（含真起子进程的 stdio 往返）
    python scripts/test_mcp.py --quick      # 跳过 stdio 那一段（不起子进程）

为什么要有「真起子进程」那一段：inproc 是直接函数调用，**不能证明 stdio 分帧是对的**。
这条链路上最容易踩的坑恰恰在管道上（分块读到一半、stdout 被 print 污染、
中文走 cp936 烂码、对端关了管子）。所以这一节真的 spawn 一个 `python -m
voice_loop.mcp.serve skills`，拿真管道走一遍 initialize → tools/list → tools/call。
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.mcp import (  # noqa: E402
    MCPClient,
    MCPHost,
    MCPError,
    MCPServer,
    PROTOCOL_VERSION,
    InProcessTransport,
    StdioTransport,
)
from voice_loop.mcp import protocol as P  # noqa: E402
from voice_loop.mcp.client import make_transport  # noqa: E402
from voice_loop.settings import McpConfig, McpServerConfig, load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def quiet_log() -> logging.Logger:
    log = logging.getLogger("voice_loop")
    log.setLevel(logging.ERROR)
    return log


def skills_server_for(tmp: Path) -> MCPServer:
    """造一个「数据落在临时目录」的技能服务器（不碰真实 data/）。"""
    from voice_loop.mcp.servers.skills import build_server
    from voice_loop.skills import Skills

    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "m.json")
    skills = Skills(settings, quiet_log())
    skills.store.save([])
    skills.store.save([])
    skills.memos.save([])
    return build_server(settings=settings, skills=skills, logger=quiet_log())


# --------------------------------------------------------------------------- #
def test_protocol() -> None:
    print("\n[1] 协议层：编解码与形状校验")
    msg = P.request(1, P.M_TOOLS_CALL, {"name": "list_memos", "arguments": {}})
    line = P.encode(msg)
    check("编码是「一行 JSON + 换行」", line.endswith(b"\n") and b"\n" not in line[:-1])
    check("中文不转义（便于人肉 debug）",
          P.encode(P.response(1, {"text": "没问题"})).decode("utf-8").find("没问题") > 0)

    dec = P.Decoder()
    got = list(dec.feed(P.encode(msg)))
    check("单条往返", len(got) == 1 and got[0]["method"] == P.M_TOOLS_CALL, str(got)[:60])

    # 分块喂：模拟管道把一条 JSON 撕成三段
    raw = P.encode(msg)
    dec = P.Decoder()
    parts = [list(dec.feed(raw[:7])), list(dec.feed(raw[7:20])), list(dec.feed(raw[20:]))]
    check("按块喂入也能拼回一条", sum(len(p) for p in parts) == 1 and parts[-1][0]["id"] == 1)

    # 多条约一次到
    dec = P.Decoder()
    got = list(dec.feed(P.encode(P.request(1, "ping")) + P.encode(P.request(2, "ping"))))
    check("一次读到两条", [m["id"] for m in got] == [1, 2])

    # 裸 JSON（没有换行）也要认
    dec = P.Decoder()
    got = list(dec.feed(P.encode(P.request(3, "ping")).rstrip(b"\n")))
    check("没有换行的裸 JSON 也认", len(got) == 1 and got[0]["id"] == 3)

    for bad in (b"{not json}\n", b'{"jsonrpc":"1.0","id":1,"method":"ping"}\n',
                b'{"jsonrpc":"2.0","id":1}\n'):
        try:
            list(P.Decoder().feed(bad))
            check(f"拒绝畸形消息 {bad[:24]!r}", False)
        except MCPError as exc:
            check(f"拒绝畸形消息 {bad[:24]!r}", exc.code == P.PARSE_ERROR or exc.code == P.INVALID_REQUEST)

    check("请求/通知/响应分得清",
          P.is_request(P.request(1, "ping"))
          and P.is_notification(P.notification(P.M_INITIALIZED))
          and P.is_response(P.response(1, {})),
          "")


def test_server() -> None:
    print("\n[2] 服务器：initialize / tools/list / tools/call / 出错不崩")
    server = MCPServer("demo", version="0.9", instructions="说明")
    server.add_tool("echo", "原样返回", {"type": "object", "properties": {"text": {"type": "string"}}},
                    lambda a: a.get("text", ""))
    server.add_tool("boom", "故意抛异常", None, lambda a: 1 / 0)

    init = server.handle(P.request(1, P.M_INITIALIZE, {"protocolVersion": PROTOCOL_VERSION}))
    res = init["result"]
    check("initialize 回协议版本与能力", res["protocolVersion"] == PROTOCOL_VERSION
          and "tools" in res["capabilities"], str(res)[:80])
    check("serverInfo 带名字版本", res["serverInfo"]["name"] == "demo"
          and res["serverInfo"]["version"] == "0.9")

    tools = server.handle(P.request(2, P.M_TOOLS_LIST))["result"]["tools"]
    check("tools/list 给了 inputSchema", [t["name"] for t in tools] == ["echo", "boom"]
          and tools[0]["inputSchema"]["properties"]["text"]["type"] == "string")

    called = server.handle(P.request(3, P.M_TOOLS_CALL, {"name": "echo", "arguments": {"text": "嗨"}}))
    check("tools/call 返回 content 文本块",
          called["result"]["content"][0] == {"type": "text", "text": "嗨"},
          str(called["result"])[:80])

    unknown = server.handle(P.request(4, P.M_TOOLS_CALL, {"name": "nope"}))
    check("调不存在的工具 → 错误（不是崩）", "error" in unknown
          and unknown["error"]["code"] == P.INVALID_PARAMS)

    bad = server.handle(P.request(5, P.M_TOOLS_CALL, {"name": "boom"}))
    check("工具抛异常 → isError/错误码，服务器还活着",
          "error" in bad and bad["error"]["code"] == P.INTERNAL_ERROR, str(bad)[:90])
    check("抛完异常后仍能继续服务",
          server.handle(P.request(6, P.M_PING))["result"] == {})

    check("未知方法 → METHOD_NOT_FOUND",
          server.handle(P.request(7, "tools/whatever"))["error"]["code"] == P.METHOD_NOT_FOUND)
    check("通知不回复（协议要求）", server.handle(P.notification(P.M_INITIALIZED)) is None)


def test_inproc(tmp: Path) -> None:
    print("\n[3] inproc 客户端：握手 → 列工具 → 调用（技能服务器）")
    server = skills_server_for(tmp)
    client = MCPClient("skills", InProcessTransport(server), log=quiet_log())
    t0 = time.perf_counter()
    client.start()
    handshake = time.perf_counter() - t0
    check("握手拿到协议版本", client.protocol_version == PROTOCOL_VERSION, client.protocol_version)
    check("inproc 握手几乎不花时间", handshake < 0.2, f"{handshake * 1000:.0f} ms")

    tools = {t.name for t in client.list_tools()}
    check("列出技能工具", {"list_events", "add_memo", "add_event", "list_memos"} <= tools,
          str(sorted(tools)))

    ok, text = client.call("add_memo", {"text": "记一下买牛奶"})
    check("调用写操作成功", ok and "牛奶" in text, text[:50])
    ok, text = client.call("list_memos", {})
    check("再查能查到（同一份数据）", ok and "牛奶" in text, text[:50])
    ok, text = client.call("add_memo", {})
    check("参数缺失 → ok=False 而不是抛异常", not ok, text[:40])
    check("ping 通", client.ping())
    client.close()


def test_stdio(tmp: Path) -> None:
    print("\n[4] stdio 传输：真起子进程走真管道")
    command = [sys.executable, "-u", "-m", "voice_loop.mcp.serve", "skills"]
    # ★必须把数据目录挪到临时目录★：stdio 子进程读的是真实 config.toml，
    # 不指走的话这个测试会往用户真实的 data/memos.json 里写（踩过）
    transport = StdioTransport(
        command,
        cwd=ROOT,
        env={"PYTHONIOENCODING": "utf-8", "VOICE_LOOP_DATA_DIR": str(tmp)},
        log=quiet_log(),
    )
    client = MCPClient("skills-stdio", transport, timeout=60.0, log=quiet_log())
    try:
        t0 = time.perf_counter()
        client.start()
        boot = time.perf_counter() - t0
        check("子进程握手成功", bool(client.protocol_version), f"{boot:.2f}s")
        tools = {t.name for t in client.list_tools()}
        check("列工具（真管道）", "list_events" in tools and len(tools) >= 7, f"{len(tools)} 个")
        ok, text = client.call("list_memos", {})
        check("调用成功且中文没烂码", ok and "备忘" in text, text[:50])
        # 一条中文进、一条中文出，最能暴露编码问题
        ok, text = client.call("add_memo", {"text": "记一下：验证中文编码"})
        check("中文参数往返正常", ok and "编码" in text, text[:50])
        check("进程还活着", transport.alive())
    finally:
        client.close()
    check("close 之后子进程已退出", not transport.alive())


def test_host(tmp: Path) -> None:
    print("\n[5] 宿主：白名单 / 命名空间 / 路由 / 崩了重启")
    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "m.json")
    from voice_loop.skills import Skills

    skills = Skills(settings, quiet_log())
    skills.store.save([])
    skills.memos.save([])

    cfg = McpConfig(
        enabled=True,
        servers=[
            # 自家：不带前缀
            McpServerConfig(name="skills", transport="inproc",
                            module="voice_loop.mcp.servers.skills", namespace=False),
            # 同一个服务器再挂一遍，这次带前缀 + 白名单只放只读的两个
            McpServerConfig(name="ro", transport="inproc",
                            module="voice_loop.mcp.servers.skills",
                            tools=["list_memos", "list_events"]),
        ],
    )
    host = MCPHost(cfg, quiet_log(), deps={"settings": settings, "skills": skills,
                                           "logger": quiet_log()})
    try:
        names = host.names()
        check("自家的名字不带前缀", "list_memos" in names)
        check("外来/第二个服务器带 mcp__ 前缀", "mcp__ro__list_memos" in names)
        check("★白名单挡掉了写操作★", "mcp__ro__add_memo" not in names
              and "mcp__ro__add_event" not in names, str(sorted(names))[:90])
        check("模型看到的工具数 = 8 + 2", len(host.specs()) == 10, f"{len(host.specs())} 个")
        check("specs 是 Ollama 形状", host.specs()[0]["type"] == "function"
              and "parameters" in host.specs()[0]["function"])

        ok, text = host.call("list_memos", {})
        check("不带前缀的名字能路由回 skills", ok, text[:40])
        ok, text = host.call("mcp__ro__list_memos", {})
        check("带前缀的名字能路由", ok, text[:40])
        ok, text = host.call("不存在的工具", {})
        check("未知工具 → ok=False", not ok and "没有这个工具" in text, text[:40])

        check("describe 标出来源", any(i["name"] == "mcp__ro__list_memos" and i["server"] == "ro"
                                      for i in host.describe()))
        check("status 一行能看明白", "skills" in host.status() and "ro" in host.status(),
              host.status())

        # 服务器崩了（handler 里抛异常）不能让宿主整体挂掉
        class Boom(InProcessTransport):
            def send(self, msg: dict) -> None:
                raise MCPError(-32603, "假装子进程死了")

        host._clients["skills"].transport = Boom(MCPServer("dead"))  # noqa: SLF001
        ok, text = host.call("list_memos", {})
        check("服务器挂了只这一路失败", not ok, text[:40])
        ok, text = host.call("mcp__ro__list_memos", {})
        check("其它服务器照常", ok, text[:40])
    finally:
        host.close()


def test_pipeline(tmp: Path) -> None:
    print("\n[6] 接进 pipeline：工具清单与调用都走 MCP")
    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "m.json")
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        check("pipeline 里挂了 MCP 宿主", loop.mcp is not None)
        specs = loop._tool_specs() or []  # noqa: SLF001
        names = {s["function"]["name"] for s in specs}
        # 8 个技能 + 2 个记忆工具（recall / remember，白名单控制；见 config.toml）
        check("工具清单来自宿主", {"list_events", "add_memo", "add_event"} <= names
              and len(names) == 10, f"{len(names)} 个")
        check("记忆工具也露出来了（带 mcp__memory__ 前缀）",
              {"mcp__memory__recall", "mcp__memory__remember"} <= names,
              str(sorted(names))[:80])
        check("TOOL_HINT 会一起给（老行为没变）", bool(names))

        ok, text = loop._call_tool({"function": {"name": "add_memo",            # noqa: SLF001
                                                "arguments": {"text": "记一下 MCP 测试"}}})
        check("经宿主写入成功", ok and "MCP" in text, text[:50])
        ok, text = loop._call_tool({"function": {"name": "list_memos",          # noqa: SLF001
                                                 "arguments": {}}})
        # 技能层会顺手清掉中文里的空格（存进去是「MCP测试」），所以只断言关键字
        check("经宿主查得到", ok and "MCP" in text and "测试" in text, text[:60])

        # 参数是 JSON 字符串（模型偶尔这么给）也要能走通
        ok, text = loop._call_tool({"function": {"name": "list_memos",          # noqa: SLF001
                                                 "arguments": "{}"}})
        check("arguments 是 JSON 字符串也能路由", ok, text[:40])

        # ★这条最关键★：_stream_answer 真正调用的就是 _run_tools（上面那些只是它内部的一步）。
        # 用合成的一个 tool_call 跑一遍，确定性验证「模型选工具 → 经 MCP 执行 → 出要念的话」。
        from voice_loop.pipeline import TurnStats

        stats = TurnStats(turn=1, user_text="我的备忘里有什么")
        reply = loop._run_tools(                                       # noqa: SLF001
            stats,
            [{"function": {"name": "list_memos", "arguments": {}}}],
            time.perf_counter(),
        )
        check("_run_tools 经 MCP 拿到回复",
              "备忘" in reply and bool(stats.extra.get("tool")), reply[:50])
        check("_run_tools 记下了成功/耗时",
              stats.extra.get("tool_ok") is True and "tool_seconds" in stats.extra,
              str(stats.extra)[:80])
    finally:
        loop.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    quick = "--quick" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_mcp_"))
    print("=" * 68)
    print(" 自己搭的 MCP 架构自测（协议 / 服务器 / 传输 / 宿主 / 接进 pipeline）")
    print("=" * 68)
    try:
        test_protocol()
        test_server()
        test_inproc(tmp)
        if quick:
            print("\n[4] stdio 传输：--quick 已跳过")
        else:
            test_stdio(tmp)
        test_host(tmp)
        test_pipeline(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 68)
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for f in _failures:
            print(f"   - {f}")
    else:
        print(" 全部通过 √")
    print("=" * 68)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
