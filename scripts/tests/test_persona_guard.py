"""验证「出戏兜底重答」这条分支真的能跑通（不需要真模型）。

用 test_route 里那套 FakeLLM / make_loop：先吐一句拒绝，再吐一句正常话，
看 pipeline 会不会丢掉第一段、换极简人设重答，并且只念第二段。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from test_route import make_loop  # noqa: E402

from voice_loop.persona import Character, render_system_prompt  # noqa: E402


class TwoShotLLM:
    """第一次吐「拒绝扮演」，第二次吐正常回答（并记下每次收到的 system_override）。"""

    def __init__(self) -> None:
        self.turns = 0
        self.overrides: list[object] = []
        self.history: list[tuple[str, str]] = []
        self.system_prompt = ""          # pipeline 会读它来判断「这一轮有没有人设」

    def chat_events(self, user_text="", images=None, model=None, num_ctx=None, tools=None,
                    extra_messages=None, messages=None, prefix_messages=None,
                    system_override=None):
        self.turns += 1
        self.overrides.append(system_override)
        text = ("作为一个人工智能助手，我无法扮演明日方舟中的角色。"
                if self.turns == 1 else "我在。博士，请说。")
        for ch in text:
            yield {"delta": ch}

    def commit(self, user_text: str, answer: str) -> None:
        self.history.append((user_text, answer))

    def reset(self) -> None:
        self.history.clear()

    def release(self, model=None) -> bool:
        return True

    def is_loaded(self, model=None):
        return True

    def ensure_model(self) -> None:
        return None

    def warmup(self, model=None) -> float:
        return 0.0


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'√' if ok else '×'} {name}" + (f"   {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with tempfile.TemporaryDirectory(prefix="guard_") as td:
        loop = make_loop(Path(td))
        char = Character.from_dict({"id": "kaltsit", "name": "凯尔希",
                                    "title": "罗德岛医疗主管", "user_title": "博士",
                                    "style": ["冷静、克制"], "lines": []})
        loop.character = char
        fake = TwoShotLLM()
        fake.system_prompt = render_system_prompt(char)
        loop.llm = fake
        spoken: list[str] = []
        loop.tts_enabled = True
        loop._speak_chunk = lambda s, _n=None: (spoken.append(s), True)[1]  # type: ignore[method-assign]

        answer = loop.respond("你好")
        check("重答了一次（共 2 轮生成）", fake.turns == 2, f"turns={fake.turns}")
        check("第二遍换了极简人设", fake.overrides[-1] is not None, str(fake.overrides[-1])[:40])
        check("第一遍没带 override", fake.overrides[0] is None)
        check("拒绝的话没被念出来", not any("人工智能" in s for s in spoken), str(spoken))
        check("念出来的是第二段", any("我在" in s for s in spoken), str(spoken))
        print(f"  最终回答：{answer!r}")

    print()
    print("全部通过 √" if not fails else f"{len(fails)} 项未通过：{fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
