"""Harness agent 工具自测：搜索/读取 + 文件/命令边界 + 多轮工具循环（不联网）。

    python scripts\\test_agent_tools.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.mcp.servers import agent as ag                  # noqa: E402
from voice_loop.settings import (ApiConfig, McpConfig, McpServerConfig,  # noqa: E402
                                 load_settings)
from voice_loop.tool_presets import expand                      # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


class FakeResponse:
    def __init__(self, body: dict | str, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def fake_post(monkey: dict, resp: FakeResponse):
    def _post(url, headers=None, json=None, timeout=None):
        monkey.update(url=url, headers=headers, payload=json, timeout=timeout)
        return resp

    return _post


def call(server, name: str, args: dict) -> tuple[str, bool]:
    res = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    result = res.get("result") or {}
    content = result.get("content") or []
    text = str(content[0].get("text") or "") if content else ""
    return text, bool(result.get("isError"))


# --------------------------------------------------------------------------- #
def test_endpoint() -> None:
    print("\n[1] 地址推导")
    cfg = ApiConfig(base_url="https://api.deepseek.com")
    check("search 默认地址", ag.search_endpoint(cfg),
          "https://api.deepseek.com/v1/tools/search")
    check("fetch 默认地址", ag.fetch_endpoint(cfg),
          "https://api.deepseek.com/v1/tools/fetch")
    check("/v1 结尾不重复拼", ag.search_endpoint(ApiConfig(base_url="https://x.test/v1")),
          "https://x.test/v1/tools/search")
    check("显式地址优先",
          ag.search_endpoint(ApiConfig(base_url="https://x.test",
                                       search_url="https://y.test/s")),
          "https://y.test/s")


def test_search(tmp: Path) -> None:
    print("\n[2] 搜索：结构化结果（含 URL，给模型看）")
    old = ag.requests.post
    try:
        results = {
            "results": [
                {"title": "DeepSeek V4 发布", "url": "https://news.example/v4",
                 "snippet": "短摘要", "summary": "长摘要" * 80,
                 "site_name": "示例新闻", "authority": 3,
                 "date": "2026-11-01T00:00:00+00:00", "content": "正文内容"},
                {"title": "第二篇", "url": "https://blog.example/2",
                 "snippet": "第二条摘要", "summary": "第二条长摘要",
                 "site_name": "示例博客", "authority": 2,
                 "date": None, "content": ""},
            ]
        }
        monkey: dict = {}
        ag.requests.post = fake_post(monkey, FakeResponse(results))
        s = load_settings()
        s.api.enabled = True
        s.api.web_enabled = True
        s.api.search_count = 3
        s.agent.max_tool_chars = 6000
        srv = ag.build_server(settings=s)
        text, err = call(srv, "web_search", {
            "query": "DeepSeek V4", "question": "V4 什么时候发布？",
            "count": 2, "need_content": True,
        })
        check("不是错误", err, False)
        check("请求地址", monkey["url"], "https://api.deepseek.com/v1/tools/search")
        check("query 原样", monkey["payload"]["query"], "DeepSeek V4")
        check("带上用户原话", monkey["payload"]["ori_question"], "V4 什么时候发布？")
        check("count 限制为 2", monkey["payload"]["count"], 2)
        check("need_content 传了", monkey["payload"]["need_content"], True)
        check("★给模型看的结果带 URL★", "https://news.example/v4" in text, True)
        check("有标题", "DeepSeek V4 发布" in text, True)
        check("有来源/日期", "site: 示例新闻" in text and "date: 2026-11-01" in text, True)
        check("请求了正文就带 content", "content:" in text, True)
        check("带 Bearer", monkey["headers"]["Authorization"].startswith("Bearer "), True)

        ag.requests.post = fake_post(monkey, FakeResponse({"results": []}))
        text, err = call(srv, "web_search", {"query": "绝对搜不到的东西"})
        check("空结果不报错", err, False)
        check("空结果说清楚", "没有搜到" in text, detail=text)

        ag.requests.post = fake_post(
            monkey, FakeResponse({"error": {"message": "bad query"}}, status=400))
        text, err = call(srv, "web_search", {"query": "x"})
        check("HTTP 400 报错", err, True)
        check("错误信息带出来", "bad query" in text, detail=text)
    finally:
        ag.requests.post = old


def test_fetch(tmp: Path) -> None:
    print("\n[3] 网页读取")
    old = ag.requests.post
    try:
        monkey: dict = {}
        ag.requests.post = fake_post(monkey, FakeResponse({
            "title": "Example Domain",
            "content": "# Example\n\n" + "正文" * 2000,
        }))
        s = load_settings()
        s.api.enabled = True
        s.api.web_enabled = True
        s.api.fetch_max_chars = 500
        s.agent.max_tool_chars = 6000
        srv = ag.build_server(settings=s)
        text, err = call(srv, "web_fetch", {"url": "https://example.com"})
        check("不是错误", err, False)
        check("请求地址", monkey["url"], "https://api.deepseek.com/v1/tools/fetch")
        check("有标题和 URL", "title: Example Domain" in text
              and "url: https://example.com" in text, True)
        check("正文截断", len(text) < 700, detail=str(len(text)))
        text, err = call(srv, "web_fetch", {"url": "ftp://x"})
        check("非 http(s) 直接拒绝", err, True)
    finally:
        ag.requests.post = old


def test_files(tmp: Path) -> None:
    print("\n[4] 工作区文件：读/列目录 / 越界拦截 / 写与命令默认关")
    sandbox = tmp / "ws"
    (sandbox / "sub").mkdir(parents=True)
    (sandbox / "a.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    (sandbox / "sub" / "b.txt").write_text("b", encoding="utf-8")
    s = load_settings()
    s.agent.workspace = str(sandbox)
    s.agent.max_file_chars = 500
    s.agent.max_list_entries = 20
    s.agent.allow_write = False
    s.agent.allow_shell = False
    srv = ag.build_server(settings=s)
    names = {t.name for t in srv.tools()}
    check("默认有读/列/找/搜/联网",
          {"read_file", "list_dir", "glob", "grep", "web_search", "web_fetch"} <= names,
          detail=str(sorted(names)))
    check("写/改文件默认不出现", {"write_file", "edit_file"} <= names, False)
    check("执行命令默认不出现", "run_command" in names, False)

    text, err = call(srv, "read_file", {"path": "a.txt", "start_line": 2, "max_lines": 1})
    check("读指定行", err, False)
    check("带行号与范围", "2  第二行" in text and "lines: 2-2 of 3" in text, detail=text)
    text, err = call(srv, "list_dir", {"path": "."})
    check("列目录有文件和子目录", "a.txt" in text and "sub/" in text, detail=text)
    text, err = call(srv, "glob", {"pattern": "**/*.txt", "path": "."})
    check("glob 找到两个 txt", err, False)
    check("glob 结果", {"a.txt", "sub/b.txt"} <= set(text.splitlines()), detail=text)
    text, err = call(srv, "grep", {"pattern": "第二行", "path": "."})
    check("grep 命中带行号", "a.txt:2:" in text and "第二行" in text, detail=text)
    text, err = call(srv, "read_file", {"path": "../outside.txt"})
    check("★越界读取被拒★", err, True)
    check("越界提示", "工作区之外" in text, detail=text)
    (sandbox / "sessions").mkdir()
    (sandbox / "sessions" / "log.txt").write_text("private", encoding="utf-8")
    (sandbox / "config.local.toml").write_text("[api]\n", encoding="utf-8")
    text, err = call(srv, "read_file", {"path": "sessions/log.txt"})
    check("★隐私目录被拒★", err, True)
    text, err = call(srv, "read_file", {"path": "config.local.toml"})
    check("★本地配置文件被拒★", err, True)
    text, _ = call(srv, "list_dir", {"path": "."})
    check("列目录也不显示隐私目录", "sessions/" in text, False)

    s.agent.allow_write = True
    s.agent.allow_shell = True
    srv2 = ag.build_server(settings=s)
    names = {t.name for t in srv2.tools()}
    check("开启后有写文件", "write_file" in names, detail=str(sorted(names)))
    check("开启后有改文件", "edit_file" in names)
    check("开启后有执行命令", "run_command" in names)
    text, err = call(srv2, "write_file", {"path": "new.txt", "content": "你好"})
    check("写文件成功", err, False)
    check("文件真的写了", (sandbox / "new.txt").read_text(encoding="utf-8"), "你好")
    text, err = call(srv2, "edit_file", {"path": "a.txt", "old": "第二行", "new": "第二行改"})
    check("改文件成功", err, False)
    check("改文件生效", "第二行改" in (sandbox / "a.txt").read_text(encoding="utf-8"), True)
    text, err = call(srv2, "write_file", {"path": "../escape.txt", "content": "x"})
    check("★越界写入被拒★", err, True)


def test_hidden_and_preset() -> None:
    print("\n[5] MCP 默认隐藏 / 预设名")
    from voice_loop.mcp.host import MCPHost

    s = load_settings()
    s.api.enabled = True
    s.api.web_enabled = True
    s.agent.allow_write = False
    s.agent.allow_shell = False
    hidden = McpServerConfig(
        name="agent", transport="inproc", module="voice_loop.mcp.servers.agent",
        hidden=True, namespace=False, timeout=30,
    )
    host = MCPHost(McpConfig(enabled=True, servers=[hidden]),
                   deps={"settings": s, "logger": None})
    try:
        names = {x["function"]["name"] for x in host.specs()}
        check("默认不露给任何人", names, set())
        names = {x["function"]["name"] for x in host.specs(include=["web_search"])}
        check("点名后才露出来", names, {"web_search"})
        check("hidden 工具也能路由", host.handleable("read_file"), True)
        check("找不到的名字会报出来", host.missing(["nope"]), ["nope"])
    finally:
        host.close()
    check("web 预设", expand(["web"]), {"web_search", "web_fetch"})
    check("files 预设", expand(["files"]),
          {"read_file", "list_dir", "glob", "grep"})
    check("write 预设", expand(["write"]), {"write_file", "edit_file"})
    check("harness 预设", expand(["harness"]),
          {"web_search", "web_fetch", "read_file", "list_dir", "glob", "grep",
           "write_file", "edit_file", "run_command"})


class ScriptedLLM:
    """按脚本吐事件；记录每次 messages，便于验证工具结果真的喂回去了。"""

    engine = "api"

    def __init__(self, script: list[list[dict]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[bool] = []
        self.history: list[tuple[str, str]] = []
        self.system_prompt = ""

    def build_messages(self, user_text, images=None, extra_messages=None,
                       prefix_messages=None, system_override=None):
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        return msgs + [{"role": "user", "content": user_text}]

    def chat_events(self, messages=None, tools=None, model=None, num_ctx=None,
                    **_kw):
        self.calls.append([dict(m) for m in (messages or [])])
        self.tools_seen.append(bool(tools))
        events = self.script.pop(0) if self.script else [{"delta": "（脚本用完了）"}]
        yield from events

    def commit(self, user_text, answer):
        self.history.append((user_text, answer))

    def reset(self):
        self.history.clear()

    def _system_prompt(self):
        return self.system_prompt

    def release(self, model=None):
        return True

    def ensure_model(self):
        return None

    def warmup(self, model=None):
        return 0.0

    def is_loaded(self, model=None):
        return True


def test_pipeline_agent(tmp: Path) -> None:
    print("\n[6] 接进 pipeline：工具结果喂回模型，再回答")
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "fish.json").write_text(json.dumps({
        "id": "fish", "name": "海鱼", "wake_words": ["海鱼"], "engine": "api",
        "api_model": "deepseek-chat", "api_tools": ["web", "files"],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "characters.json").write_text(json.dumps({
        "default": "fish",
        "characters": [{"id": "fish", "file": "personas/fish.json"}],
    }, ensure_ascii=False), encoding="utf-8")

    s = load_settings()
    s.persona.file = str(tmp / "characters.json")
    s.subtitle.enabled = False
    s.skills.visual_alert = False
    s.skills.data_dir = str(tmp)
    s.skills.event_file = str(tmp / "events.json")
    s.skills.memo_file = str(tmp / "memos.json")
    s.app.sessions_dir = str(tmp / "sessions")
    s.api.enabled = True
    s.api.tts_enabled = False
    s.api.web_enabled = True
    s.api.api_key = ""
    s.api.api_key_env = "NO_SUCH_KEY_FOR_TEST"
    s.agent.enabled = True
    s.agent.max_rounds = 4
    key = tmp / "key.txt"
    key.write_text("sk-test\n", encoding="utf-8")
    s.api.api_key_file = str(key)

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(s, enable_listening=False, lazy_whisper=True)
    loop.tts_enabled = False
    try:
        loop._apply_character(loop.persona.get("fish"), "测试")   # noqa: SLF001
        names = {x["function"]["name"] for x in (loop._tool_specs() or [])}  # noqa: SLF001
        check("API 角色看得到 web_search", "web_search" in names, detail=str(sorted(names)))
        check("API 角色看得到 read_file", "read_file" in names)

        fake = ScriptedLLM([
            [{"tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "web_search", "arguments": "{\"query\":\"DeepSeek\"}"},
            }]}],
            [{"delta": "根据搜索结果，"}, {"delta": "DeepSeek 最近发布了新模型。"}],
        ])
        loop.llm = fake
        used: list[tuple[str, dict]] = []

        def fake_tool(name, args):
            used.append((name, args))
            return True, "query: DeepSeek\n1. 某新闻\n   url: https://example.com/news"

        loop._call_tool_for_model = fake_tool   # noqa: SLF001
        st = loop.respond("最近 DeepSeek 有什么新闻？")
        check("最终回答来自模型第二轮", st.answer, "根据搜索结果，DeepSeek 最近发布了新模型。")
        check("走了 agent 循环", bool(st.extra.get("agent")), True)
        check("工具轮数 = 1", st.extra.get("tool_rounds"), 1)
        check("工具被调用", used, [("web_search", {"query": "DeepSeek"})])
        check("第一次调用带了工具", fake.tools_seen[0], True)
        check("第二轮仍带工具（模型自己决定不调）", fake.tools_seen[1], True)
        second = fake.calls[1]
        roles = [m.get("role") for m in second]
        check("喂回了 assistant tool_calls", "assistant" in roles and any(
            m.get("tool_calls") for m in second if m.get("role") == "assistant"), True)
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        check("喂回了 tool 结果", len(tool_msgs), 1)
        check("tool_call_id 对上了", tool_msgs[0].get("tool_call_id"), "c1")
        check("结果内容真的在里面", "https://example.com/news" in tool_msgs[0]["content"], True)
        check("历史只存最终问答", fake.history, [("最近 DeepSeek 有什么新闻？",
                                                   "根据搜索结果，DeepSeek 最近发布了新模型。")])

        # ask_user：模型先问用户，拿到回答后再继续
        fake3 = ScriptedLLM([
            [{"tool_calls": [{
                "id": "q1", "type": "function",
                "function": {"name": "ask_user",
                             "arguments": "{\"question\":\"用哪个音色？\","
                                          "\"options\":[\"stella\",\"tide\"]}"},
            }]}],
            [{"delta": "好，用 stella。"}],
        ])
        loop.llm = fake3
        asked: list[tuple[str, list[str]]] = []
        real_ask = loop._ask_user  # noqa: SLF001

        def fake_ask(question, options):
            asked.append((question, list(options)))
            return "stella"

        loop._ask_user = fake_ask   # noqa: SLF001
        st = loop.respond("帮我选个音色")
        check("ask_user 被调用", asked, [("用哪个音色？", ["stella", "tide"])])
        check("ask_user 后继续回答", st.answer, "好，用 stella。")
        tool_msg = [m for m in fake3.calls[1] if m.get("role") == "tool"][0]
        check("用户回答喂回模型", "用户回答：stella" in tool_msg["content"],
              detail=tool_msg["content"])

        # update_plan
        fake4 = ScriptedLLM([
            [{"tool_calls": [{
                "id": "p1", "type": "function",
                "function": {"name": "update_plan",
                             "arguments": "{\"steps\":[\"查资料\",\"改文件\"]}"},
            }]}],
            [{"delta": "按计划做完了。"}],
        ])
        loop.llm = fake4
        st = loop.respond("分步做一下")
        check("计划记进 stats", st.extra.get("plan"), ["查资料", "改文件"])
        check("计划后继续回答", st.answer, "按计划做完了。")

        # 纯文本模式下 ask_user 真的会读 input()
        loop._ask_user = real_ask   # noqa: SLF001
        import builtins
        old_input = builtins.input
        builtins.input = lambda *_a, **_k: "蓝色"   # type: ignore[assignment]
        try:
            answer = loop._ask_user("用哪个颜色？", ["蓝", "红"])  # noqa: SLF001
        finally:
            builtins.input = old_input                   # type: ignore[assignment]
        check("文本模式 ask_user 读到回答", answer, "蓝色")

        # 到轮数上限 → 最后一次不给工具，逼它收口
        fake2 = ScriptedLLM([
            [{"tool_calls": [{"id": "a", "type": "function",
                              "function": {"name": "web_search",
                                           "arguments": "{\"query\":\"x\"}"}}]}],
            [{"tool_calls": [{"id": "b", "type": "function",
                              "function": {"name": "web_search",
                                           "arguments": "{\"query\":\"y\"}"}}]}],
            [{"delta": "资料够了，就这样。"}],
        ])
        loop.llm = fake2
        loop.settings.agent.max_rounds = 2
        st = loop.respond("再查一次")
        check("到顶后强制收口", st.answer, "资料够了，就这样。")
        check("最后一轮 tools=None", fake2.tools_seen[-1], False)
        check("轮数记录", st.extra.get("tool_rounds"), 2)
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent_tools_") as d:
        d = Path(d)
        test_endpoint()
        test_search(d)
        test_fetch(d)
        test_files(d)
        test_hidden_and_preset()
        test_pipeline_agent(d)
    print("\n" + "=" * 66)
    if _failures:
        print(f"  {len(_failures)} 项没通过：{'、'.join(_failures)}")
        return 1
    print("  Harness agent 工具自测：全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
