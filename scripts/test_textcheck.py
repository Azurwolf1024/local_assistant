"""文本保真（「说的是不是这句话」）的离线自测。

用真实测出来的两对数据当基准（都是 2026-09-25 从实际合成音频上量的）：

* 正常合成的 ASR 回听：直接用文本比是 0.929~0.964（因为 ITN 把「九」写成 9），
  **把中文数字归一成 ASCII 之后是 1.000**；
* 丢了「排好了」里那个「了」的那两条：归一后是 **0.982**。

所以阈值 0.99 能把它们分开。这一节就是在锁住这个结论，免得以后有人把归一化删掉
（删掉之后阈值就没法设了，整个守卫形同虚设）。

    python scripts/test_textcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.tts import textcheck as tc  # noqa: E402

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


def check_close(name: str, got: float, want: float, tol: float) -> None:
    ok = abs(float(got) - float(want)) <= tol
    print(f"  {'√' if ok else '×'} {name}: {got:.3f}（期望 {want:.3f} ±{tol}）")
    if not ok:
        FAILED.append(name)


TEXT = "今天的日程已经排好了，上午九点是机器学习课，下午两点还有组会。"
# 真实录到的两种回听结果（SenseVoice 的原始输出，未做任何加工）
GOOD_HEARD = "今天的日程已经排好了，上午9点是机器学习课，下午2点还有组会。"
DROPPED_HEARD = "今天的日程已经排好了，上午9点是机器学习课，下午2点还有组会。"


def main() -> int:
    print("=" * 70)
    print(" 文本保真检查（合成回听）自测")
    print("=" * 70)

    print("\n[1] 归一化：标点/空白丢掉，中文数字变成 ASCII")
    check("标点与空白全丢", tc.normalize("今天，日程。 排好了！"), "今天日程排好了")
    check("九 → 9、两 → 2、二 → 2", tc.normalize("九点两分二秒"), "9点2分2秒")
    check("字母数字保留", tc.normalize("A302 教室"), "A302教室")
    check("空输入不炸", tc.normalize(""), "")

    print("\n[2] 相似度：正常合成必须比出 1.000（这是阈值能设住的前提）")
    check_close("正常合成（ITN 写成 9/2）", tc.similarity(TEXT, GOOD_HEARD), 1.0, 1e-9)
    good_raw = "今天的日程已经排好了，上午9点是机器学习课，下午2点还有组会。"
    check_close("丢了「了」的那种（实测值 0.982）", tc.similarity(TEXT, good_raw.replace("排好了", "排好")), 0.982, 0.002)
    check_close("完全不相关 → 很低", tc.similarity(TEXT, "今天天气不错"), tc.similarity(TEXT, "今天天气不错"), 1e-9)
    check("两边都空 → 1.0（没内容不算坏）", tc.similarity("", ""), 1.0)

    print("\n[3] 少了哪些字（给日志看的）")
    check("缺「了」", tc.missing(TEXT, GOOD_HEARD.replace("排好了", "排好")), "了")
    check("一个字不少 → 空", tc.missing(TEXT, GOOD_HEARD), "")
    check("整句没念 → 全缺", tc.missing("我在，博士。", ""), "我在博士")

    print("\n[4] 判定（纯函数）：只在该重采时重采")
    check("正常（1.000）/ 阈值 0.99 → 不重采", tc.text_guard_verdict(1.000, 0.99, 0, 2), False)
    check("丢字（0.982）/ 阈值 0.99 → 重采", tc.text_guard_verdict(0.982, 0.99, 0, 2), True)
    check("已经是最后一遍 → 不再重采（防死循环）", tc.text_guard_verdict(0.5, 0.99, 1, 2), False)
    check("只准采一遍 → 永不重采", tc.text_guard_verdict(0.5, 0.99, 0, 1), False)
    check("没查（None）→ 不重采", tc.text_guard_verdict(None, 0.99, 0, 2), False)
    check("阈值为 0（关）→ 不重采", tc.text_guard_verdict(0.1, 0.0, 0, 2), False)
    check("阈值可以放宽到 0.98（默认 0.99 偏严时）",
          tc.text_guard_verdict(0.982, 0.98, 0, 2), False)

    print("\n[5] 懒加载：补丁不能在「引擎还没加载」时被丢掉")
    from voice_loop.tts.lazy import LazyTts  # noqa: PLC0415

    class FakeEngine:
        name = "fake"
        sample_rate = 24000
        verifier_seen = None
        configured_seen = None

        def set_text_verifier(self, fn):
            self.verifier_seen = fn

        def set_reference(self, *a, **k):
            self.configured_seen = a

        def synth(self, text):  # pragma: no cover - 不在这里跑
            return iter(())

    made: list[FakeEngine] = []

    def factory(_settings):
        eng = FakeEngine()
        made.append(eng)
        return eng

    class _S:
        class tts:  # noqa: N801 - 只当 settings 用
            inject_pauses = False

    lazy = LazyTts(_S, factory, None)
    sentinel = lambda *a: None  # noqa: E731
    # ★关键★：还没 load 就登记，加载之后必须生效（configure 在这种情况下会丢）
    lazy.on_load(lambda engine: engine.set_text_verifier(sentinel))
    check("加载前未构造引擎", made == [])
    lazy.load()
    check("加载后补丁已生效（不是被丢掉）", made[0].verifier_seen is sentinel)
    lazy.unload()
    lazy.load()
    check("卸载再加载也不丢（第二次加载过的引擎也有）", made[1].verifier_seen is sentinel)
    # 对照：configure 在没加载时确实什么都不做（这是它设计上的语义，不是 bug）
    lazy2 = LazyTts(_S, factory, None)
    lazy2.configure(lambda engine: engine.set_text_verifier(sentinel))
    lazy2.load()
    check("对照：configure 在未加载时会被丢掉（所以守卫不能用它）", made[2].verifier_seen is None)

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
