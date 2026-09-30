"""「风格」这一层：把人格里的 `voice_refs` 和回答里的情绪标签接起来。

★为什么要这一层★（2026-09-30，工程日志 §49/§50）：
ZipVoice 的接口里**没有「情绪/语气」这个输入维度** —— 风格只能从**参考音频**里来
（实测：参考音自己只差 1.4 倍，到输出被放大 2.5 倍）。所以当前模型下唯一能做的「情绪控制」
就是**准备几条不同语气的参考，按需要换**。这一层管的就是「换哪一条」：
人格里写 `voice_refs`，运行时按 `style` 键取；取不到就退回 `voice_ref`（老行为）。

风格来自两处（都没有就什么都不换）：
1. **LLM 标签**（默认关）：回答开头写 `<style=calm>`，念之前先剥掉；
2. **显式设置**（控制台/命令行/代码 `pipeline.set_style("calm")`）。

★不编造风格名★：键名是用户自己定的（`calm`/`催促`/`happy` 都行），
本模块只负责「取得到就换、取不到就退回」，不猜语义。
"""

from __future__ import annotations

import re
from typing import Any

# 标签格式：`<style=calm>` / `[style=calm]`（大小写不敏感；键名允许中文）
TAG_RE = re.compile(r"^\s*[<\[]\s*style\s*=\s*([^>\]]{1,24}?)\s*[>\]]\s*", re.IGNORECASE)
# 只认开头 24 个字符里出现的标签（防着正文里恰好写了「<style=…>」这种说明文字）
TAG_WINDOW = 24


def parse_tag(text: str) -> tuple[str, str]:
    """从开头读情绪标签。返回 ``(键, 去掉标签的正文)``；没有标签就 ``("", 原文)``。"""
    got = TAG_RE.match(str(text or ""))
    if not got:
        return "", str(text or "")
    return got.group(1).strip(), str(text or "")[got.end():]


def keys(char: Any) -> list[str]:
    """这个角色有哪些风格键（人格 `voice_refs` 的键，按写进去的顺序）。"""
    refs = getattr(char, "voice_refs", None)
    return [str(k) for k in refs] if isinstance(refs, dict) else []


def pick_ref(char: Any, style: str = "") -> tuple[str, str]:
    """按风格取 ``(参考音频, 参考文本)``。没有这一档就退回 `voice_ref`（老行为）。

    `voice_refs` 的两种写法都吃：
    - ``"calm": "data/personas/x/a.wav"``  → 文本留空（由清单/自动转写去找）；
    - ``"calm": {"ref": "...", "text": "..."}`` → 文本一起带上。
    """
    refs = getattr(char, "voice_refs", None)
    key = str(style or "").strip()
    if key and isinstance(refs, dict):
        got = refs.get(key)
        if isinstance(got, dict):
            ref = str(got.get("ref") or got.get("voice_ref") or "").strip()
            if ref:
                return ref, str(got.get("text") or got.get("voice_ref_text") or "").strip()
        elif got is not None and str(got).strip():
            return str(got).strip(), ""
    # 退回默认那一档（原来的 voice_ref / voice_ref_text）
    return (str(getattr(char, "voice_ref", "") or "").strip(),
            str(getattr(char, "voice_ref_text", "") or "").strip())


def hint(char: Any) -> str:
    """给 LLM 的「可用风格」提示（只在开着标签、且角色真有多档时才加）。

    ★说得越省越好★：4b 模型对长指令不敏感，只给它键名和格式。
    """
    got = keys(char)
    if not got:
        return ""
    return ("回答最前面可以加一个情绪标签，例如 <style=" + got[0] + ">，只能用这些："
            + "、".join(got) + "。不加就按默认语气说。标签本身不要念出来。")


class StyleStripper:
    """边收边剥「开头那个情绪标签」的小状态机（流式用）。

    为什么要有它：LLM 是**一次一个 token** 吐出来的，标签也会被切开
    （`<sty` / `le=ca` / `lm>`）。所以第一个标签没定下来之前先攒着；
    一确定「不是标签」就立刻原样放行 —— 不能为了剥标签把开头几个字压住不发。
    """

    def __init__(self) -> None:
        self.style = ""
        self._buf = ""
        self._done = False

    def feed(self, delta: str) -> str:
        """喂一段新文本，返回「现在可以放出去」的部分（可能为空）。"""
        if self._done or not delta:
            return delta or ""
        self._buf += delta
        got = TAG_RE.match(self._buf)
        if got:                                   # 整个标签到齐了
            self.style = got.group(1).strip()
            rest = self._buf[got.end():]
            self._buf = ""
            self._done = True
            return rest
        if self._could_be_tag(self._buf) and len(self._buf.lstrip()) <= TAG_WINDOW:
            return ""                             # 还可能是个标签：再等一个 token
        self._done = True
        out, self._buf = self._buf, ""
        return out

    @staticmethod
    def _could_be_tag(text: str) -> bool:
        """开头还可能是 `[<]style=…` 吗？（真标签才等，普通正文一个字不压）"""
        m = re.match(r"^\s*[<\[]\s*([A-Za-z]*)", text)
        if m is None:
            return False
        word = m.group(1).lower()
        if word:
            return "style".startswith(word)
        # 只敲了 `<`/`[`：看紧跟的那个字符（不是字母就不是标签，比如「<3 米」）
        rest = re.sub(r"^\s*[<\[]\s*", "", text)
        return (not rest) or rest[0].isalpha()

    def finish(self) -> str:
        """流结束时把还攒着的吐出来（不然短回答会被吃掉）。"""
        out, self._buf = self._buf, ""
        self._done = True
        return out
