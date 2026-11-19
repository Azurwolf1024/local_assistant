"""可选 API 后端自测：配置 / 人格字段 / OpenAI 兼容流 / 本地-API 切换。

全部离线跑：``requests.post`` 用假的替代，不会真的打到 DeepSeek。

    python scripts\\test_api_llm.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import llm as llm_mod                      # noqa: E402
from voice_loop.llm import ApiError, ChatClient, OpenAICompatClient  # noqa: E402
from voice_loop.persona import Character, CharacterRegistry          # noqa: E402
from voice_loop.settings import ApiConfig, LlmConfig, load_settings   # noqa: E402

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
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self.lines = lines
        self.status_code = status
        self.text = "\n".join(lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def iter_lines(self, decode_unicode: bool = False):
        for line in self.lines:
            yield line.encode("utf-8")


def fake_stream(monkey: dict, lines: list[str], status: int = 200):
    """替换 requests.post，顺便记下请求体，便于断言。"""

    def _post(url, json=None, headers=None, stream=False, timeout=None):
        monkey.update(url=url, payload=json, headers=headers,
                      stream=stream, timeout=timeout)
        return FakeResponse(lines, status=status)

    return _post


# --------------------------------------------------------------------------- #
def test_config() -> None:
    print("\n[1] 配置：[api] 段能读、默认还是本地")
    settings = load_settings()
    api = settings.api
    check("有 api 配置对象", api is not None)
    check("默认引擎是 local（全离线不变）", api.engine, "local")
    check("默认地址是 DeepSeek", api.base_url, "https://api.deepseek.com")
    check("模型默认 deepseek-chat", api.model, "deepseek-chat")
    check("密钥默认走环境变量", api.api_key_env, "DEEPSEEK_API_KEY")
    check("支持把密钥放在仓库外的文件里", hasattr(api, "api_key_file"), True)
    check("额外工具默认空", api.extra_tools, [])


def test_character_fields() -> None:
    print("\n[2] 人格字段：白名单式解析必须显式搬运")
    raw = {
        "id": "fish", "name": "鱼", "engine": "API",
        "api_model": "deepseek-reasoner", "api_base": "https://x.test/v1",
        "api_key": "env:MY_KEY", "api_tools": ["memory", " web_search "],
        "tts": {"provider": "api", "voice_id": "stella"},
    }
    char = Character.from_dict(raw)
    check("engine 小写归一", char.engine, "api")
    check("api_model 进来了", char.api_model, "deepseek-reasoner")
    check("api_base 进来了", char.api_base, "https://x.test/v1")
    check("api_key 进来了", char.api_key, "env:MY_KEY")
    check("api_tools 去空白", char.api_tools, ["memory", "web_search"])
    check("voice_id 读进来了（不再强制小写：edge 的音色带大小写）", char.tts_voice, "stella")
    again = Character.from_dict(char.to_dict())
    check("to_dict → from_dict 不丢字段",
          (again.engine, again.api_model, again.api_base, again.api_key, again.api_tools,
           again.tts_voice),
          ("api", "deepseek-reasoner", "https://x.test/v1", "env:MY_KEY",
           ["memory", "web_search"], "stella"))
    check("缺省跟随全局（空串）", Character.from_dict({"name": "x"}).engine, "")


def test_openai_client() -> None:
    print("\n[3] OpenAI 兼容流：文字 / 工具调用分片 / 错误")
    old_post = llm_mod.requests.post
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        monkey: dict = {}
        lines = [
            'data: {"choices":[{"delta":{"content":"你"}}]}',
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"function":{"name":"now","arguments":"{\\"te"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
            '{"arguments":"xt\\":\\"现在几点\\"}"}}]},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        llm_mod.requests.post = fake_stream(monkey, lines)
        client = OpenAICompatClient(ApiConfig(model="deepseek-chat"))
        client.system_prompt = "你是测试"
        events = list(client.chat_events(
            "你好", tools=[{"type": "function", "function": {"name": "now"}}],
        ))
        check("文字增量照原样吐出", [e["delta"] for e in events if "delta" in e],
              ["你", "好"])
        calls = [e["tool_calls"] for e in events if "tool_calls" in e]
        check("工具调用攒齐后一次给出", len(calls), 1)
        fn = calls[0][0]["function"]
        check("工具名对", fn["name"], "now")
        check("参数分片拼回合法 JSON", json.loads(fn["arguments"]), {"text": "现在几点"})
        check("地址补成 /chat/completions", monkey["url"].endswith("/chat/completions"),
              True)
        check("Authorization 用环境变量里的 key",
              monkey["headers"]["Authorization"], "Bearer sk-test")
        check("带上了工具定义", bool(monkey["payload"].get("tools")), True)
        check("system prompt 在第一句", monkey["payload"]["messages"][0],
              {"role": "system", "content": "你是测试"})

        # 历史窗口
        llm_mod.requests.post = fake_stream(monkey, ['data: {"choices":[{"delta":{}}]}'])
        client.cfg.history_turns = 1
        client.commit("u1", "a1")
        client.commit("u2", "a2")
        list(client.chat_events("u3"))
        roles = [(m["role"], m["content"]) for m in monkey["payload"]["messages"]]
        check("只带最近一轮历史", roles[1:], [("user", "u2"), ("assistant", "a2"),
                                             ("user", "u3")])

        # 没有 key
        llm_mod.requests.post = fake_stream(monkey, [])
        no_key = OpenAICompatClient(ApiConfig(api_key="", api_key_env="NO_SUCH_KEY_XYZ"))
        check("没 key 时 ready=False", no_key.ready()[0], False)
        try:
            list(no_key.chat_events("hi"))
            check("没 key 会报错", False, True)
        except ApiError as exc:
            check("没 key 报 ApiError", "API key" in str(exc), True)

        # 密钥文件：仓库里只放路径，key 放仓库外
        with tempfile.TemporaryDirectory(prefix="api_key_") as kd:
            key_file = Path(kd) / "deepseek.txt"
            key_file.write_text("\nsk-from-file\n", encoding="utf-8")
            from_file = OpenAICompatClient(ApiConfig(api_key_file=str(key_file)))
            check("api_key_file 能读到 key", from_file.api_key(), "sk-from-file")
            check("密钥文件也算 ready", from_file.ready()[0], True)
            explicit = OpenAICompatClient(ApiConfig(
                api_key="sk-explicit", api_key_file=str(key_file)))
            check("api_key 优先于文件", explicit.api_key(), "sk-explicit")
            env_key = OpenAICompatClient(ApiConfig(
                api_key="env:DEEPSEEK_API_KEY", api_key_file=str(key_file)))
            check("env: 写法优先于文件", env_key.api_key(), "sk-test")

        # 401
        llm_mod.requests.post = fake_stream(monkey, ["unauthorized"], status=401)
        bad = OpenAICompatClient(ApiConfig(api_key="sk-bad"))
        try:
            list(bad.chat_events("hi"))
            check("401 会报错", False, True)
        except ApiError as exc:
            check("401 报 ApiError 且带状态码", "401" in str(exc), True)
    finally:
        llm_mod.requests.post = old_post


def test_facade() -> None:
    print("\n[4] 门面：本地 / API 切换与降级")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    c = ChatClient(LlmConfig(), ApiConfig(), None)
    check("默认本地", c.engine, "local")
    check("没 key 时 api 请求退回本地", c.configure("api"), "local")
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    c.configure("api")
    check("有 key 时切到 api", c.engine, "api")
    c.system_prompt = "人设"
    c.temperature = 0.2
    check("人设同步到两边", (c.local.system_prompt, c._api.system_prompt), ("人设", "人设"))
    check("温度同步到两边", (c.local.temperature, c._api.temperature), (0.2, 0.2))
    check("关闭 [api] 时强制本地",
          ChatClient(LlmConfig(), ApiConfig(enabled=False), None).configure("api"), "local")
    check("写错引擎名按本地", ChatClient(LlmConfig(), ApiConfig(), None).configure("turbo"),
          "local")

    # 看图永远走本地
    c2 = ChatClient(LlmConfig(), ApiConfig(), None)
    c2.configure("api")
    c2._api.chat_events = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走 API"))
    c2.local.chat_events = lambda *a, **k: iter([{"delta": "local-image"}])
    check("带图时走本地视觉模型", list(c2.chat_events("看图", images=["b64"])),
          [{"delta": "local-image"}])
    check("这一轮的历史记在本地", (c2.commit("看图", "x"), c2.local._history[-1]),
          (None, {"role": "assistant", "content": "x"}))

    # API 没通且一个字都没念 → 本地重来
    c3 = ChatClient(LlmConfig(), ApiConfig(), None)
    c3.configure("api")
    c3._api.chat_events = lambda *a, **k: (_ for _ in ()).throw(ApiError("boom"))
    c3.local.chat_events = lambda *a, **k: iter([{"delta": "fallback"}])
    check("API 没通自动改本地", list(c3.chat_events("hi")), [{"delta": "fallback"}])
    check("降级后引擎记本地", c3.engine, "local")
    c3.commit("hi", "fallback")
    check("历史跟着本地", c3.local._history[-1]["content"], "fallback")

    # 已经念了半句就如实报错（不能重复）
    c4 = ChatClient(LlmConfig(), ApiConfig(), None)
    c4.configure("api")

    def half(*a, **k):
        yield {"delta": "半"}
        raise ApiError("mid")

    c4._api.chat_events = half
    try:
        list(c4.chat_events("hi"))
        check("半途失败要报错", False, True)
    except ApiError:
        check("半途失败不重复念", True)


def write_index(tmp: Path) -> Path:
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "fish.json").write_text(json.dumps({
        "id": "fish", "name": "海鱼", "wake_words": ["海鱼"],
        "engine": "api", "api_model": "deepseek-chat",
        "api_tools": ["mcp__memory__memory_stats"],
    }, ensure_ascii=False), encoding="utf-8")
    (personas / "local.json").write_text(json.dumps({
        "id": "local", "name": "本地", "wake_words": ["本地"],
        "api_tools": ["mcp__memory__memory_stats"],
    }, ensure_ascii=False), encoding="utf-8")
    index = tmp / "characters.json"
    index.write_text(json.dumps({
        "default": "local",
        "characters": [
            {"id": "fish", "file": "personas/fish.json"},
            {"id": "local", "file": "personas/local.json"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return index


def test_pipeline(tmp: Path) -> None:
    print("\n[5] 接进流水线：按角色切引擎 / API 角色多开工具")
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    index = write_index(tmp)
    settings = load_settings()
    settings.persona.file = str(index)
    settings.subtitle.enabled = False
    settings.skills.visual_alert = False
    settings.skills.data_dir = str(tmp)
    settings.skills.event_file = str(tmp / "events.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.app.sessions_dir = str(tmp / "sessions")

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(settings, enable_listening=False, lazy_whisper=True)
    try:
        loop._apply_character(loop.persona.get("fish"), "测试")  # noqa: SLF001
        check("API 角色切到云端", loop.llm.engine, "api")
        names = {s["function"]["name"] for s in (loop._tool_specs() or [])}  # noqa: SLF001
        check("API 角色多开了白名单外的工具", "mcp__memory__memory_stats" in names,
              detail=str(sorted(names)))
        check("原有 10 个工具仍在", len(names) == 11, detail=str(len(names)))

        loop._apply_character(loop.persona.get("local"), "测试")  # noqa: SLF001
        check("本地角色回本地", loop.llm.engine, "local")
        names = {s["function"]["name"] for s in (loop._tool_specs() or [])}  # noqa: SLF001
        check("本地角色不看 api_tools", "mcp__memory__memory_stats" in names, False)
        check("本地角色仍是 10 个", len(names) == 10, detail=str(len(names)))

        reg = loop.persona
        before = reg._signature()  # noqa: SLF001
        reg.get("local").engine = "api"
        check("改引擎会算进热加载签名", reg._signature() != before)  # noqa: SLF001
    finally:
        loop.close()


def test_shipped_characters() -> None:
    print("\n[6] 仓库自带的角色：默认本地 / 原生 API 角色在位")
    reg = CharacterRegistry(ROOT / "data" / "characters.json")
    default = reg.default()
    check("默认角色仍是白泽", getattr(default, "name", ""), "白泽")
    check("默认角色不跟 API", default.engine, "")
    fish = reg.get("bluefish")
    check("蓝色大肥鱼挂着", fish is not None)
    if fish is None:
        return
    check("它是 API 角色", fish.engine, "api")
    check("模型是 deepseek-chat", fish.api_model, "deepseek-chat")
    check("TTS 也走官方 API", (fish.backend, fish.tts_voice), ("deepseek", "stella"))
    check("额外开放了 harness 工具", {"web", "files"} <= set(fish.api_tools),
          detail=str(fish.api_tools))
    check("退路是出厂 Piper（不碰克隆素材）",
          (fish.voice_ref, fish.voice_model, (fish.tts or {}).get("fallback")),
          ("", "", ["piper:zh_CN-huayan-medium"]))
    check("没有把密钥写进文件", bool((fish.api_key or "").strip()), False)


# --------------------------------------------------------------------------- #
def main() -> int:
    test_config()
    test_character_fields()
    test_openai_client()
    test_facade()
    with tempfile.TemporaryDirectory(prefix="api_llm_") as d:
        test_pipeline(Path(d))
    test_shipped_characters()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  {len(_failures)} 项没通过：{'、'.join(_failures)}")
        return 1
    print("  API 后端自测：全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
