"""解析「名字 + 文本」清单文件。

实测素材（``data/personas/kaltsit/kaltsit.txt``）长这样——名字行**紧接**正文行，
之后才是空行：

    任命助理
    博士，请坐。别紧张，我只是来查看你的身体状况。……

    交谈1
    我会定期为你进行理学检查，记录你的生命征象与意识状态……

也支持名字和正文之间隔一个空行的写法，以及一行搞定的
``名字<TAB>正文``、``名字：正文``。

两处用它：
- ``voice_loop/tts/zipvoice_tts.py``：找参考音频对应的文本（只认目录里真有的音频）
- ``scripts/import_lines.py``：把清单导进角色文件的 ``lines``
"""

from __future__ import annotations

import re
from pathlib import Path

# 超过这个长度就不当「名字」看（宁可当成正文，也不能把正文误当名字）
NAME_MAX_CHARS = 24
# 名字行不该以句子结束标点收尾
_END_PUNCT = "。！？!?…；;，,、"
# 行内分隔符（名字 + 正文写在一行时）
_INLINE_SEP = re.compile(r"^(.{1,%d}?)\s*(?:\t|：|:|\|)\s*(.+)$" % NAME_MAX_CHARS)

# 带 mtime 的缓存：{(目录,): ((文件, mtime, size)..., {名字: 文本})}
_CACHE: dict[Path, tuple[tuple, dict[str, str]]] = {}


def _flatten(lines: list[str]) -> str:
    """把块里的多行拼成一段（中文之间不留空格，英文单词之间留一个）。"""
    text = " ".join(x.strip() for x in lines if x.strip())
    return re.sub(
        r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[\u4e00-\u9fff])", "", text
    )


def _blocks(text: str) -> list[list[str]]:
    """按空行切块，块内保留原始行。"""
    out: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _as_name(line: str) -> tuple[str | None, str]:
    """看一行是不是「名字」或「名字+正文」。返回 ``(名字或 None, 同行正文)``。"""
    line = line.strip()
    inline = _INLINE_SEP.match(line)
    if inline:
        return inline.group(1).strip(), inline.group(2).strip()
    if len(line) <= NAME_MAX_CHARS and not line.endswith(tuple(_END_PUNCT)):
        return line, ""
    return None, ""


def parse_manifest(text: str, plain: bool = False) -> list[tuple[str, str]]:
    """解析清单，返回 ``[(名字, 文本)]``。

    ``plain=True`` 时整份文件都当正文（每段一条，名字留空），不会把短行误当名字——
    适合「就是一堆段落」的文本。
    """
    lines = text.splitlines()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if not plain:
            name, inline = _as_name(lines[i])
            if inline:  # 「名字<TAB>正文」这种一行搞定的
                out.append((name or "", inline))
                i += 1
                continue
            if name is not None:
                j = i + 1
                while j < len(lines) and not lines[j].strip():  # 空行可有可无，两种排版都吃
                    j += 1
                body: list[str] = []
                while j < len(lines) and lines[j].strip():
                    body.append(lines[j])
                    j += 1
                if body:
                    out.append((name, _flatten(body)))
                    i = j
                    continue
                i += 1
                continue
        # 不是名字 → 一段（到空行为止）当正文
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i])
            i += 1
        text_body = _flatten(body)
        if text_body:
            out.append(("", text_body))
    return out


def parse_manifest_file(path: Path, plain: bool = False) -> list[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_manifest(text, plain=plain)


def manifest_pairs(directory: Path) -> dict[str, str]:
    """把目录下所有 ``*.txt`` 当清单读进来，合并成 ``{名字(小写): 文本}``（先到先得）。

    带 mtime 缓存：文件没动就不重复解析。**不**做「必须是音频文件名」这类过滤，
    调用方自己按需筛（TTS 那边只认目录里真有的音频）。
    """
    files = sorted(p for p in directory.glob("*.txt") if p.is_file())
    if not files:
        return {}
    try:
        stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    except OSError:
        return {}
    cached = _CACHE.get(directory)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    found: dict[str, str] = {}
    for path in files:
        for name, body in parse_manifest_file(path):
            if name and body:
                found.setdefault(name.lower(), body)
    _CACHE[directory] = (stamp, found)
    return found
