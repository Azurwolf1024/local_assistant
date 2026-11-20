"""ASR 解耦自测：注册表 / 策略 / 两级加载 / 词库 / KWS / 评测（★不联网、不加载真模型★）。

为什么这些点值得一条条钉住
    识别是"静默失败"的重灾区：策略写错只是**听起来**差一点、词库误改只是把一句话
    改坏、两级加载漏关只是多占 1 GB —— 都不会报错。所以这里全部用假引擎断言：

    · 注册表能扫到后端、别名归一、`[asr.providers.X]` 只改副本不改全局
    · 「hybrid」这个老策略映射成 spec 后，**三个复核条件逐个**都能触发/不触发
    · 两级加载：`heavy` 引擎在待唤醒状态绝不被构造；关掉时即使正在加载也要卸
    · 词库：四种消费方式都对；★误伤为 0★（「海尔洗衣机」不许改成「凯尔希」）
    · KWS：默认关、没模型时自动退回文本匹配、检测失败当"没命中"
    · 评测：CER / 归一（「九」vs「9」）/ 唤醒命中 / 误唤醒 都能算对

    python scripts\\tests\test_asr_registry.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.asr import eval as asr_eval  # noqa: E402
from voice_loop.asr.base import AsrResult  # noqa: E402
from voice_loop.asr.kws import build_kws  # noqa: E402
from voice_loop.asr.lexicon import KIND_NAME, Lexicon, build_lexicon  # noqa: E402
from voice_loop.asr.registry import AsrRegistry  # noqa: E402
from voice_loop.asr.session import Hearing  # noqa: E402
from voice_loop.asr.spec import (  # noqa: E402
    CAP_CLOUD, CAP_FAST, CAP_HEAVY, CAP_LOCAL, HOTWORDS_NATIVE, HOTWORDS_POST,
    HOTWORDS_PROMPT, AsrSpec, FieldSpec, ProviderSpec, parse_when,
)
from voice_loop.settings import (  # noqa: E402
    AsrConfig, AsrKwsConfig, AsrLexiconConfig, load_settings,
)

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# 假引擎 / 假后端
# --------------------------------------------------------------------------- #
class FakeEngine:
    """一句话引擎：固定返回 text，延迟可控（用来测策略，不测模型）。"""

    def __init__(self, name: str, text: str = "你好", latency: float = 0.01) -> None:
        self.name = name
        self.text = text
        self.latency = latency
        self.calls = 0
        self.closed = False

    def transcribe(self, samples, sample_rate: int = 16000) -> AsrResult:
        self.calls += 1
        return AsrResult(text=self.text, engine=self.name, latency=self.latency,
                         audio_seconds=len(samples) / float(sample_rate or 16000))

    def close(self) -> None:
        self.closed = True


def fake_provider(pid: str, *, text: str = "你好", caps=frozenset({CAP_LOCAL}),
                  hotwords: str = HOTWORDS_POST, heavy_log: list | None = None,
                  latency: float = 0.01, made: dict | None = None) -> ProviderSpec:
    """造一个假后端；``heavy_log`` 数"构造过几次"，``made`` 存实例（数调用次数）。"""
    def create(settings, spec):
        engine = FakeEngine(pid, text=spec.params.get("text", text), latency=latency)
        if heavy_log is not None:
            heavy_log.append(pid)
        if made is not None:
            made[pid] = engine
        return engine

    return ProviderSpec(
        id=pid, label=f"假后端 {pid}", capabilities=caps, hotwords=hotwords,
        fields=(FieldSpec("text", "固定返回", "text", default=text),),
        create=create,
    )


def make_registry(settings, *providers: ProviderSpec) -> AsrRegistry:
    """在**给定配置**上建注册表（★和 Hearing 共用同一个 settings★，否则策略读不到）。"""
    registry = AsrRegistry(settings)
    registry._discovered = True          # 不扫真目录，只测我们自己注册的
    for provider in providers:
        registry.register(provider, replace=True)
    return registry


def make_settings(tmp: Path, **kwargs) -> SimpleNamespace:
    """够 Hearing / Lexicon / KWS 用的配置壳子（★不读真实 config.toml★）。"""
    asr = AsrConfig(**kwargs)
    asr.providers = {}
    skills = SimpleNamespace(data_dir=str(tmp / "data"))
    persona = SimpleNamespace(file=str(tmp / "characters.json"), default="baize")
    return SimpleNamespace(
        root=tmp, asr=asr, skills=skills, persona=persona,
        wake=SimpleNamespace(file=str(tmp / "wakewords.json")),
        llm=SimpleNamespace(model="x", host="http://127.0.0.1:11434"),
        resolve=lambda p: Path(p) if Path(p).is_absolute() else Path(tmp) / p,
    )


def audio(seconds: float = 1.0, rate: int = 16000) -> np.ndarray:
    return np.zeros(int(rate * seconds), dtype=np.float32)


# --------------------------------------------------------------------------- #
def test_registry(tmp: Path) -> None:
    print("\n[1] 注册表：扫目录 / 别名 / 三个内置后端的能力")
    settings = load_settings()
    registry = AsrRegistry(settings)
    found = registry.discover()
    check("扫到了内置后端", found >= 2, True, f"{found} 个：{registry.ids()}")
    check("sensevoice 在", "sensevoice" in registry.ids(), True)
    check("whisper 在", "whisper" in registry.ids(), True)
    check("别名 whisper-ov → whisper", registry.normalize("whisper-ov"), "whisper")

    sensevoice = registry.get("sensevoice")
    whisper = registry.get("whisper")
    check("sensevoice 自称 fast（能当回听校验的快路径）",
          sensevoice.supports(CAP_FAST), True)
    check("sensevoice 词库只能事后纠正", sensevoice.hotwords, HOTWORDS_POST)
    check("whisper 自称 heavy（要走两级加载）", whisper.supports(CAP_HEAVY), True)
    check("whisper 词库走事后纠正（这个导出没有 prompt 入口）", whisper.hotwords,
          HOTWORDS_POST)
    sherpa = registry.get("sherpa")
    if sherpa is not None:
        check("sherpa 原生热词", sherpa.hotwords, HOTWORDS_NATIVE)
    check("没有任何后端自称 cloud（本版本不做云端）",
          any(p.supports(CAP_CLOUD) for p in registry.providers.values()), False)


def test_default_spec(tmp: Path) -> None:
    print("\n[2] `[asr]` → spec：新写法、老 strategy 写法、写坏了不炸")
    settings = make_settings(tmp)
    registry = make_registry(settings, fake_provider("sensevoice"),
                             fake_provider("whisper", caps=frozenset({CAP_LOCAL, CAP_HEAVY})))
    registry.settings.asr = AsrConfig()          # 默认：provider=sensevoice / verify=whisper
    spec = registry.default_spec()
    check("新写法：主引擎", spec.provider, "sensevoice")
    check("新写法：复核引擎", spec.verify, "whisper")
    check("新写法：三个复核条件", [w.as_text() for w in spec.verify_when],
          ["empty", "long:6", "score<0.45"])

    registry.settings.asr = AsrConfig(strategy="sensevoice")
    spec = registry.default_spec()
    check("老写法 sensevoice → 不复核", (spec.provider, spec.verify), ("sensevoice", ""))

    registry.settings.asr = AsrConfig(strategy="whisper")
    spec = registry.default_spec()
    check("老写法 whisper → 主引擎是 whisper", spec.provider, "whisper")

    registry.settings.asr = AsrConfig(strategy="hybrid", whisper_min_duration=9.5)
    spec = registry.default_spec()
    check("老写法 hybrid → 三条复核条件都在", len(spec.verify_when), 3)
    check("★长的阈值跟着 whisper_min_duration 走★",
          [w.as_text() for w in spec.verify_when][1], "long:9.5")

    registry.settings.asr = AsrConfig(strategy="whisperx")
    spec = registry.default_spec()
    check("写坏了的 strategy 不会炸，退回新字段", spec.provider, "sensevoice")

    check("复核条件解析：score<0.45", parse_when("score<0.45")[0].value, 0.45)
    check("复核条件解析：乱写就忽略", parse_when(["", "什么鬼"]), ())


def test_effective(tmp: Path) -> None:
    print("\n[3] 合成配置副本：优先级 + ★不改全局★")
    settings = make_settings(tmp)
    registry = make_registry(settings, fake_provider("sensevoice"))
    settings.asr = AsrConfig(num_threads=6)
    settings.asr.providers = {"sensevoice": {"num_threads": 2, "language": "en"}}
    before = (settings.asr.num_threads, settings.asr.language)

    resolved = registry.effective(AsrSpec(provider="sensevoice", params={"language": "ja"}))
    check("spec.params 优先级最高（语言）", resolved.settings.asr.language, "ja")
    check("`[asr.providers.X]` 覆盖了默认线程数", resolved.settings.asr.num_threads, 2)
    check("★全局那份没被改★", (settings.asr.num_threads, settings.asr.language), before)
    check("副本是另一个对象", resolved.settings is not settings, True)
    check("副本里的 asr 也是另一个对象", resolved.settings.asr is not settings.asr, True)

    opts = registry.options(AsrSpec(provider="sensevoice", params={"extra": 1}))
    check("options 也按同一套优先级合", (opts.get("num_threads"), opts.get("extra")), (2, 1))


def test_chain(tmp: Path) -> None:
    print("\n[4] 降级链：自己 → fallback → `[asr] fallback` → 一个快的本地引擎")
    settings = make_settings(tmp, provider="heavyone", verify="", fallback=["fastone"])
    registry = make_registry(
        settings,
        fake_provider("heavyone", caps=frozenset({CAP_LOCAL})),
        fake_provider("fastone", caps=frozenset({CAP_LOCAL, CAP_FAST})),
    )
    settings.asr.providers = {}
    chain = [item.provider for item in registry.chain(AsrSpec(provider="heavyone"))]
    check("第一个是自己", chain[0], "heavyone")
    check("第二个是 spec 的 fallback", chain[1], "fastone")
    check("没有重复项", len(chain), len(set(chain)))


# --------------------------------------------------------------------------- #
def test_policy(tmp: Path) -> None:
    print("\n[5] 策略：直接命中 / 空结果复核 / 长音频复核 / 低分复核 / prefer")
    made: dict[str, FakeEngine] = {}

    def build(primary_text: str, verify_text: str, *, when, primary_caps=None):
        settings = make_settings(tmp, provider="p", verify="v")
        settings.asr.verify_when = list(when)
        settings.asr.providers = {}
        registry = make_registry(
            settings,
            fake_provider("p", text=primary_text, made=made,
                          caps=primary_caps or frozenset({CAP_LOCAL, CAP_FAST})),
            fake_provider("v", text=verify_text, made=made, caps=frozenset({CAP_LOCAL})),
        )
        return Hearing(settings, None, registry=registry, preload=False)

    hearing = build("你好呀，今天天气不错。", "复核出来的别的话。",
                    when=("empty", "long:6.0", "score<0.45"))
    got = hearing.transcribe(audio(1.0))
    check("短音频 + 高分 → 直接用它", got.engine, "p")
    check("★没白跑复核（复核引擎根本没被构造）★", "v" in made, False)

    hearing = build("", "听清了，这是复核结果。", when=("empty",))
    got = hearing.transcribe(audio(1.0))
    check("★空结果 → 复核接管★", got.engine, "v")
    check("复核确实跑了一次", made["v"].calls, 1)

    hearing = build("短的。", "长音频复核出来的整句，标点齐全。", when=("long:6.0",))
    check("短音频不触发", hearing.transcribe(audio(1.0)).engine, "p")
    check("★超过 6 秒触发复核（复核更好就用它）★",
          hearing.transcribe(audio(7.0)).engine, "v")

    hearing = build("啊啊啊啊啊啊啊啊", "正常的句子，今天天气不错。", when=("score<0.45",))
    check("★复读 → 质量分低 → 复核接管★", hearing.transcribe(audio(1.0)).engine, "v")

    hearing = build("。", "MING PAO CANADA MING PAO TORONTO", when=("empty",))
    got = hearing.transcribe(audio(0.6))
    check("★主引擎只回一个标点 + 复核吐出整句英文 → 宁可算没听清★", got.engine, "p")
    check("幻觉句没有被采用", got.text, "。")

    hearing = build("随便一句。", "另一句。", when=())
    check("没配复核条件 → 永不复核", hearing.transcribe(audio(7.0)).engine, "p")

    settings = make_settings(tmp, provider="slow", verify="")
    settings.asr.providers = {}
    registry = make_registry(
        settings,
        fake_provider("slow", text="慢慢来", caps=frozenset({CAP_LOCAL})),
        fake_provider("quick", text="快一点", caps=frozenset({CAP_LOCAL, CAP_FAST})),
    )
    hearing = Hearing(settings, None, registry=registry, preload=False)
    check("★prefer=\"fast\" 按能力挑★", hearing.transcribe(audio(1.0), prefer="fast").engine,
          "quick")
    check("不认识的 prefer 退回策略", hearing.transcribe(audio(1.0), prefer="谁啊").engine, "slow")
    check("prefer 写具体后端也认", hearing.transcribe(audio(1.0), prefer="quick").engine, "quick")


def test_heavy(tmp: Path) -> None:
    print("\n[6] 两级加载：待唤醒不构造 heavy、关掉必卸、加载完发现该卸就卸")
    made: dict[str, FakeEngine] = {}
    heavy_log: list[str] = []
    settings = make_settings(tmp, provider="p", verify="v")
    settings.asr.verify_when = ["long:1.0"]
    settings.asr.providers = {}
    registry = make_registry(
        settings,
        fake_provider("p", text="快的", made=made, caps=frozenset({CAP_LOCAL, CAP_FAST})),
        fake_provider("v", text="准的", made=made, heavy_log=heavy_log,
                      caps=frozenset({CAP_LOCAL, CAP_HEAVY})),
    )
    hearing = Hearing(settings, None, registry=registry, heavy_enabled=False)
    check("待唤醒：heavy 引擎一个都没构造", heavy_log, [])
    check("heavy_ids 认得出来", hearing.heavy_ids, ["v"])
    check("heavy_loaded = False", hearing.heavy_loaded, False)
    check("★待唤醒时 transcribe 不会顺手加载 heavy★",
          hearing.transcribe(audio(2.0)).engine, "p")
    check("还是没有 heavy 实例", heavy_log, [])

    check("打开开关", hearing.set_heavy_enabled(True), True)
    check("load_heavy 真的加载了", hearing.load_heavy(), True)
    check("构造了一次", len(heavy_log), 1)
    check("heavy_loaded = True", hearing.heavy_loaded, True)
    check("复核这时才真的跑起来", (hearing.transcribe(audio(2.0)), made["v"].calls)[1], 1)

    check("关掉并卸载", hearing.set_heavy_enabled(False), True)
    check("heavy_loaded 回到 False", hearing.heavy_loaded, False)
    check("老名字也通（whisper_loaded）", hearing.whisper_loaded, False)
    check("引擎收到 close()", made["v"].closed, True)

    hearing.set_heavy_enabled(True)
    hearing.set_heavy_enabled(False)
    check("★加载完发现该卸就卸★", hearing.load_heavy(), False)
    check("没有留下实例", hearing.heavy_loaded, False)



# --------------------------------------------------------------------------- #
def test_lexicon(tmp: Path) -> None:
    print("\n[7] 词库：文件解析 / 人格名并入 / 四种消费方式 / ★误伤为 0★")
    settings = make_settings(tmp)
    room = tmp / "data" / "lexicon"
    room.mkdir(parents=True, exist_ok=True)
    (room / "domain.txt").write_text(
        "# 领域词\n源石 2.0\n罗德岛\n泰拉|泰拉大陆,泰拉世界\n", encoding="utf-8")
    (tmp / "personas").mkdir(parents=True, exist_ok=True)
    (tmp / "personas" / "baize.json").write_text(
        '{"id":"baize","name":"白泽","wake_words":["白泽","小泽"],'
        '"aliases":{"白泽":["百则"]}}', encoding="utf-8")
    (tmp / "characters.json").write_text(
        '{"default":"baize","characters":[{"id":"baize","file":"personas/baize.json"}]}',
        encoding="utf-8")
    settings.asr.lexicon = AsrLexiconConfig(files=[str(room / "*.txt")])

    lex = build_lexicon(settings)
    check("词库建起来了", lex is not None, True)
    words = lex.words()
    check("文件里的词进来了", "源石" in words, True)
    check("人格里的名字进来了", "白泽" in words, True)
    check("人格里的唤醒词进来了", "小泽" in words, True)
    check("名字类被标成 name", any(e.kind == KIND_NAME and e.word == "白泽"
                                for e in lex.entries()), True)
    check("★词条不重复★", len(words), len(set(words)))

    prompt = lex.prompt_text()
    check("Whisper 提示词带上了词", "白泽" in prompt and "源石" in prompt, True)
    hot = lex.hotwords_file()
    check("生成了热词表", bool(hot) and Path(hot).is_file(), True)
    content = Path(hot).read_text(encoding="utf-8") if hot else ""
    check("热词表格式（词 :权重）", "源石 :2" in content, True)

    fixed, hits = lex.fix("白则，现在几点")
    check("★同音错字被改成规范名★", fixed.startswith("白泽"), True, fixed)
    check("命中记录里有原文", hits and hits[0].spoken == "白则", True)

    for text in ("海尔洗衣机怎么用", "开尔文温度是多少", "米亚的论文写完了吗"):
        got, _hits = lex.fix(text)
        check(f"★不许误伤：{text}★", got, text)

    settings.asr.lexicon = AsrLexiconConfig(enabled=False)
    check("关掉就返回 None", build_lexicon(settings), None)


def test_kws(tmp: Path) -> None:
    print("\n[8] KWS：默认关 / 没模型自动退回 / 检测失败当没命中")
    settings = make_settings(tmp)
    check("默认关着 → 不建", build_kws(settings), None)

    settings.asr.kws = AsrKwsConfig(enabled=True, model_dir=str(tmp / "no-such-model"))
    spotter = build_kws(settings)
    check("开着但没模型也建对象（好让界面显示原因）", spotter is not None, True)
    check("ready = False", spotter.ready, False)
    check("原因说清是缺文件", "缺模型文件" in spotter.why, True, spotter.why)
    check("★检测失败一律当没命中★", spotter.detect(audio(1.0)), [])
    spotter.close()


def test_eval(tmp: Path) -> None:
    print("\n[9] 评测：CER / 归一 / 唤醒命中 / 误唤醒")
    check("编辑距离", asr_eval.edit_distance("你好", "你号"), 1)
    check("★归一：全角数字与 ITN★", asr_eval.normalize_text("３点"), "3点")
    check("CER 全对是 0", asr_eval.cer("今天天气不错", "今天天气不错。"), 0.0)
    check("CER 错一个字", round(asr_eval.cer("今天天气不错", "今天天气挺好"), 2), 0.33)
    check("空参考 + 有输出 = 1", asr_eval.cer("", "嗯？"), 1.0)

    from voice_loop.audio import save_wav

    room = tmp / "asr_eval"
    for set_name, files in (("wake", {"w1": "凯尔希"}), ("negative", {"n1": "今天有点累"})):
        (room / set_name).mkdir(parents=True, exist_ok=True)
        for name, ref in files.items():
            wav = room / set_name / f"{name}.wav"
            save_wav(wav, np.zeros(16000, dtype=np.float32), 16000)
            wav.with_suffix(".txt").write_text(ref + "\n", encoding="utf-8")
    cases = asr_eval.load_cases(room)
    check("素材读到了", len(cases), 2)
    check("参考文本读到了", any(c.ref == "凯尔希" for c in cases), True)

    settings = make_settings(tmp, provider="p", verify="")
    settings.asr.providers = {}
    registry = make_registry(settings,
                             fake_provider("p", text="凯尔希", caps=frozenset({CAP_LOCAL})))
    hearing = Hearing(settings, None, registry=registry)
    try:
        report = asr_eval.run(settings, cases, hearing=hearing)
    finally:
        hearing.close()
    summary = report.summary()
    check("CER 按素材平均（唤醒那条全对、负样本那条全错 → 0.5）", summary["cer"], 0.5)
    check("每条也都单独打了分（按目录名排序：negative 在前）",
          [round(c.cer, 2) for c in report.cases], [1.0, 0.0])

    class FakeWake:
        def match(self, text, ratio=None):
            return object() if "凯尔希" in (text or "") else None

    report = asr_eval.run(settings, cases, hearing=Hearing(settings, None, registry=registry),
                          wake=FakeWake())
    summary = report.summary()
    check("★唤醒命中率 100%★", summary["wake_hit"], 1.0)
    check("★误唤醒率 100%（负样本里也出现了唤醒词）★", summary["false_wake"], 1.0)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vl-asr-"))
    print("=" * 78)
    print(" ASR 解耦自测：注册表 / 策略 / 两级加载 / 词库 / KWS / 评测")
    print("=" * 78)
    try:
        for index, fn in enumerate((test_registry, test_default_spec, test_effective,
                                    test_chain, test_policy, test_heavy, test_lexicon,
                                    test_kws, test_eval), start=1):
            room = tmp / f"case{index}"
            room.mkdir(parents=True, exist_ok=True)
            fn(room)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f"× {len(_failures)} 项没通过：")
        for name in _failures:
            print(f"  · {name}")
        return 1
    print("√ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
