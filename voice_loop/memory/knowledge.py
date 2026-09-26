"""L4 知识库：把「世界观设定 / 资料 / 自己的笔记」接进来。

## 接口长什么样

一个 `Provider` 只要能「列出所有片段」和「按 token 找」就够了：

```python
class Provider(Protocol):
    name: str
    def chunks(self) -> list[Chunk]: ...
    def available(self) -> bool: ...
```

默认给两个实现：
  - `LocalFilesProvider`：扫若干个目录/文件（`.md` / `.txt` / `.json`）→ 切段；
  - `InlineProvider`：直接塞一段文字（配置里写世界观设定、或运行时从别处抓来的一段）。

想接**外界**（公司 wiki、网页、另一个 MCP 服务器…）：写一个子类，把结果装进 `Chunk` 返回即可 ——
检索层不关心它是从哪来的（`Chunk.source` 记来源，方便回答「这是从哪知道的」）。

★为什么不上向量库★：这台机器没有 GPU、项目也没有 embedding 依赖，
而「世界观/笔记」这类知识量本来就不大（几百段），**关键词重合 + 标题加权**已经够用，
上向量库只会多一份要维护的索引。真需要时，换掉 `retrieve.score_chunk` 那一个函数就行。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .levels import keywords_of, overlap, tokenize
from .model import Chunk, now_iso

TEXT_EXTS = (".md", ".markdown", ".txt", ".rst")
MAX_CHUNK_CHARS = 900          # 一段别太长：检索命中后要整段进提示词
MIN_CHUNK_CHARS = 6            # ★别把「一行设定」当噪声丢掉★（丢了就成了「查不到还不知道为什么」）


class Provider(Protocol):
    """知识来源。实现 `chunks()` 就行（`name` 用来标来源）。"""

    name: str

    def chunks(self) -> list[Chunk]:
        ...

    def available(self) -> bool:
        ...


def _chunk_id(source: str, index: int, text: str) -> str:
    digest = hashlib.sha1(f"{source}:{index}:{text[:80]}".encode("utf-8")).hexdigest()[:8]
    return f"kb-{digest}"


def split_text(text: str, source: str, title: str = "") -> list[Chunk]:
    """把一份文档切成片段：先按 Markdown 标题切，再按空行段落打包到 `MAX_CHUNK_CHARS`。

    ★切段是知识库唯一容易做错的地方★：段太大 → 提示词被一段灌满；
    段太小 → 「世界观」被切得七零八落，检索出来看不懂。所以：
    标题行**跟着它下面那段**（标题是最强的检索信号），段落只在必要时才硬切。
    """
    blocks: list[tuple[str, str]] = []      # (当前标题, 段落)
    current_title = title
    for raw in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        block = raw.strip()
        if not block:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", block)
        if heading and len(block.splitlines()) == 1:
            current_title = heading.group(2).strip()
            continue
        blocks.append((current_title, block))

    chunks: list[Chunk] = []
    buf_title, buf = "", ""
    for head, block in blocks:
        if buf and (len(buf) + len(block) > MAX_CHUNK_CHARS or head != buf_title):
            chunks.append(_make_chunk(buf_title, buf, source, len(chunks)))
            buf = ""
        buf_title = head or buf_title
        buf = f"{buf}\n\n{block}".strip() if buf else block
    if buf:
        chunks.append(_make_chunk(buf_title, buf, source, len(chunks)))
    kept = [c for c in chunks if len(c.text) >= MIN_CHUNK_CHARS]
    if not kept and chunks:
        # ★兜底：有内容就必须有片段★（文档本身很短时，别把唯一那一句也过滤掉）
        chunks[0].text = buf or chunks[0].text
        return [chunks[0]]
    return kept


def _make_chunk(title: str, text: str, source: str, index: int) -> Chunk:
    return Chunk(id=_chunk_id(source, index, text), source=source, title=title or Path(source).name,
                 text=text[:MAX_CHUNK_CHARS],
                 keywords=keywords_of(f"{title} {text}", prefer=title),
                 updated_at=now_iso())


@dataclass
class LocalFilesProvider:
    """扫目录里的文本文件（`.md` / `.txt` / `.json`），带 mtime 缓存。

    ★`skip_subdirs` 是「角色隔离」的关键★：共享知识库只收**顶层文件**，
    子目录按约定属于某个角色（`data/knowledge/<角色id>/`）或一个世界观组
    （`data/knowledge/_worlds/<世界观>/`），否则共享库会把所有人的专属设定一起卷进来 ——
    那就无所谓隔离了。

    ★下划线开头的不当知识★（任何一层都适用）：`_README.md`、`_worlds/` 这种是**结构**，
    不是给模型读的正文。没有这条，一份使用说明会被当成世界观检索出来。
    """

    name: str = "local"
    roots: list[str] = field(default_factory=list)
    skip_subdirs: bool = False          # True = 只收 roots 下的顶层文件（共享库用）
    _cache: dict[str, tuple[float, list[Chunk]]] = field(default_factory=dict, repr=False)

    def available(self) -> bool:
        return any(Path(p).exists() for p in self.roots)

    def _files(self) -> list[Path]:
        out: list[Path] = []
        for raw in self.roots:
            path = Path(raw)
            if path.is_file():
                if not path.name.startswith("_"):
                    out.append(path)
                continue
            if not path.is_dir():
                continue
            for ext in TEXT_EXTS:
                if self.skip_subdirs:
                    found = [p for p in path.glob(f"*{ext}") if p.is_file()]
                else:
                    found = list(path.rglob(f"*{ext}"))
                for got in sorted(found):
                    # ★只看**相对这个根**的路径★：不然 D:\_work\… 这种带下划线的上级目录
                    #   会把整个知识库静默屏蔽掉（排查起来极其费劲）
                    try:
                        rel = got.relative_to(path)
                    except ValueError:
                        rel = Path(got.name)
                    if not any(part.startswith("_") for part in rel.parts):
                        out.append(got)
        return out

    def chunks(self) -> list[Chunk]:
        out: list[Chunk] = []
        for path in self._files():
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(str(path))
            if cached and cached[0] == stamp:
                out.extend(cached[1])
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if path.suffix.lower() == ".json":
                text = json.dumps(json.loads(text or "{}"), ensure_ascii=False, indent=2) \
                    if _looks_json(text) else text
            pieces = split_text(text, source=str(path), title=path.stem)
            self._cache[str(path)] = (stamp, pieces)
            out.extend(pieces)
        return out


def _looks_json(text: str) -> bool:
    head = text.lstrip()[:1]
    return head in "{["


@dataclass
class InlineProvider:
    """直接写死在配置里的一段知识（世界观背景常用这种）。"""

    name: str = "inline"
    title: str = "设定"
    text: str = ""

    def available(self) -> bool:
        return bool(self.text.strip())

    def chunks(self) -> list[Chunk]:
        if not self.available():
            return []
        return split_text(self.text, source=f"<{self.name}>", title=self.title)


@dataclass
class KnowledgeBase:
    """把若干 provider 合起来看；检索时统一打分（见 retrieve.score_chunk）。"""

    providers: list[Provider] = field(default_factory=list)

    def add(self, provider: Provider) -> None:
        self.providers.append(provider)

    def available(self) -> bool:
        return any(getattr(p, "available", lambda: True)() for p in self.providers)

    def chunks(self) -> list[Chunk]:
        out: list[Chunk] = []
        for provider in self.providers:
            try:
                if getattr(provider, "available", lambda: True)():
                    out.extend(provider.chunks())
            except Exception:  # noqa: BLE001 - 一个来源坏了不该影响别的
                continue
        return out

    def search(self, query: str, limit: int = 3) -> list[Chunk]:
        """按关键词粗筛（真正排序在 retrieve 里做，这里给 `/ 知识` 命令用）。"""
        tokens = tokenize(query)
        if not tokens:
            return []
        scored: list[tuple[float, Chunk]] = []
        for chunk in self.chunks():
            score = overlap(tokens, tokenize(f"{chunk.title} {chunk.text}"))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def stats(self) -> dict[str, int]:
        rows = {}
        for provider in self.providers:
            try:
                rows[provider.name] = len(provider.chunks())
            except Exception:  # noqa: BLE001
                rows[provider.name] = 0
        return rows

    def merge(self, other: "KnowledgeBase") -> "KnowledgeBase":
        """并上另一份知识库（★拼接，不是替换★）。

        「共享世界观 + 这个角色专属世界观」就是这么拼出来的：
        各自保留自己的 provider，检索时统一打分，`stats()` 也分得清哪几段是谁的。
        """
        return KnowledgeBase(providers=[*self.providers, *other.providers])


def build_knowledge(paths: Iterable[str] = (), inline: Iterable[tuple[str, str]] = (),
                    name: str = "local", skip_subdirs: bool = False) -> KnowledgeBase:
    """按配置拼一个知识库：`paths` 是文件/目录，`inline` 是 (标题, 正文)。

    `name` 只影响 `stats()` 里的显示（角色专属的会显示成 `local:<角色>`），
    这样一眼能看出「这条知识是谁的」。
    """
    kb = KnowledgeBase()
    roots = [str(p) for p in paths if str(p).strip()]
    if roots:
        kb.add(LocalFilesProvider(name=name, roots=roots, skip_subdirs=skip_subdirs))
    for title, text in inline:
        if str(text).strip():
            kb.add(InlineProvider(name=name, title=str(title or "设定"), text=str(text)))
    return kb
