"""联网工具自测：DeepSeek Search / Fetch + MCP 隐藏 + API 角色多开（不联网）。

    python scripts\\test_web_tools.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.mcp.servers import web                          # noqa: E402
from voice_loop.settings import (ApiConfig, McpConfig, McpServerConfig,  # noqa: E402
                                 load_settings)
from voice_loop.tool_presets import expand                     # noqa: E402

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
    check("search 默认地址", web.search_endpoint(cfg),
          "https://api.deepseek.com/v1/tools/search")
    check("fetch 默认地址", web.fetch_endpoint(cfg),
          "https://api.deepseek.com/v1/tools/fetch")
    check("/v1 结尾不重复拼", web.search_endpoint(ApiConfig(base_url="https://x.test/v1")),
          "https://x.test/v1/tools/search")
    check("显式地址优先",
          web.search_endpoint(ApiConfig(base_url="https://x.test",
                                        search_url="https://y.test/s")),
          "https://y.test/s")


def test_search(tmp: Path) -> None:
    print("\n[2] 搜索：请求参数 / 可朗读的结果 / 错误")
    old = web.requests.post
    try:
        results = {
            "results": [
                {"title": "DeepSeek V4 发布", "url": "https://news.example/v4",
                 "snippet": "短摘要", "summary": "长摘要" * 80,
                 "site_name": "示例新闻", "authority": 3},
                {"title": "第二篇", "url": "https://blog.example/2",
                 "snippet": "第二条摘要", "summary": "第二条长摘要",
                 "site_name": "示例博客", "authority": 2},
            ]
        }
        monkey: dict = {}
        web.requests.post = fake_post(monkey, FakeResponse(results))
        s = load_settings()
        s.api.enabled = True
        s.api.web_enabled = True
        s.api.search_count = 3
        s.api.search_timeout = 30
        srv = web.build_server(settings=s)
        text, err = call(srv, "search", {
            "query": "DeepSeek V4", "question": "V4 什么时候发布？",
            "count": 2, "need_content": True,
        })
        check("不是错误", err, False)
        check("请求地址", monkey["url"], "https://api.deepseek.com/v1/tools/search")
        check("query 原样", monkey["payload"]["query"], "DeepSeek V4")
        check("带上用户原话", monkey["payload"]["ori_question"], "V4 什么时候发布？")
        check("count 限制为 2", monkey["payload"]["count"], 2)
        check("need_content 传了", monkey["payload"]["need_content"], True)
        check("念出来的有标题", "DeepSeek V4 发布" in text, True)
        check("念出来的有来源", "示例新闻" in text, True)
        check("★不念 URL★", "https://news.example" in text, False)
        check("长摘要被截断（语音场景）", len(text) < 900, detail=str(len(text)))
        check("带 Bearer", monkey["headers"]["Authorization"].startswith("Bearer "), True)

        web.requests.post = fake_post(monkey, FakeResponse({"results": []}))
        text, err = call(srv, "search", {"query": "绝对搜不到的东西"})
        check("空结果不报错", err, False)
        check("空结果说清楚", "没有搜到" in text, detail=text)

        many = {"results": [
            {"title": f"结果{i}", "url": f"https://x/{i}", "summary": "摘要",
             "snippet": "摘要", "site_name": "站"}
            for i in range(10)
        ]}
        web.requests.post = fake_post(monkey, FakeResponse(many))
        text, err = call(srv, "search", {"query": "很多东西", "count": 10})
        check("请求了 10 条", monkey["payload"]["count"], 10)
        check("★念出来最多 search_count 条（默认 3）★", "4. " not in text, detail=text[:120])

        web.requests.post = fake_post(
            monkey, FakeResponse({"error": {"message": "bad query"}}, status=400))
        text, err = call(srv, "search", {"query": "x"})
        check("HTTP 400 报错", err, True)
        check("错误信息带出来", "bad query" in text, detail=text)

        web.requests.post = fake_post(monkey, FakeResponse({"results": []}))
        text, err = call(srv, "search", {"query": "   "})
        check("空 query 直接拒绝", err, True)

        s.api.web_enabled = False
        text, err = call(srv, "search", {"query": "x"})
        check("web_enabled=false 时拒绝", err, True)
    finally:
        web.requests.post = old


def test_fetch(tmp: Path) -> None:
    print("\n[3] 网页读取 / 截断")
    old = web.requests.post
    try:
        monkey: dict = {}
        web.requests.post = fake_post(monkey, FakeResponse({
            "title": "Example Domain",
            "content": "# Example\n\n" + "正文" * 2000,
        }))
        s = load_settings()
        s.api.enabled = True
        s.api.web_enabled = True
        s.api.fetch_max_chars = 500
        srv = web.build_server(settings=s)
        text, err = call(srv, "fetch", {"url": "https://example.com"})
        check("不是错误", err, False)
        check("请求地址", monkey["url"], "https://api.deepseek.com/v1/tools/fetch")
        check("url 原样", monkey["payload"]["url"], "https://example.com")
        check("带标题", text.startswith("《Example Domain》"), detail=text[:40])
        check("正文截断到配置上限", len(text) < 700, detail=str(len(text)))
        text, err = call(srv, "fetch", {"url": "ftp://x"})
        check("非 http(s) 直接拒绝", err, True)
    finally:
        web.requests.post = old


def test_hidden_and_preset() -> None:
    print("\n[4] MCP 默认隐藏 / 预设展开")
    from voice_loop.mcp.host import MCPHost

    s = load_settings()
    s.api.enabled = True
    s.api.web_enabled = True
    hidden = McpServerConfig(
        name="web", transport="inproc", module="voice_loop.mcp.servers.web",
        hidden=True, namespace=True, timeout=30,
    )
    host = MCPHost(McpConfig(enabled=True, servers=[hidden]),
                   deps={"settings": s, "logger": None})
    try:
        names = {x["function"]["name"] for x in host.specs()}
        check("默认不露给任何人", names, set())
        names = {x["function"]["name"] for x in host.specs(include=["mcp__web__search"])}
        check("点名后才露出来", names, {"mcp__web__search"})
        check("hidden 工具也能路由", host.handleable("mcp__web__search"), True)
        check("找不到的名字会报出来", host.missing(["mcp__web__nope"]), ["mcp__web__nope"])
    finally:
        host.close()
    check("web 预设含两个工具", expand(["web"]),
          {"mcp__web__search", "mcp__web__fetch"})


def test_pipeline(tmp: Path) -> None:
    print("\n[5] 接进 pipeline：只有 API 角色看得到联网工具")
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "fish.json").write_text(json.dumps({
        "id": "fish", "name": "海鱼", "wake_words": ["海鱼"], "engine": "api",
        "api_model": "deepseek-chat", "api_tools": ["web"],
    }, ensure_ascii=False), encoding="utf-8")
    (personas / "local.json").write_text(json.dumps({
        "id": "local", "name": "本地", "wake_words": ["本地"], "api_tools": ["web"],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "characters.json").write_text(json.dumps({
        "default": "local",
        "characters": [
            {"id": "fish", "file": "personas/fish.json"},
            {"id": "local", "file": "personas/local.json"},
        ],
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
    key = tmp / "key.txt"
    key.write_text("sk-test\n", encoding="utf-8")
    s.api.api_key_file = str(key)

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(s, enable_listening=False, lazy_whisper=True)
    try:
        loop._apply_character(loop.persona.get("fish"), "测试")   # noqa: SLF001
        names = {x["function"]["name"] for x in (loop._tool_specs() or [])}  # noqa: SLF001
        check("API 角色看得到联网搜索", "mcp__web__search" in names, detail=str(sorted(names)))
        check("API 角色看得到网页读取", "mcp__web__fetch" in names)
        loop._apply_character(loop.persona.get("local"), "测试")  # noqa: SLF001
        names = {x["function"]["name"] for x in (loop._tool_specs() or [])}  # noqa: SLF001
        check("本地角色看不到", "mcp__web__search" in names, False)
        check("本地角色仍是 10 个", len(names), 10)
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    test_endpoint()
    with tempfile.TemporaryDirectory(prefix="web_tools_") as d:
        d = Path(d)
        test_search(d)
        test_fetch(d)
        test_hidden_and_preset()
        test_pipeline(d)
    print("\n" + "=" * 66)
    if _failures:
        print(f"  {len(_failures)} 项没通过：{'、'.join(_failures)}")
        return 1
    print("  联网工具自测：全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
