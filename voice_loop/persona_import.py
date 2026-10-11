"""角色素材导入：把「角色文件夹里那几个 txt」变成人格文件与知识库的一部分。

两种素材，两条去向 —— **都不依赖固定模板**：

========================================  ==========================================
``<角色>.txt``（台词清单：名字 + 正文）      人格文件的 ``lines``（示例台词）
``profile.txt``（角色档案 / 角色卡 / 设定）   知识库 ``data/knowledge/<id>/档案.md``
========================================  ==========================================

★为什么不做成「按明日方舟的档案模板解析」★

    用户的原话：以后的角色**可能不是同一个游戏，不一定有固定模板**。所以这里只认
    **结构**，不认字段名（规则见 :func:`parse_profile`），并且：

    - ``【键】值`` 一律抽成元信息（代号/身高/出身地…），不硬塞进正文；
    - 「提升信赖至50%以查看」这类**解锁门槛行**按通用形状丢掉（``--keep-gates`` 可留）；
    - 章节标题只要求「短、独占一行、后面有正文」，不要求叫「档案资料一」；
    - 一页**纯散文**也能进来（整篇当一个章节），不会因为「没模板」而报错。

    新游戏、新模板、手写的角色卡、甚至另一套语言的门槛行，都能进来。

三条不变量（与 ``scripts/import_lines.py`` 一致）：

    1) ★只添加，不替换★：目标文件里已有的内容一个字都不动，只追加没有的；
    2) 默认**试运行**，``--apply`` 才写；写之前备份 ``.bak``；
    3) **幂等**：同一份素材导两次，第二次什么都不做（去重按「去掉空白后的正文」比）。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import parse_manifest

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
KNOWLEDGE_DIR = "data/knowledge"
WORLDS_DIR = "_worlds"
DEFAULT_PROFILE_FILE = "档案.md"
"""档案导进知识库后的文件名（一个主题一个文件，见 data/knowledge/_README.md）。"""

PROFILE_STEMS = ("profile", "档案", "角色档案", "干员档案", "资料", "设定", "角色卡",
                 "setting", "settings", "about", "bio", "card", "desc")
"""文件名像「档案」的词。★只是**加分项**，不是判据★（判据是内容形状，见 classify）。"""

HEADING_MAX = 24
"""超过这个长度就不当标题（宁可当正文，也不能把一段话拆成标题）。

★带 markdown ``#`` 标记的行不受这个限制★：标记本身就是「我这是标题」的声明，
英文标题（``Neon Wanderer — Character Sheet``）比中文长得多。
"""

PROSE_MIN = 120
"""纯散文没有结构可依，只能按长度判断：到这长度就当是一份设定（手写的角色卡常见）。"""

META_KEY_MAX = 10
"""``键：值`` 里「键」的最大长度（再长就当成正文里的冒号）。"""

_PROFILE_ANSWER = ("profile", "archive")
_LINES_ANSWER = ("lines", "voice", "manifest")

_HEADING_MARKS = re.compile(
    r"^(?:#{1,6}\s*"                      # markdown 标题
    r"|[-*•·●○◆■□▎]\s*"                  # 各种项目符号
    r"|\(?\d{1,2}\s*[).、．]\s*"           # 1. / 2) / 3、
    r"|（?[一二三四五六七八九十]{1,2}）?\s*[、.．]\s*"  # 一、/ （二）
    r")"
)

# 「解锁门槛」这类**不是内容**的行：通用形状，不绑定某个游戏
_GATE_PATTERNS = (
    re.compile(r"^.{0,16}以(?:查看|解锁|阅读|开启|开放)$"),
    re.compile(r"^(?:初始|默认|首次|全部)(?:开放|解锁|可用|可查看)$"),
    re.compile(r"^(?:精英化?|晋升|信赖|羁绊|专精|潜能|技能|研发|满级)\s*\d*\s*(?:级)?\s*(?:解锁|开放|可查看|以查看)?$"),
    re.compile(r"^(?:locked|unlocked|available|unlock at).*$", re.IGNORECASE),
)
_SENTENCE_END = "。！？!?…；;，,、：:"
_QUOTE_START = "“\"'「『(（"
"""以这些开头的行是**对话/引文**，不当标题 —— 否则一段访谈会被拆成一问一答两节，
读起来上下文就断了（检索命中「问」时也拿不到「答」）。"""


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    """角色文件夹里发现的一份素材。"""

    path: Path
    kind: str = "lines"
    """``lines``（台词清单）或 ``profile``（档案）。"""
    reason: str = ""
    chars: int = 0
    entries: int = 0
    """台词条数 / 档案章节数。"""

    def rel(self, root: Path) -> str:
        try:
            return self.path.relative_to(root).as_posix()
        except ValueError:
            return str(self.path)


@dataclass
class Section:
    title: str
    body: str = ""
    meta: list[tuple[str, str]] = field(default_factory=list)
    """★这一节自己的** ``【键】值`` 表★（如「基础档案」里的代号/身高、试炼表里的六项）。"""


@dataclass
class ProfileDoc:
    """一份档案解析出来的结构（与具体游戏无关）。"""

    title: str = ""
    meta: list[tuple[str, str]] = field(default_factory=list)
    """全部元信息（按出现顺序去重）——给「生成骨架」用，渲染时按节分开。"""
    sections: list[Section] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    """被当成「解锁门槛」丢掉的原文（报告里列出来，方便 --keep-gates 抢救）。"""
    source: str = ""
    inline_meta: bool = False
    """这份档案是不是「键：值」占主导（决定要不要把冒号行当元信息）。"""

    @property
    def chars(self) -> int:
        return sum(len(s.title) + len(s.body) + 1 for s in self.sections)


@dataclass
class MergeResult:
    added: list = field(default_factory=list)
    dup: list[str] = field(default_factory=list)


@dataclass
class CharPlan:
    """一个角色「准备做什么」。scan 出来给人和界面看，apply 时照着做。"""

    char_id: str
    char_dir: Path
    json_path: Path | None = None
    json_exists: bool = False
    sources: list[Source] = field(default_factory=list)
    in_index: bool = False
    name: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.sources) or bool(self.problems)


# --------------------------------------------------------------------------- #
# 档案解析（★本节是「适配不同模板」的核心★）
# --------------------------------------------------------------------------- #
def _strip_marks(line: str) -> str:
    text = line.strip()
    for _ in range(3):                       # ```## 一、 这种叠着来的也剥掉
        new = _HEADING_MARKS.sub("", text, count=1).strip()
        if new == text:
            break
        text = new
    return text.strip().strip("*_`").strip()


def is_gate(line: str) -> bool:
    """是不是「解锁门槛」那类行（不是内容，默认丢掉）。"""
    text = line.strip().strip("*_`").strip("（）()").strip()
    if not text or len(text) > 32:
        return False
    return any(pattern.match(text) for pattern in _GATE_PATTERNS)


def _blocks(text: str) -> list[list[str]]:
    """按空行切块（块内保留原始行）。"""
    out: list[list[str]] = []
    cur: list[str] = []
    for raw in text.replace("\r\n", "\n").splitlines():
        if raw.strip():
            cur.append(raw.rstrip())
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _meta_line(line: str) -> tuple[str, str] | None:
    """``【代号】克莱门莎`` / ``身高：168cm`` → ``("代号", "克莱门莎")``。"""
    text = _strip_marks(line)
    got = re.match(r"^【([^】]{1,16})】\s*(.*)$", text)
    if got:
        key, value = got.group(1).strip(), got.group(2).strip()
        return (key, value) if value else None        # 没值的方括号是「小标题」
    got = re.match(r"^([^：:【】\s]{1,%d})[：:]\s*(.+)$" % META_KEY_MAX, text)
    if got:
        return got.group(1).strip(), got.group(2).strip()
    return None


def _bracket_alone(line: str) -> str:
    """``【一份投诉信，来自克莱门莎的医师】`` → 里面的字（没有值的那种）。"""
    text = _strip_marks(line)
    got = re.match(r"^【([^】]{1,40})】$", text)
    return got.group(1).strip() if got else ""


def _is_heading(line: str, block: list[str], index: int) -> bool:
    """这一行是不是标题：短、不像句子、且后面确实还有内容。"""
    raw = line.strip()
    if not raw:
        return False
    if raw[0] in _QUOTE_START:
        return False
    marked = bool(_HEADING_MARKS.match(raw))
    if not marked and len(raw) > HEADING_MAX:
        return False
    text = _strip_marks(raw)
    if not text or (not marked and len(text) > HEADING_MAX):
        return False
    if text.endswith(tuple(_SENTENCE_END)) and not marked:
        return False
    if "：" in text or ":" in text:                  # 「甲：乙」这种是正文
        return False
    if marked:
        return True
    # 没带标记：要求它「独占一行的短句」或者「后面还有更长的正文」
    rest = [x for x in block[index + 1:] if x.strip()]
    if not rest:
        return len(block) == 1                       # 整块就这一行 → 当标题
    return len(text) <= max(6, len(_strip_marks(rest[0])) // 2)


def parse_profile(text: str, *, keep_gates: bool = False, source: str = "") -> ProfileDoc:
    """把一份「档案」切成若干章节（每节自带元信息表）。★只认结构，不认字段名★

    四条规则（换个游戏、换套模板、手写的角色卡都吃）：

    1. **短行 + 后面还有正文** = 标题（``## 档案资料一``、``一、基础档案``、
       ``【一份投诉信】`` 都算）；
    2. ``【键】值`` / ``键：值`` 出现在**某一节的开头**（还没写正文时）= 这一节的
       元信息表；一旦这一节开始写正文，后面的方括号行就原样留在正文里
       （「临床诊断分析」那种把两个数值夹在句子里写的排版不会被拆散）；
    3. 解锁门槛行（``提升信赖至50%以查看`` / ``初始开放``）默认丢掉，
       ``keep_gates=True`` 就留着；
    4. 其余是正文，按空行分段落**原样保留**（剧本体那种一行一句的排版不动它）。

    ★节与节之间不许串味★：正文缓冲区在遇到新标题时必须先结算到底，
    否则「第一段的字跑到最后一段去」这种错会很难看出来（第一版就是这么错的）。
    """
    doc = ProfileDoc(source=source)
    blocks = _blocks(text or "")

    # 整份文件第一行的 markdown 一级标题 = 文档标题（不占一节）
    if blocks and len(blocks[0]) == 1 and re.match(r"^#\s+\S", blocks[0][0].strip()):
        doc.title = _strip_marks(blocks[0][0])
        blocks = blocks[1:]

    # 先扫一遍全量，决定「冒号行算不算元信息」——避免把正文里偶发的一个冒号当字段
    keyed = 0
    for block in blocks:
        for line in block:
            got = _meta_line(line)
            if got and len(got[1]) <= 60:
                keyed += 1
    doc.inline_meta = keyed >= 2                      # 至少两处才认为「这是元信息体例」

    current = Section(title="")
    sections: list[Section] = []
    pending: list[str] = []                           # 段落（不是行）：跨块用空行分开

    def push_para(lines: list[str]) -> None:
        text = "\n".join(x for x in lines if x.strip()).strip()
        lines.clear()
        if text:
            pending.append(text)

    def settle_body() -> None:
        """把挂着的段落并入当前节（★切节/换标题前必须调★，不然会串味）。"""
        if not pending:
            return
        body = "\n\n".join(pending)
        pending.clear()
        current.body = f"{current.body}\n\n{body}".strip() if current.body else body

    def close_section() -> None:
        nonlocal current
        settle_body()
        if current.title or current.body or current.meta:
            sections.append(current)
        current = Section(title="")

    def is_shell() -> bool:
        """当前这节还**只有标题**吗？

        注意把 ``pending`` 也算进去：正文是先挂在 pending 里的，
        不算它的话「上一节的正文还没结算」就会被当成空节，
        结果标题被覆盖、那段正文跟进下一节（第一版就错在这里）。
        """
        return bool(current.title) and not current.body and not current.meta and not pending

    for block in blocks:
        body_lines: list[str] = []
        for index, raw in enumerate(block):
            line = raw.strip()
            if not line:
                continue
            if is_gate(line):
                if keep_gates:
                    body_lines.append(line)
                else:
                    doc.dropped.append(line)
                continue
            alone = _bracket_alone(line)
            if alone:
                # 【…】 单独一行 = 它引出下面那段材料 → 让它自己成一节（标题就是检索信号）
                push_para(body_lines)
                if is_shell():
                    current.title = alone            # 空壳节：换标题，别堆空节
                else:
                    close_section()
                    current.title = alone
                continue
            fresh = not current.body and not pending and not body_lines
            got = _meta_line(line) if fresh else None
            if got and doc.inline_meta:
                key, value = got
                if (key, value) not in current.meta:
                    current.meta.append((key, value))
                if (key, value) not in doc.meta:
                    doc.meta.append((key, value))
                continue
            if _is_heading(raw, block, index):
                push_para(body_lines)
                title = _strip_marks(raw)
                if is_shell():
                    current.title = title            # 空壳节：直接换标题
                else:
                    close_section()
                    current.title = title
                continue
            body_lines.append(line)
        push_para(body_lines)

    close_section()
    doc.sections = sections
    return doc


def profile_markdown(doc: ProfileDoc, *, title: str, source_rel: str = "",
                     sources: bool = True) -> str:
    """把解析结果写成知识库要的 Markdown（标题 = 检索时最强的信号）。"""
    lines: list[str] = [f"# {title}", ""]
    if sources and source_rel:
        lines += [f"> 来源：`{source_rel}`（由 `scripts/import_persona.py` 生成，可重复导入）", ""]
    for section in doc.sections:
        lines.append(f"## {section.title or title}")
        lines.append("")
        if section.meta:
            lines += [f"- **{k}**：{v}" for k, v in section.meta]
            lines.append("")
        if section.body:
            lines.append(section.body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# 素材识别
# --------------------------------------------------------------------------- #
def _wav_stems(directory: Path) -> set[str]:
    return {p.stem.lower() for p in directory.glob("*.wav")}


def _looks_like_running_labels(text: str) -> bool:
    """整篇是不是「一长串**短标签 + 一段正文**」？★这是台词清单的形状★

    清单（``任命助理`` / ``交谈1`` / ``作战中1``…）与档案的区别不在「有没有标题」，
    而在**每一块的形状都一样**：标签很短、不带标点、不是方括号引用、不是 `键：值`，
    后面跟的是一段正文。档案通常不是这样（有长段落、有【】引用、有 1 行或 10 行的块）。

    ★没有音频时只能靠它★：``data/personas/<id>/<id>.txt`` 里的音频还没下、或者
    刚建目录只有文本时，光看「有标题」会把清单误判成档案（沙盒里就是这么发现的）。
    """
    blocks = _blocks(text)
    if len(blocks) < 2:
        return False
    for block in blocks:
        if len(block) != 2:
            return False
        name = block[0].strip()
        if not name or len(name) > 12 or name.endswith(tuple(_SENTENCE_END)):
            return False
        if (_HEADING_MARKS.match(name) or _bracket_alone(name) or _meta_line(name)
                or is_gate(name)):
            return False
    return True


def classify(path: Path, text: str, *, wav_stems: set[str] | None = None,
             assume: str = "") -> tuple[str, str]:
    """这份 txt 是「台词清单」还是「档案」，还是「压根不是素材」？返回 ``(kind, 理由)``。

    ★判据是内容形状与命名约定，不是某个游戏的模板★，按顺序：

    1. 文件名和目录里某个音频同名 → ``ignore``（那是**那段音频的逐字文本**，
       比如 ``ref_join_B.txt`` 配 ``ref_join_B.wav``；零样本克隆要用它，
       但它不是给人格文件/知识库的素材）；
    2. 大多数名字能对上目录里的音频 → **台词清单**（压倒性证据）；
    3. 文件名和**角色目录同名**（``clementia/clementia.txt``）→ 台词清单（命名约定）；
    4. 整篇是「短标签 + 一段正文」的长串 → 台词清单（没音频时的形状判据）；
    5. 其余看形状：够像档案才算档案，否则 ``ignore``。
    """
    if assume in _PROFILE_ANSWER:
        return "profile", "按你的指定（--as profile）"
    if assume in _LINES_ANSWER:
        return "lines", "按你的指定（--as lines）"

    stem = path.stem.lower()
    stems = wav_stems if wav_stems is not None else _wav_stems(path.parent)
    if stem in {s.lower() for s in stems}:
        return "ignore", f"和 {path.stem}.wav 同名：这是那段音频的逐字文本，不是素材"

    name_hint = stem in PROFILE_STEMS or stem.startswith(tuple(s + "_" for s in PROFILE_STEMS))
    folder_hint = path.parent.name.lower() == stem
    pairs = parse_manifest(text)
    named = sum(1 for name, _ in pairs if name)
    hits = sum(1 for name, _ in pairs if name and name.lower() in stems)

    # ★多数名字对得上音频就是台词清单★：门槛不能写成「至少 3 条」——
    # 只有两条台词的原创角色也是真会出现的（自测里那条 2/2 的就曾被误判成档案）。
    if stems and named and (hits * 4 >= named * 3 or (hits >= 3 and hits * 2 >= named)):
        return "lines", f"{named} 条里 {hits} 条名字对得上目录里的音频"
    if folder_hint and not name_hint:
        return "lines", f"文件名和角色目录同名（{path.parent.name}/）——台词清单的约定"
    if _looks_like_running_labels(text) and not name_hint:
        return "lines", f"形状是「短标签 + 一段正文」×{len(_blocks(text))}（台词清单）"

    doc = parse_profile(text)
    shaped = (len(doc.sections) >= 2 or len(doc.meta) >= 2
              or len(text.strip()) >= PROSE_MIN)
    if stems and not hits and not shaped:
        return "ignore", f"既不像台词清单（0 条对上音频）也不像档案（{len(text)} 字、无结构）"
    if stems and not hits:
        return "profile", f"没一条名字对得上音频（{len(stems)} 个），但结构像档案"
    if name_hint and shaped:
        return "profile", f"文件名像档案，且解析出 {len(doc.sections)} 个章节"
    if shaped:
        return "profile", f"结构像档案（{len(doc.sections)} 个章节、{len(doc.meta)} 条元信息）"
    if name_hint:
        return "profile", "文件名像档案"
    if not named:
        return "ignore", "既没有「名字 + 正文」的清单结构，也没有档案的形状"
    return "lines", f"按台词清单读（{named} 条）"


def scan_dir(char_dir: Path, *, assume: str = "") -> list[Source]:
    """扫描一个角色文件夹里的 ``*.txt``（跳过说明文件与「音频的逐字文本」）。"""
    out: list[Source] = []
    stems = _wav_stems(char_dir)
    for path in sorted(char_dir.glob("*.txt")):
        if not path.is_file() or path.name.startswith(("_", ".")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        kind, why = classify(path, text, wav_stems=stems, assume=assume)
        if kind == "ignore":
            continue
        if kind == "profile":
            doc = parse_profile(text, source=path.name)
            out.append(Source(path=path, kind="profile", reason=why,
                              chars=len(text), entries=len(doc.sections)))
        else:
            pairs = [(n, b) for n, b in parse_manifest(text) if b.strip()]
            out.append(Source(path=path, kind="lines", reason=why,
                              chars=len(text), entries=len(pairs)))
    return out


# --------------------------------------------------------------------------- #
# 目录 → 计划
# --------------------------------------------------------------------------- #
def personas_dir(root: Path) -> Path:
    return Path(root) / "data" / "personas"


def index_path(root: Path) -> Path:
    return Path(root) / "data" / "characters.json"


def index_ids(root: Path) -> dict[str, str]:
    """索引里的 ``{id: file}``（读不到就空）。"""
    try:
        raw = json.loads(index_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for entry in (raw.get("characters") or []) if isinstance(raw, dict) else []:
        if isinstance(entry, str):
            out[Path(entry).stem] = entry
        elif isinstance(entry, dict):
            rel = str(entry.get("file") or "")
            out[str(entry.get("id") or Path(rel).stem)] = rel
    return out


def json_for(root: Path, char_id: str) -> Path:
    """人格文件的位置：``personas/<id>.json``（与索引里的约定一致）。"""
    return personas_dir(root) / f"{char_id}.json"


def scan(root: str | Path, *, only: list[str] | tuple[str, ...] = (),
         assume: str = "") -> list[CharPlan]:
    """扫描 ``data/personas/*/``，列出每个角色「发现了什么、能做什么」。

    ★只读★：不写任何文件（``--apply`` 才写）。索引里没有的角色也会列出来，
    只是会提醒「还没在索引里」。
    """
    root = Path(root)
    base = personas_dir(root)
    known = index_ids(root)
    plans: list[CharPlan] = []
    if not base.is_dir():
        return plans
    wanted = {str(x).strip().lower() for x in only if str(x).strip()}
    for char_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        char_id = char_dir.name
        if char_id.startswith(("_", ".")):
            continue                       # _nonvoice 这类：约定不当角色
        if wanted and char_id.lower() not in wanted:
            continue
        target = json_for(root, char_id)
        plan = CharPlan(char_id=char_id, char_dir=char_dir, json_path=target,
                        json_exists=target.is_file(), in_index=char_id in known,
                        sources=scan_dir(char_dir, assume=assume))
        if plan.json_exists:
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                plan.name = str(data.get("name") or "")
            except (OSError, ValueError) as exc:
                plan.problems.append(f"{target.name} 读不了：{exc}")
        elif char_id in known:
            rel = known[char_id]
            other = index_path(root).parent / rel if rel else None
            if other and other.is_file():
                plan.json_path, plan.json_exists = other, True
                try:
                    plan.name = str(json.loads(other.read_text(encoding="utf-8")).get("name") or "")
                except (OSError, ValueError):
                    pass
        if not plan.in_index:
            plan.problems.append("不在 characters.json 索引里（导入了也不会被唤醒）")
        plans.append(plan)
    return plans


def plan_lines_merge(items: list, pairs: list[tuple[str, str]]) -> MergeResult:
    """台词并进 ``lines``：只加没有的（去重按「抹掉空白后的正文」比）。"""
    have = {
        re.sub(r"\s+", "", str(x.get("text", "")) if isinstance(x, dict) else str(x))
        for x in items
    }
    result = MergeResult()
    for scene, body in pairs:
        body = (body or "").strip()
        if not body:
            continue
        key = re.sub(r"\s+", "", body)
        if key in have:
            result.dup.append(scene or body[:12])
            continue
        have.add(key)
        result.added.append({"scene": (scene or "随意对话").strip(), "text": body})
    return result


def prompt_len(data: dict) -> int | None:
    """用角色渲染器量一下提示词长度（量不出来就 None，不影响导入）。"""
    try:
        from .persona import Character, render_system_prompt
    except Exception:  # noqa: BLE001
        return None
    try:
        return len(render_system_prompt(Character.from_dict(data)))
    except Exception:  # noqa: BLE001
        return None


def backup(path: Path) -> Path | None:
    try:
        target = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, target)
        return target
    except OSError:
        return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def skeleton(char_id: str, doc: ProfileDoc | None, *, name: str = "", worlds: list[str] | None = None,
             lines: list | None = None) -> dict:
    """给「还没有人格文件」的角色一个**能跑的骨架**（人再来补风格那几栏）。

    自动填的是**能从素材里确定**的：名字/身份一句话（元信息）、示例台词、世界观。
    ``background`` 用档案里第一段（如果有）——★报告里会提醒你改写成第一人称★。
    """
    meta = dict(doc.meta) if doc else {}
    real_name = name or meta.get("代号") or meta.get("名字") or meta.get("名称") or char_id
    title = ""
    for key in ("职业", "身份", "职位", "种族", "出身地"):
        if meta.get(key):
            title = f"{meta[key]}" + (f"·{meta['职业']}" if key != "职业" and meta.get("职业") else "")
            break
    background = ""
    if doc:
        for section in doc.sections:
            body = section.body.strip().replace("\n", "")
            if 20 <= len(body) <= 260:
                background = body
                break
    return {
        "id": char_id,
        "name": real_name,
        "title": title,
        "background": background,
        "user_title": "你",
        "wake_words": [real_name],
        "aliases": {},
        "ack": "",
        "style": [],
        "rules": [
            "不知道的事情就直说不知道，不要编",
        ],
        "avoid": [],
        "lines": lines or [],
        "worlds": list(worlds or []),
        "knowledge": [],
        "notes": "★骨架★由 scripts/import_persona.py 生成："
                 "style / rules / background 还需要人补（控制台「角色」标签页可以直接改）",
    }


# --------------------------------------------------------------------------- #
# 真的动手：一个角色 → 台词进 lines、档案进知识库
# --------------------------------------------------------------------------- #
@dataclass
class CharResult:
    """一个角色的导入结果（CLI 打印它，界面也渲染它）。"""

    char_id: str = ""
    name: str = ""
    apply: bool = False
    lines_added: int = 0
    lines_dup: int = 0
    prompt_before: int | None = None
    prompt_after: int | None = None
    profile_path: Path | None = None
    profile_sections: int = 0
    profile_meta: int = 0
    profile_changed: bool = False
    dropped: list[str] = field(default_factory=list)
    created_json: bool = False
    index_added: bool = False
    worlds_added: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self, root: Path | None = None) -> dict:
        def rel(p: Path | None) -> str:
            if p is None:
                return ""
            try:
                return p.relative_to(root).as_posix() if root else str(p)
            except ValueError:
                return str(p)

        return {
            "id": self.char_id, "name": self.name, "apply": self.apply,
            "lines_added": self.lines_added, "lines_dup": self.lines_dup,
            "prompt_before": self.prompt_before, "prompt_after": self.prompt_after,
            "profile": rel(self.profile_path),
            "profile_sections": self.profile_sections, "profile_meta": self.profile_meta,
            "profile_changed": self.profile_changed, "dropped": self.dropped,
            "created_json": self.created_json, "index_added": self.index_added,
            "worlds_added": self.worlds_added, "backups": self.backups,
            "problems": self.problems, "notes": self.notes, "ok": self.ok,
        }


def import_char(root: str | Path, plan: CharPlan, *, apply: bool = False,
                lines: bool = True, profile: bool = True, create: bool = False,
                add_index: bool = False, world: str = "", keep_gates: bool = False,
                profile_name: str = "", assume: str = "") -> CharResult:
    """把一个角色文件夹里的素材导进人格文件与知识库。

    ★默认试运行★（``apply=False`` 什么都不写）。写的时候：
    ``lines`` → 追加到人格文件（备份 .bak）；``profile`` → 写成知识库的 Markdown。
    """
    root = Path(root)
    out = CharResult(char_id=plan.char_id, name=plan.name, apply=apply)

    sources = scan_dir(plan.char_dir, assume=assume) if not plan.sources else plan.sources
    by_kind: dict[str, list[Source]] = {"lines": [], "profile": []}
    for src in sources:
        by_kind.setdefault(src.kind, []).append(src)

    target = plan.json_path or json_for(root, plan.char_id)
    data: dict | None = None
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            out.problems.append(f"{target.name} 读不了：{exc}")
            return out
    if data is not None:
        out.name = str(data.get("name") or out.name)

    # ---------------- 档案（先解析出来，骨架要用它填 name/title/background）
    profile_doc: ProfileDoc | None = None
    profile_src = by_kind["profile"][0] if by_kind["profile"] else None
    if profile and profile_src is not None:
        try:
            text = profile_src.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.problems.append(f"{profile_src.path.name} 读不了：{exc}")
            return out
        profile_doc = parse_profile(text, keep_gates=keep_gates, source=profile_src.path.name)
        out.profile_sections = len(profile_doc.sections)
        out.profile_meta = len(profile_doc.meta)
        out.dropped = list(profile_doc.dropped)
        if not profile_doc.sections and not profile_doc.meta:
            out.problems.append("档案里没解析出任何章节或元信息（--keep-gates 再看看）")
            return out

    # ---------------- 台词
    added_lines: list[dict] = []
    if lines and by_kind["lines"]:
        pairs: list[tuple[str, str]] = []
        for src in by_kind["lines"]:
            try:
                body = src.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pairs += [(n, b) for n, b in parse_manifest(body) if b.strip()]
        if data is None:
            items: list = []
        else:
            items = data.get("lines") or []
            if not isinstance(items, list):
                out.problems.append("人格文件里的 lines 不是数组，不敢动它")
                return out
        merged = plan_lines_merge(items, pairs)
        added_lines = merged.added
        out.lines_added = len(added_lines)
        out.lines_dup = len(merged.dup)
        if data is not None:
            before = prompt_len(data)
            after = prompt_len({**data, "lines": items + added_lines})
            out.prompt_before, out.prompt_after = before, after
            # ★必须真的写回 data★：第一版只算了 merged、忘了这一步，
            # 于是报告写着「台词 +38」而文件里仍是 0 条（自测把它钉住了）
            data["lines"] = items + added_lines

    # ---------------- 没有人格文件：要不要生成骨架
    if data is None:
        if not create:
            out.problems.append(
                f"没有 {target.name}：先建人格文件（或加 --create 用素材生成骨架）"
            )
            return out
        data = skeleton(plan.char_id, profile_doc,
                        lines=added_lines, worlds=[world] if world else [])
        data["wake_words"] = [str(data.get("name") or plan.char_id)]
        if apply:
            write_json(target, data)
        out.created_json = True
        out.notes.append(f"生成了骨架 {target.name}：style / rules / background 还需要人补")
        if profile_doc:
            out.notes.append("background 用的是档案里第一段，★记得改写成第一人称的口吻★")
        added_lines = []                       # 已经在骨架里了，别再追加一次
        out.lines_added = len(data.get("lines") or [])
    else:
        out.lines_dup = out.lines_dup
        out.name = str(data.get("name") or out.name)

    # ---------------- 世界观字段（--world）：只加不减
    if world and data is not None:
        have = data.get("worlds") or []
        if not isinstance(have, list):
            have = []
        if world not in have:
            data["worlds"] = [*have, world]
            out.worlds_added = [world]

    # ---------------- 写回人格文件
    if apply and not out.created_json and data is not None:
        changed = bool(added_lines) or bool(out.worlds_added)
        if changed:
            bak = backup(target)
            if bak:
                out.backups.append(bak.name)
            write_json(target, data)

    # ---------------- 写知识库
    if profile_doc is not None:
        if world:
            dest = (root / KNOWLEDGE_DIR / WORLDS_DIR / world
                    / f"{plan.char_id}-{(profile_name or DEFAULT_PROFILE_FILE)}")
        else:
            dest = root / KNOWLEDGE_DIR / plan.char_id / (profile_name or DEFAULT_PROFILE_FILE)
        doc_title = f"{out.name or plan.char_id} · 档案"
        markdown = profile_markdown(
            profile_doc, title=doc_title,
            source_rel=profile_src.rel(root) if profile_src else "")
        old = dest.read_text(encoding="utf-8") if dest.is_file() else None
        out.profile_path = dest
        if old == markdown:
            out.notes.append(f"{dest.name} 没变，跳过（幂等）")
        else:
            out.profile_changed = True
            if apply:
                if old is not None:
                    bak = backup(dest)
                    if bak:
                        out.backups.append(bak.name)
                write_text(dest, markdown)
        if not world:
            out.notes.append("知识库按角色 id 自动可见（不用写 knowledge 字段）")

    # ---------------- 索引
    if add_index and apply and plan.char_id not in index_ids(root):
        path = index_path(root)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError) as exc:
            out.problems.append(f"{path.name} 读不了，没加索引：{exc}")
            raw = None
        if isinstance(raw, dict):
            bak = backup(path) if path.is_file() else None
            if bak:
                out.backups.append(bak.name)
            entries = raw.get("characters")
            if not isinstance(entries, list):
                entries = []
            rel = target.relative_to(index_path(root).parent).as_posix()
            entries.append({"id": plan.char_id, "file": rel, "enabled": True, "default": False})
            raw["characters"] = entries
            write_json(path, raw)
            out.index_added = True
    if not apply and (plan.char_id not in index_ids(root)):
        out.notes.append("还不在 characters.json 里：--add-index 可以顺手加上")

    return out
