"""DeepSeek 官方 TTS 后端自测（不联网）。

    python scripts\\test_api_tts.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import ApiConfig, load_settings          # noqa: E402
from voice_loop.tts import api_tts                               # noqa: E402
from voice_loop.tts.api_tts import (ApiTts, api_tts_ready, endpoint,  # noqa: E402
                                    voice_of)
from voice_loop.tts.piper_tts import PiperTts                    # noqa: E402

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
    def __init__(self, body: bytes, status: int = 200, ctype: str = "audio/pcm") -> None:
        self.body = body
        self.status_code = status
        self.text = body.decode("utf-8", "ignore") if status != 200 else ""
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i : i + chunk_size]


def fake_post(monkey: dict, resp: FakeResponse):
    def _post(url, json=None, headers=None, stream=False, timeout=None):
        monkey.update(url=url, payload=json, headers=headers, stream=stream, timeout=timeout)
        return resp

    return _post


def pcm_bytes(samples: int) -> bytes:
    t = np.arange(samples, dtype=np.float32) / 24000.0
    pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2")
    return pcm.tobytes()


def base_settings(tmp: Path):
    s = load_settings()
    s.tts.backend = "api"
    s.api.enabled = True
    s.api.tts_enabled = True
    s.api.api_key = ""
    s.api.api_key_env = "NO_SUCH_KEY_FOR_TEST"
    key = tmp / "key.txt"
    key.write_text("sk-tts-test\n", encoding="utf-8")
    s.api.api_key_file = str(key)
    return s


# --------------------------------------------------------------------------- #
def test_config() -> None:
    print("\n[1] 配置：地址推导 / 音色 / 可用性")
    check("裸域名推出官方地址",
          endpoint(ApiConfig(base_url="https://api.deepseek.com")),
          "https://api.deepseek.com/v1/tools/tts")
    check("已经带 /v1 的地址不重复拼",
          endpoint(ApiConfig(base_url="https://api.deepseek.com/v1")),
          "https://api.deepseek.com/v1/tools/tts")
    check("显式 tts_url 优先",
          endpoint(ApiConfig(base_url="https://x.test", tts_url="https://y.test/tts")),
          "https://y.test/tts")
    check("chat 端点也能推出 TTS 地址",
          endpoint(ApiConfig(base_url="https://api.deepseek.com/chat/completions")),
          "https://api.deepseek.com/v1/tools/tts")
    check("默认音色 stella", voice_of(ApiConfig()), "stella")
    check("支持 tide", voice_of(ApiConfig(tts_voice="tide")), "tide")
    check("写错音色退回 stella", voice_of(ApiConfig(tts_voice="nope")), "stella")

    with tempfile.TemporaryDirectory(prefix="api_tts_cfg_") as d:
        s = base_settings(Path(d))
        check("有 key 时可用", api_tts_ready(s)[0], True)
        s.api.tts_enabled = False
        check("tts_enabled=false 时不可用", api_tts_ready(s)[0], False)
        s.api.tts_enabled = True
        s.api.api_key_file = str(Path(d) / "missing.txt")
        check("key 文件不存在时不可用", api_tts_ready(s)[0], False)


def test_pcm(tmp: Path) -> None:
    print("\n[2] PCM 流：请求体 / 边收边吐 / 采样率")
    old = api_tts.requests.post
    try:
        monkey: dict = {}
        body = pcm_bytes(12000)                 # 0.5 秒 @24k
        api_tts.requests.post = fake_post(monkey, FakeResponse(body))
        s = base_settings(tmp)
        s.api.tts_voice = "tide"
        engine = ApiTts(s)
        blocks = list(engine.synth("你好，我是一条鱼。"))
        total = sum(pcm.size for _r, pcm in blocks)
        check("总样本数一根不差", total, 12000)
        check("采样率 24 kHz", {r for r, _ in blocks}, {24000})
        check("按块吐（不是等整段）", len(blocks) >= 2, detail=str(len(blocks)))
        check("请求 text 原样", monkey["payload"]["text"], "你好，我是一条鱼。")
        check("请求音色 tide", monkey["payload"]["voice_id"], "tide")
        check("请求格式 pcm", monkey["payload"]["format"], "pcm")
        check("去 Markdown 开关带上了", monkey["payload"]["filter_markdown"], True)
        check("带 Bearer", monkey["headers"]["Authorization"], "Bearer sk-tts-test")
        check("地址是官方 TTS", monkey["url"], "https://api.deepseek.com/v1/tools/tts")
        check("dtype 是 int16", blocks[0][1].dtype, np.dtype("int16"))
    finally:
        api_tts.requests.post = old


def test_wav(tmp: Path) -> None:
    print("\n[3] WAV 模式：带文件头也能读")
    old = api_tts.requests.post
    try:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm_bytes(4800))
        monkey: dict = {}
        api_tts.requests.post = fake_post(
            monkey, FakeResponse(buf.getvalue(), ctype="audio/wav"))
        s = base_settings(tmp)
        s.api.tts_format = "wav"
        engine = ApiTts(s)
        blocks = list(engine.synth("你好"))
        check("WAV 也能播", sum(p.size for _r, p in blocks), 4800)
        check("WAV 采样率来自文件头", {r for r, _ in blocks}, {24000})
        check("请求格式 wav", monkey["payload"]["format"], "wav")
    finally:
        api_tts.requests.post = old


def test_fallback(tmp: Path) -> None:
    print("\n[4] 失败兜底：API 不通就退回 Piper")
    old = api_tts.requests.post
    try:
        api_tts.requests.post = fake_post(
            {}, FakeResponse(b'{"error":"bad"}', status=400))
        s = base_settings(tmp)
        engine = ApiTts(s)

        class FakeFallback:
            def synth(self, text):
                yield 22050, np.array([11, 22, 33], dtype=np.int16)

            def close(self):
                pass

        engine._fallback_engine = lambda: FakeFallback()   # noqa: SLF001
        blocks = list(engine.synth("你好"))
        check("退回后仍有声音", sum(p.size for _r, p in blocks), 3)
        check("退回的是 Piper 的采样率", blocks[0][0], 22050)
    finally:
        api_tts.requests.post = old


def test_backend(tmp: Path) -> None:
    print("\n[5] 后端起用与回退")
    from voice_loop.tts import VoiceSpec, registry_for

    s = base_settings(tmp)
    s.tts.provider = "deepseek"
    spec = VoiceSpec(provider="deepseek", voice_id="stella")
    reg = registry_for(s)
    resolved = reg.effective(spec)
    check("配好 key 时落到 ApiTts", resolved.provider.id, "deepseek")
    check("实例就是 ApiTts", type(reg.open(spec)).__name__, "ApiTts")
    check("说明里带音色", "stella" in resolved.settings.api.tts_voice, True,
          resolved.settings.api.tts_voice)
    s.api.tts_enabled = False
    reg2 = registry_for(s)
    check("关掉 API TTS → 降级链退到 Piper",
          reg2.open(spec).__class__.__name__, "PiperTts")


def test_pipeline(tmp: Path) -> None:
    print("\n[6] 接进 pipeline：按角色切到官方 TTS / 失败回本地")
    personas = tmp / "personas"
    personas.mkdir(parents=True)
    (personas / "fish.json").write_text(json.dumps({
        "id": "fish", "name": "海鱼", "wake_words": ["海鱼"], "engine": "api",
        "tts": {"provider": "deepseek", "voice_id": "tide",
                "fallback": ["piper:zh_CN-huayan-medium"]},
    }, ensure_ascii=False), encoding="utf-8")
    (personas / "local.json").write_text(json.dumps({
        "id": "local", "name": "本地", "wake_words": ["本地"],
        "tts": {"provider": "piper", "voice_id": "zh_CN-huayan-medium"},
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
    s.api.tts_enabled = True
    s.api.tts_voice = "stella"
    s.api.api_key = ""
    s.api.api_key_env = "NO_SUCH_KEY_FOR_TEST"
    key = tmp / "key.txt"
    key.write_text("sk-tts-test\n", encoding="utf-8")
    s.api.api_key_file = str(key)

    from voice_loop.pipeline import VoiceLoop

    loop = VoiceLoop(s, enable_listening=False, lazy_whisper=True)
    try:
        loop._apply_character(loop.persona.get("fish"), "测试")   # noqa: SLF001
        check("角色把后端切到 deepseek", loop.tts.provider_id, "deepseek")
        check("角色音色覆盖为 tide（只在副本里）", loop.settings.api.tts_voice, "stella")
        check("★全局配置没被改，但这一轮真的用 tide★",
              loop.tts.spec.voice_id, "tide")
        check("横幅说的是官方 TTS", "DeepSeek TTS" in loop._tts_label(),
              detail=loop._tts_label())  # noqa: SLF001
        loop._apply_character(loop.persona.get("local"), "测试")  # noqa: SLF001
        check("本地角色切回 piper", loop.tts.provider_id, "piper")

        # key 没了 → 同一个 API 角色也不能把嘴弄哑
        loop.settings.api.api_key_file = str(tmp / "no-key.txt")
        loop._apply_character(loop.persona.get("fish"), "测试")   # noqa: SLF001
        check("API TTS 不可用时退回 piper", settings_backend(loop), "piper")
    finally:
        loop.close()


def settings_backend(loop) -> str:
    """这一轮**实际会出声**的后端：按降级链挑第一个可用的（不看全局配置）。"""
    reg = getattr(loop.tts, "registry", None)
    spec = getattr(loop.tts, "spec", None)
    if reg is None or spec is None:
        return str(getattr(loop.tts, "name", "") or "").lower()
    for candidate in reg.chain(spec):
        provider = reg.get(candidate.provider)
        if provider is None:
            continue
        if reg._ready(provider)[0]:              # noqa: SLF001 - 自测里用内部判定
            return provider.id
    return ""


# --------------------------------------------------------------------------- #
def main() -> int:
    test_config()
    with tempfile.TemporaryDirectory(prefix="api_tts_") as d:
        d = Path(d)
        test_pcm(d)
        test_wav(d)
        test_fallback(d)
        test_backend(d)
        test_pipeline(d)
    print("\n" + "=" * 66)
    if _failures:
        print(f"  {len(_failures)} 项没通过：{'、'.join(_failures)}")
        return 1
    print("  官方 TTS 自测：全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
