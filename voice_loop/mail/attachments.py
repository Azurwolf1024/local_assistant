"""附件：**先拿清单，按需抽正文**。

分两步是有原因的：
    - Graph 每封邮件的附件列表很便宜（一次请求、几百字节），先存进索引，
      「上周那份 PDF 说了什么」这种问题才能靠**附件名**搜到；
    - 附件正文可能很大（几十 MB 的扫描件），绝不能跟着同步一起拉 ——
      只有用户真的问到这封、或者模型读了它，才按需下载并抽取，抽到的文本也缓存住。

能抽的（装了库就抽，没装就老实说抽不了，不假装）：
    ``.txt/.md/.csv/.json``  直接解码（UTF-8 优先，失败按 GBK）
    ``.pdf``                 pypdf / PyPDF2
    ``.docx``                python-docx
    ``.html/.htm``           去掉标签
其它（图片、压缩包、xlsx、pptx）只给清单信息，不抽正文。

★硬上限★：``max_mb``（默认 8）以上的不下载；抽出来的文本按 ``max_chars``（默认 4000）截断。
"""

from __future__ import annotations

import io
import logging
import re

#: 只抽正文、不做 OCR 的类型（扫描件抽不出来是已知限制）
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml")
_TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml",
                  ".log", ".ini", ".cfg", ".py", ".js", ".ts", ".html", ".htm")
_TAG = re.compile(r"<[^>]+>")


def kind_of(name: str, content_type: str = "") -> str:
    """给人看的类型标签：``pdf`` / ``docx`` / ``图片`` / 空串（不认识的）。"""
    suffix = ("." + str(name or "").rsplit(".", 1)[-1].lower()) if "." in str(name or "") else ""
    ctype = str(content_type or "").lower()
    if suffix == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if suffix in (".docx", ".doc") or "wordprocessingml" in ctype or ctype == "application/msword":
        return "docx"
    if suffix in (".xlsx", ".xls") or "spreadsheetml" in ctype:
        return "xlsx"
    if suffix in (".pptx", ".ppt") or "presentationml" in ctype:
        return "pptx"
    if ctype.startswith("image/") or suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        return "图片"
    if suffix in (".zip", ".rar", ".7z", ".gz"):
        return "压缩包"
    if suffix in _TEXT_SUFFIXES or ctype.startswith(_TEXT_TYPES):
        return "文本"
    return ""


def can_extract(name: str, content_type: str = "") -> bool:
    return kind_of(name, content_type) in ("pdf", "docx", "文本")


def human_size(size: int) -> str:
    n = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def describe(items: list[dict], *, limit: int = 6) -> str:
    """附件清单 → 一句人话：``报告.pdf（1.2 MB）、数据.csv（3 KB）``。"""
    parts = []
    for item in (items or [])[:limit]:
        if item.get("is_inline"):
            continue
        name = str(item.get("name") or "未命名")
        size = human_size(int(item.get("size") or 0))
        parts.append(f"{name}（{size}）")
    return "、".join(parts)


def extract_text(
    data: bytes,
    name: str,
    content_type: str = "",
    *,
    max_chars: int = 4000,
    log: logging.Logger | None = None,
) -> str:
    """从附件字节里抽正文。抽不了就返回空串（**不抛异常**，也不猜内容）。"""
    log = log or logging.getLogger("voice_loop.mail")
    if not data:
        return ""
    kind = kind_of(name, content_type)
    text = ""
    try:
        if kind == "文本":
            text = _decode(data)
            if kind_of(name, "") == "文本" and str(name or "").lower().endswith((".html", ".htm")):
                text = _TAG.sub(" ", text)
        elif kind == "pdf":
            text = _pdf_text(data)
        elif kind == "docx":
            text = _docx_text(data)
    except Exception as exc:  # noqa: BLE001 - 抽不出来不该让读信流程崩
        log.info(f"[mail] 附件抽文本失败（{name}）：{exc}")
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = max(200, int(max_chars))
    return text[:limit]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:  # pragma: no cover - 环境相关
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return ""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in list(reader.pages)[:20]:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _docx_text(data: bytes) -> str:
    try:
        import docx  # type: ignore
    except ImportError:
        return ""
    doc = docx.Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text]
    for table in doc.tables[:5]:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)
