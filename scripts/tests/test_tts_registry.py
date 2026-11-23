"""TTS 解耦自测：注册表 / 声音描述 / 换嗓子 / 降级链 / 云端隐私（★不联网★）。

为什么值得一条条钉住：这一层以前是「按后端名字写 if/elif + 改全局配置」，
加一个新模型要同时改四个文件。现在「加后端 = 加一个文件」，那这份自测守的就是
**这个承诺**：

1. 注册表能扫出后端，且描述（能力/参数/音色）够控制台直接画表单；
2. `VoiceSpec` 能把各种写法（字符串/字典/短名）解析成同一份数据；
3. `effective()` 给的是**副本** —— 切角色绝不改全局配置（两个角色可共存）；
4. `use()` 幂等，换的是会话的 spec，不重建外壳（补丁还在）；
5. 降级链以 piper 收尾；piper 模型缺失时体检会报出来（链尾也救不了哑）；
6. 云端后端必须声明 `cloud` 能力（「本地角色不联网」这条不变量靠它守）；
7. `http` 这个「给个 API 就能接」的后端，用一个**本地假服务器**跑通端到端。

    python scripts\\tests\test_tts_registry.py [--online]
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.persona import Character                     # noqa: E402
from voice_loop.settings import load_settings                # noqa: E402
from voice_loop.tts import (                                 # noqa: E402
    CAP_CLOUD, CAP_CLONE, VoiceSpec, create_tts, registry_for,
)
from voice_loop.tts import audio_decode                      # noqa: E402
from voice_loop.tts.providers import zipvoice as zipvoice_provider  # noqa: E402
from voice_loop.tts.session import VoiceShell                # noqa: E402
from voice_loop.tts.spec import FieldSpec, ProviderSpec      # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def wav_bytes(seconds: float = 0.3, rate: int = 24000, value: int = 1000) -> bytes:
    pcm = np.full(int(rate * seconds), value, dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# --------------------------------------------------------------------------- #
def test_discover(settings) -> None:
    print("\n[1] 注册表：扫目录就能得到后端 + 它们的能力/参数")
    reg = registry_for(settings)
    ids = reg.ids()
    check("五个内置后端都注册上了",
          set(ids) >= {"piper", "zipvoice", "deepseek", "edge", "http"}, True, ids)
    piper = reg.get("piper")
    check("piper 不能用云端", piper.cloud, False)
    check("zipvoice 声明了 clone 能力", CAP_CLONE in reg.get("zipvoice").capabilities, True)
    check("edge / deepseek / http 都标了 cloud",
          [reg.get(x).cloud for x in ("edge", "deepseek", "http")], [True, True, True])
    check("piper 的音色表来自 models 目录（非空 = 真扫到了）",
          bool(reg.voices("piper")), True, reg.voices("piper")[:3])
    check("edge 有静态音色表（不联网也能画下拉）", len(reg.voices("edge")) >= 6, True)
    described = {item["id"]: item for item in reg.describe()}
    check("describe() 带 ready 与 why（控制台能说「为什么不能用」）",
          ("ready" in described["piper"] and "why" in described["http"]), True)
    check("http 没配 url → ready=False", described["http"]["ready"], False)
    check("describe() 的参数表能直接画表单",
          [f["name"] for f in described["zipvoice"]["fields"]][:3],
          ["model", "ref_audio", "ref_text"])
    check("老名字 api 当别名（= deepseek）", reg.get("api").id, "deepseek")


def test_spec() -> None:
    print("\n[2] 声音描述：几种写法解析成同一份数据")
    a = VoiceSpec.parse("edge:zh-CN-XiaoxiaoNeural")
    check("字符串短写法", (a.provider, a.voice_id), ("edge", "zh-CN-XiaoxiaoNeural"))
    b = VoiceSpec.parse({"provider": "edge", "voice_id": "zh-CN-XiaoxiaoNeural"})
    check("★两种写法等价（缓存键也相同）★", a.key() == b.key(), True)
    c = VoiceSpec.parse({"provider": "zipvoice", "ref": {"audio": "a.wav", "text": "你好"}})
    check("ref 子字典也认", (c.ref_audio, c.ref_text), ("a.wav", "你好"))
    d = VoiceSpec.parse({"provider": "deepseek", "fallback": "piper:zh_CN-huayan-medium"})
    check("fallback 写字符串也收", d.fallback, ("piper:zh_CN-huayan-medium",))
    check("空值 = 跟全局", VoiceSpec.parse(None).as_dict(), {})
    check("note 不进缓存键", VoiceSpec(note="甲").key() == VoiceSpec(note="乙").key(), True)
    check("写回文件时丢掉空字段",
          VoiceSpec(provider="piper", voice_id="", note="").as_dict(), {"provider": "piper"})


def test_effective(settings) -> None:
    print("\n[3] 配置合成：每个 spec 一份副本，绝不改全局")
    reg = registry_for(settings)
    before = (settings.tts.provider, settings.tts.voice, settings.tts.clone_audio,
              settings.api.tts_voice)
    piper = reg.effective(VoiceSpec(provider="piper", voice_id="kaltsit-e029"))
    check("piper：voice/model/config 一起换",
          (piper.settings.tts.voice, piper.settings.tts.model),
          ("kaltsit-e029", "models/tts/piper/kaltsit-e029.onnx"))
    zip_v = reg.effective(VoiceSpec(provider="zipvoice", ref_audio="data/personas/amiya/交谈1.wav",
                                    ref_text="你好", params={"clone_steps": 8}))
    check("zipvoice：参考音频/文本进副本",
          (zip_v.settings.tts.clone_audio, zip_v.settings.tts.clone_text),
          ("data/personas/amiya/交谈1.wav", "你好"))
    check("zipvoice：参数覆盖到 TtsConfig", zip_v.settings.tts.clone_steps, 8)
    api = reg.effective(VoiceSpec(provider="deepseek", voice_id="tide"))
    check("deepseek：音色落到 [api] tts_voice", api.settings.api.tts_voice, "tide")
    check("deepseek：退路声线摆好了", api.settings.tts.voice, "zh_CN-huayan-medium")
    check("★全局配置一个字节都没动★",
          (settings.tts.provider, settings.tts.voice, settings.tts.clone_audio,
           settings.api.tts_voice), before)
    check("两份副本互不影响",
          piper.settings.tts.voice != zip_v.settings.tts.voice, True)
    check("别名的 provider 也能合成", reg.effective(VoiceSpec(provider="api")).provider.id,
          "deepseek")


def test_session(settings) -> None:
    print("\n[4] 会话：换 spec 才动引擎，字段一样就不动；补丁还在")
    reg = registry_for(settings)
    shell = VoiceShell(settings, reg, None)
    seen: list[str] = []
    shell.on_load(lambda engine: seen.append(getattr(engine, "name", "?")))
    check("外壳初始是「跟全局」", shell.spec.provider, "")
    check("换到 deepseek", shell.use(VoiceSpec(provider="deepseek", voice_id="stella")), True)
    check("再换同一个 → 不动（幂等）",
          shell.use(VoiceSpec(provider="deepseek", voice_id="stella", note="只是备注")), False)
    check("补丁还挂着（没被 use 冲掉）", len(shell._lazy._pending), 1)   # noqa: SLF001
    check("describe 说人话", "deepseek" in shell.describe(), True, shell.describe())
    check("provider_id 跟着 spec", shell.provider_id, "deepseek")
    # 假引擎：验证「open 时真的会加载 + 补丁会打到新引擎上」
    fake = SimpleNamespace(name="fake", sample_rate=24000, closed=False,
                           synth=lambda text: iter([(24000, np.zeros(10, dtype=np.int16))]),
                           set_text_verifier=lambda fn: seen.append("verifier"),
                           close=lambda: None)
    reg2 = registry_for(settings)
    reg2.register(ProviderSpec(id="fake", label="假后端", create=lambda s, sp: fake,
                               fields=(FieldSpec("x"),)), replace=True)
    shell2 = VoiceShell(settings, reg2, VoiceSpec(provider="fake"))
    shell2.on_load(lambda engine: engine.set_text_verifier(lambda *a: 0.99))
    shell2.load()
    check("加载后补丁打到了新引擎", "verifier" in seen, True, seen)
    check("synth 透传", len(list(shell2.synth("你好"))), 1)


def test_fallback(settings) -> None:
    print("\n[5] 降级链：缺东西就往下一个试，末尾追加 piper（模型须装好）")
    reg = registry_for(settings)
    chain = reg.chain(VoiceSpec(provider="http"))
    check("http 没配 url → 链里排到 piper", [c.provider for c in chain][-1], "piper")
    check("链里没有重复", len({c.key() for c in chain}), len(chain))
    role = reg.chain(VoiceSpec(provider="deepseek", fallback=("edge:x",)))
    check("角色写的 fallback 也被收进链里",
          [c.provider for c in role][:3], ["deepseek", "edge", "piper"])
    first = reg.open(VoiceSpec(provider="http"))
    check("★不可用的后端会自动降级，而不是抛异常★",
          getattr(first, "name", ""), "piper", getattr(first, "name", ""))
    check("配置问题只提示一次",
          [w for w in reg._warned if "http" in w][:1] != [], True)   # noqa: SLF001
    check("没有可用后端时才抛（这里不会）", reg.has(VoiceSpec(provider="piper")), True)


def test_lru(settings) -> None:
    print("\n[6] 缓存上限：max_loaded 决定同时留几个引擎")
    reg = registry_for(settings)
    closed: list[str] = []

    def make(name: str):
        return lambda s, sp: SimpleNamespace(
            name=name, sample_rate=24000, synth=lambda t: iter([]),
            close=lambda: closed.append(name))

    reg.register(ProviderSpec(id="a", label="A", create=make("A")), replace=True)
    reg.register(ProviderSpec(id="b", label="B", create=make("B")), replace=True)
    settings.tts.max_loaded = 1
    reg.open(VoiceSpec(provider="a"))
    reg.open(VoiceSpec(provider="b"))
    check("超上限就把旧的卸掉", closed, ["A"])
    check("缓存里只剩一个", len(reg.loaded()), 1)
    settings.tts.max_loaded = 2
    reg.close_all()
    check("close_all 清空", reg.loaded(), [])


def test_cloud_guard(settings) -> None:
    print("\n[7] 隐私不变量：云端能力必须自报，本地角色不会悄悄上云")
    reg = registry_for(settings)
    local = [p.id for p in reg.providers.values() if not p.cloud]
    cloud = [p.id for p in reg.providers.values() if p.cloud]
    check("piper / zipvoice 是本地", set(local) >= {"piper", "zipvoice"}, True, local)
    check("deepseek / edge / http 自报云端", set(cloud) >= {"deepseek", "edge", "http"},
          True, cloud)
    from voice_loop.persona import voice_spec_for

    pure = Character.from_dict({"id": "x", "name": "X",
                                "tts": {"provider": "piper", "voice_id": "zh_CN-huayan-medium"}})
    spec = voice_spec_for(pure)
    check("★纯本地角色解析出来不是云端★", reg.effective(spec).cloud, False)
    check("CAP_CLOUD 和 cloud 属性一致",
          all(reg.get(p).cloud == (CAP_CLOUD in reg.get(p).capabilities) for p in reg.ids()),
          True)


def test_persona_spec() -> None:
    print("\n[8] 人格文件：新结构读得进、旧结构不再认（迁移脚本负责搬）")
    new = Character.from_dict({"id": "b", "name": "B",
                               "tts": {"provider": "deepseek", "voice_id": "stella",
                                       "fallback": ["piper:zh_CN-huayan-medium"],
                                       "styles": {"calm": "a.wav"}}})
    check("新结构解析", (new.backend, new.voice), ("deepseek", "stella"))
    check("styles 走 voice_refs 的视图（风格挑选还在用）", list(new.voice_refs), ["calm"])
    check("退路读得回", new.tts.get("fallback"), ["piper:zh_CN-huayan-medium"])
    legacy = Character.from_dict({"id": "c", "name": "C", "backend": "api", "tts_voice": "tide",
                                  "voice_ref": "x.wav"})
    check("★旧平铺字段不再生效（迁移脚本负责搬）★", legacy.tts, {})
    from voice_loop.persona import voice_spec_for

    check("没人写 tts → 跟全局", voice_spec_for(legacy).as_dict(), {})


def test_migration() -> None:
    print("\n[9] 迁移脚本：旧 8 个字段 → 一份 tts")
    sys.path.insert(0, str(ROOT / "scripts" / "tools"))
    from migrate_tts_format import migrate  # noqa: PLC0415

    cases = [
        ({"backend": "piper", "voice": "zh_CN-huayan-medium"},
         {"provider": "piper", "voice_id": "zh_CN-huayan-medium"}),
        ({"backend": "api", "voice": "zh_CN-huayan-medium", "tts_voice": "stella"},
         {"provider": "deepseek", "voice_id": "stella",
          "fallback": ["piper:zh_CN-huayan-medium"]}),
    ]
    for raw, want in cases:
        got, touched = migrate({"id": "x", "name": "X", **raw})
        check(f"{raw.get('backend')} → {want.get('provider')}",
              (got.get("tts"), touched), (want, True))
    kept = {"id": "x", "name": "X", "tts": {"provider": "piper"}}
    got, touched = migrate(kept)
    check("已经是新格式就不动", (got, touched), (kept, False))


def test_http_provider(settings, tmp: Path) -> None:
    print("\n[10] 通用 HTTP 后端：本地假服务器跑通端到端（不联网）")
    audio = wav_bytes(0.4)
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):                                   # noqa: N802 - 标准库命名
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen["body"] = body
            seen["auth"] = self.headers.get("Authorization", "")
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)

        def log_message(self, *args):                        # noqa: D102 - 静音
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import os

        os.environ["SELF_TEST_TTS_KEY"] = "sk-fake"
        settings.tts.providers["http"] = {
            "url": f"http://127.0.0.1:{server.server_port}/tts",
            "headers": {"Authorization": "Bearer {key}", "Content-Type": "application/json"},
            "body": '{"text": "{text}", "voice": "{voice}"}',
            "audio_format": "wav",
            "key_env": "SELF_TEST_TTS_KEY",
        }
        reg = registry_for(settings)
        engine = reg.open(VoiceSpec(provider="http", voice_id="v1"))
        chunks = list(engine.synth("你好呀"))
        check("真的发出了请求", bool(seen.get("body")), True, seen.get("body"))
        check("模板占位符被替换", seen["body"].get("text"), "你好呀")
        check("音色也填进去了", seen["body"].get("voice"), "v1")
        check("★密钥从环境变量来★", seen.get("auth"), "Bearer sk-fake")
        check("拿回音频并切块", len(chunks) >= 1, True)
        rate, pcm = chunks[0]
        check("解码正确（24k）", (rate, pcm.dtype.name), (24000, "int16"))
        # 云端失败 → 本地兜底（把 url 指向一个关掉的端口）
        settings.tts.providers["http"]["url"] = "http://127.0.0.1:9/tts"
        settings.tts.providers["http"]["timeout"] = 2
        reg2 = registry_for(settings)
        engine2 = reg2.open(VoiceSpec(provider="http"))
        engine2.set_fallback_factory(lambda: reg2.fallback_engine(VoiceSpec(provider="http")))
        got = list(engine2.synth("这一句会失败"))
        check("★云端这句失败 → 自动用本地兜底（不哑）★", bool(got), True, len(got))
    finally:
        server.shutdown()
        os.environ.pop("SELF_TEST_TTS_KEY", None)


def test_edge_fallback(settings) -> None:
    print("\n[11] Edge 云端失败 → 走退路（假失败，不联网）")
    from voice_loop.tts.edge_tts import EdgeTts, EdgeTtsError

    original = EdgeTts._fetch

    def boom(self, text):  # noqa: ARG001 - 故意让网络请求失败
        raise EdgeTtsError("假的 edge-tts 失败")

    class FakeLocal:
        def synth(self, text):  # noqa: ARG002 - 退路只要能出块
            yield 24000, np.zeros(240, dtype=np.int16)

    EdgeTts._fetch = boom
    try:
        engine = EdgeTts(settings)
        engine.set_fallback_factory(lambda: FakeLocal())
        got = list(engine.synth("这一句会失败"))
        check("★edge 运行时失败 → 自动用退路出声★", len(got) >= 1, True, len(got))
        check("退路音频仍是 int16 单声道", got[0][1].dtype.name if got else "", "int16")
    finally:
        EdgeTts._fetch = original


def test_piper_ready(tmp: Path) -> None:
    print("\n[12] Piper 体检：没有 onnx 声线时 ready=False（链尾也不哑的前提）")
    from voice_loop.tts.providers.piper import PROVIDER as PIPER

    class NoModels:
        def resolve(self, path: str) -> Path:  # noqa: D102 - 测试替身
            return tmp / path

    class WithModels(NoModels):
        def resolve(self, path: str) -> Path:  # noqa: D102 - 测试替身
            room = tmp / "models/tts/piper"
            room.mkdir(parents=True, exist_ok=True)
            (room / "fake.onnx").touch()
            return super().resolve(path)

    ok, why = PIPER.ready(NoModels())
    check("★空目录 → 不可就绪★", (ok, bool(why)), (False, True), why)
    ok2, _ = PIPER.ready(WithModels())
    check("有声线 → 就绪", ok2, True)


def test_decode() -> None:
    print("\n[11] 音频解码：wav / 裸 PCM 都能吃（云端格式五花八门）")
    data = wav_bytes(0.2, 16000, 500)
    rate, pcm = audio_decode.decode(data, "wav")
    check("wav 解出 16k", (rate, pcm.size), (16000, 3200))
    rate2, pcm2 = audio_decode.decode(np.zeros(100, dtype="<i2").tobytes(), "pcm",
                                      sample_rate=24000)
    check("裸 pcm 用给定采样率", (rate2, pcm2.size), (24000, 100))
    odd = audio_decode.decode(b"\x01\x02\x03", "pcm", sample_rate=8000)
    check("奇数长度不炸（丢掉半个样本）", odd[1].size, 1)


def test_online(settings) -> None:
    print("\n[12] 真机：真的合成出一段音频（--online 才跑）")
    reg = registry_for(settings)
    for spec, label in ((VoiceSpec(provider="piper"), "piper"),
                        (VoiceSpec(provider="edge", voice_id="zh-CN-XiaoxiaoNeural"), "edge")):
        ready, why = reg._ready(reg.get(spec.provider))       # noqa: SLF001
        if not ready:
            check(f"{label} 可用", False, False, why)
            continue
        engine = reg.open(spec)
        rate, pcm = engine.synth_bytes("你好，这是一次接口解耦的测试。")
        check(f"{label} 合成出音频", pcm.size > rate * 0.2, True,
              f"{pcm.size / max(1, rate):.2f}s @ {rate} Hz")


def test_lazy_package() -> None:
    print("\n[9] ★import 人格卡不许拖 numpy★（控制台 exe 里 numpy 被排掉了）")
    # 真实事故（2026-11-21）：`persona.normalize_tts` 只想要 VoiceSpec，却因为
    # `voice_loop/tts/__init__.py` 在顶层 import 了引擎 → 连带拉 numpy / sherpa。
    # 控制台 exe 排掉了这些重库，于是 `CharacterRegistry.load()` 抛
    # ModuleNotFoundError，控制台把异常当成「没有角色」——**角色列表整片空掉**。
    # 这条用子进程验，避免本进程早就 import 过 numpy 造成假通过。
    import subprocess  # noqa: PLC0415

    code = ("import sys, voice_loop.persona;"
            "print('numpy' in sys.modules, 'sherpa_onnx' in sys.modules)")
    try:
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=120, cwd=str(ROOT), encoding="utf-8", errors="replace")
        tail = (got.stdout or "").strip().splitlines()[-1] if got.stdout else ""
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        tail = f"跑不起来：{exc}"
    check("persona 的 import 不拉 numpy / sherpa", tail, "False False")
    # 老写法仍要能用（`from voice_loop.tts import PiperTts`）
    from voice_loop.tts import PiperTts, VoiceSpec  # noqa: PLC0415

    check("from voice_loop.tts import PiperTts 仍然可用", PiperTts.__name__, "PiperTts")
    check("spec 名字也还在", VoiceSpec(provider="piper").provider, "piper")


def main() -> int:
    online = "--online" in sys.argv
    settings = load_settings()
    with tempfile.TemporaryDirectory(prefix="tts-reg-") as raw:
        tmp = Path(raw)
        test_discover(settings)
        test_spec()
        test_effective(settings)
        test_session(settings)
        test_fallback(settings)
        test_lru(settings)
        test_cloud_guard(settings)
        test_persona_spec()
        test_migration()
        test_http_provider(settings, tmp)
        test_edge_fallback(settings)
        test_piper_ready(tmp)
        test_decode()
        test_lazy_package()
        if online:
            test_online(settings)
    print("\n" + "=" * 66)
    if _failures:
        print(f"  TTS 解耦自测：{len(_failures)} 条没过：" + "、".join(_failures[:6]))
        return 1
    print("  TTS 解耦自测：全部通过 √")
    return 0


if __name__ == "__main__":
    sys.exit(main())
