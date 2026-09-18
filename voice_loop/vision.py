"""看图：摄像头 / 屏幕截图 / 剪贴板 / 指定文件。

为什么单独一个模块：这几件事都要「拿到一份图或文本 -> 编码成模型能吃的样子」，
放一起才能共享降采样、存档、路径模糊匹配这些细节。

设计上的几个取舍
    - **原始图存盘**（``data/vision/``）：模型看错了，你能回头核对它到底看了什么。
      目录只保留最近 ``keep_images`` 张，不会无限长大。
    - **降采样**：CPU 上跑视觉模型，图越大越慢。默认把最长边压到 1024（截图 1568，
      因为屏幕上的小字压太狠就认不出了）。
    - **文件只读、且必须先确认**：模糊匹配出来的候选一律先报出**完整路径 + 大小 + 修改时间**，
      你说「是」才读。找文件只在配置的根目录（桌面/下载/文档）里找，
      别的目录必须把完整路径说出来——这样「看看那个文件」不会变成翻你整个硬盘。
    - **文本文件不需要视觉模型**：``.txt/.md/.py/.json`` 这类直接抽文本交给普通模型，
      只有图片才需要 VL 模型。PDF 要额外的库（``pip install pypdf``）。
"""

from __future__ import annotations

import base64
import difflib
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .settings import VisionConfig


class VisionError(RuntimeError):
    """看图失败（打不开摄像头 / 文件读不了 / 没有视觉模型）。"""


# --------------------------------------------------------------------------- #
# 找文件
# --------------------------------------------------------------------------- #
# 说话时会夹带的废话：留着会影响匹配（「读一下桌面上的报告」里真正有用的是「报告」）
_FILE_STOP = re.compile(
    r"(?:帮我|请|麻烦|你|我|把|给|这个|那个|这份|那份|这本|那本|这篇|那篇|这封|那封|"
    r"一份|一个|一下|的|了|吧|吗|"
    r"打开|看看|看一下|看下|看一眼|读一下|读读|读一读|念一下|念给我听|念|"
    r"文件|文档|附件|内容|里面的|里头|里头的|里面|里的|讲的|讲了什么|是什么|"
    r"有没有|找一下|找一找|找找|搜索|搜一下|"
    r"桌面上|桌面的|下载里|下载的|文档里|文档的|屏幕上|屏幕上的|桌面|下载|文档)"
)
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~/|\.\.?/)")
# 「论文.pdf」这种带点号的，以及「下载里的 json」这种只说后缀的
_EXT_HINT = re.compile(r"\.([A-Za-z0-9]{1,6})\b")
_EXT_WORD = re.compile(
    r"\b(jpg|jpeg|png|bmp|webp|gif|tiff|txt|md|rst|log|csv|tsv|json|toml|ini|yaml|yml|"
    r"xml|html|pdf|docx?|xlsx?|pptx?|py|js|ts|java|c|cpp|h|bat|ps1|sh)\b",
    re.IGNORECASE,
)
# 中文说法 -> 可能的后缀（「桌面上的图片」"下载里的表格"）
_CN_EXT: dict[str, tuple[str, ...]] = {
    "图片": ("png", "jpg", "jpeg", "webp", "bmp", "gif"),
    "图像": ("png", "jpg", "jpeg", "webp", "bmp"),
    "照片": ("jpg", "jpeg", "png"),
    "截图": ("png", "jpg", "jpeg"),
    "表格": ("xlsx", "xls", "csv"),
    "幻灯片": ("pptx", "ppt"),
    "幻灯片": ("pptx", "ppt"),
    "代码": ("py", "js", "ts", "c", "cpp", "h", "java"),
    "word": ("docx", "doc"),
    "excel": ("xlsx", "xls", "csv"),
    "ppt": ("pptx", "ppt"),
}
# 两个汉字之间的空格去掉（「火龙果队 总结」这种），英文名里的空格保留
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")
# 说「桌面上的 / 下载里的」时可以锁定根目录
_ROOT_HINT = re.compile(r"(桌面|下载|文档|desktop|downloads?|documents?)", re.IGNORECASE)

_HOME_ALIASES = {
    "桌面": ("Desktop", "桌面"),
    "下载": ("Downloads", "下载"),
    "文档": ("Documents", "文档"),
    "desktop": ("Desktop",),
    "download": ("Downloads",),
    "downloads": ("Downloads",),
    "document": ("Documents",),
    "documents": ("Documents",),
}

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".sql", ".bat", ".cmd", ".ps1", ".sh", ".m", ".r",
}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

# 目标名和查询名都做一遍这个归一化，避免空格/分隔符/大小写搞出假的不匹配
_NORM_STRIP = re.compile(r"[\s_\-—·．.，,。:：、'\"“”‘’()（）\[\]【】]")


def _norm(text: str) -> str:
    return _NORM_STRIP.sub("", (text or "").strip().lower())


def norm_name(text: str) -> str:
    """文件名/说话内容的归一化：去掉空格标点、转小写，再比相似度。"""
    return _norm(text)


@dataclass
class FileHit:
    """一个候选文件（含打分，便于解释「为什么是它」）。"""

    path: Path
    score: float
    why: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Shot:
    """一张拍下来/截下来的图。"""

    path: Path
    source: str                      # camera / screen / clipboard
    at: float = field(default_factory=time.time)

    @property
    def what(self) -> str:
        return {"camera": "摄像头画面", "screen": "屏幕截图", "clipboard": "剪贴板里的图"}.get(
            self.source, "图片"
        )


# --------------------------------------------------------------------------- #
class Vision:
    """采集 + 读文件 + 编码。状态只有「最近一张」，别的都在配置里。"""

    def __init__(self, cfg: VisionConfig, root: Path, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.log = logger or logging.getLogger("voice_loop")
        self.last: Shot | None = None

    # ------------------------------------------------------------ 目录
    @property
    def save_dir(self) -> Path:
        p = Path(self.cfg.save_dir)
        if not p.is_absolute():
            p = self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _new_path(self, tag: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.save_dir / f"{stamp}_{tag}.jpg"

    def prune(self) -> int:
        """只留最近 ``keep_images`` 张，返回删掉几张。"""
        keep = max(1, int(self.cfg.keep_images))
        try:
            files = sorted(
                (p for p in self.save_dir.glob("*.jpg") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return 0
        gone = 0
        for old in files[keep:]:
            try:
                old.unlink()
                gone += 1
            except OSError:
                pass
        return gone

    # ------------------------------------------------------------ 采集
    def camera(self) -> Shot:
        """拍一张。丢掉前几帧，等自动曝光稳下来（第一帧往往是黑的）。"""
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - 依赖已装
            raise VisionError("没有装 opencv-python，摄像头用不了：pip install opencv-python") from exc

        idx = int(self.cfg.camera_index)
        cap = None
        for backend in (getattr(cv2, "CAP_DSHOW", 0), 0):
            cap = cv2.VideoCapture(idx, backend) if backend else cv2.VideoCapture(idx)
            if cap.isOpened():
                break
            cap.release()
            cap = None
        if cap is None:
            raise VisionError(f"打不开摄像头（索引 {idx}）。是不是被其它程序占用了？")

        frame = None
        try:
            for _ in range(max(1, int(self.cfg.warmup_frames)) + 1):
                ok, img = cap.read()
                if ok and img is not None:
                    frame = img
        finally:
            cap.release()
        if frame is None:
            raise VisionError("摄像头没有返回画面，可能要等它初始化一下，再试一次。")

        from PIL import Image

        rgb = frame[:, :, ::-1]                       # OpenCV 是 BGR
        shot = Shot(self._new_path("cam"), "camera")
        self._save(Image.fromarray(rgb), shot.path, self.cfg.max_side)
        return self._remember(shot)

    def screen(self) -> Shot:
        """截屏（多显示器一起截）。

        顺序试几种抓法：``all_screens`` 在少数环境（远程桌面 / 屏幕锁定 / 奇怪的分辨率）
        上会直接抛 ``screen grab failed``，那就退回只抓主屏。全都不行才报错，
        并且把「屏幕锁了」这个最常见的原因说出来。
        """
        from PIL import ImageGrab

        img = None
        last_exc: Exception | None = None
        for kwargs in ({"all_screens": True}, {}, {"all_screens": False}):
            try:
                img = ImageGrab.grab(**kwargs)
                if img is not None:
                    break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if img is None:
            raise VisionError(
                f"截不到屏幕（{last_exc}）。如果屏幕锁着（Win+L）或者在被远程控制，"
                "解锁后再试；也可以先用「用摄像头看看」。"
            )
        shot = Shot(self._new_path("screen"), "screen")
        self._save(img, shot.path, self.cfg.screen_max_side)
        return self._remember(shot)

    def clipboard(self) -> Shot:
        """剪贴板里的图。复制的是文件的话返回 None（交给文件那条路）。"""
        from PIL import ImageGrab

        try:
            data = ImageGrab.grabclipboard()
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"读剪贴板失败：{exc}") from exc
        if data is None:
            raise VisionError("剪贴板里现在没有图片。")
        if not hasattr(data, "save"):
            # 在资源管理器里复制文件时，剪贴板给的是文件名列表
            if isinstance(data, list) and data:
                raise VisionError(f"剪贴板里是文件而不是图：{Path(str(data[0])).name}")
            raise VisionError("剪贴板里现在没有图片。")
        shot = Shot(self._new_path("clip"), "clipboard")
        self._save(data, shot.path, self.cfg.screen_max_side)
        return self._remember(shot)

    def _remember(self, shot: Shot) -> Shot:
        self.last = shot
        self.prune()
        self.log.info(f"[看图] {shot.what} -> {shot.path}")
        return shot

    def _save(self, img, path: Path, max_side: int) -> Path:
        from PIL import Image

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img = _fit(img, max_side)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="JPEG", quality=int(self.cfg.jpeg_quality), optimize=True)
        return path

    # ------------------------------------------------------------ 编码
    def encode(self, path: str | Path, max_side: int | None = None) -> str:
        """把图片压到最长边 ``max_side`` 并转成 base64 JPEG（Ollama 的 images 字段）。"""
        from PIL import Image

        p = Path(path)
        if not p.exists():
            raise VisionError(f"图片不在了：{p}")
        try:
            with Image.open(p) as img:
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img = _fit(img, max_side or self.cfg.screen_max_side)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=int(self.cfg.jpeg_quality), optimize=True)
        except VisionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"这张图打不开：{exc}") from exc
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ------------------------------------------------------------ 找文件
    def roots(self) -> list[Path]:
        """把配置里的根目录（桌面/下载/文档，或任意路径）摊成真实目录。"""
        out: list[Path] = []
        home = Path.home()
        for raw in self.cfg.file_roots:
            name = str(raw).strip()
            if not name:
                continue
            p = Path(name)
            candidates = [p] if (p.is_absolute() or _PATH_RE.match(name)) else []
            if not candidates:
                for alias in _HOME_ALIASES.get(name.lower(), (name,)):
                    candidates.append(home / alias)
            for c in candidates:
                c = _expand(c)
                if c.is_dir() and c not in out:
                    out.append(c)
        return out

    def find_file(self, query: str) -> list[FileHit]:
        """模糊找文件。返回按分排序的候选（空列表 = 没找到）。

        顺序：完整路径优先 -> 根目录里按名字相似度找；越近改过的越占优。
        """
        text = (query or "").strip()
        if not text:
            return []
        hits: dict[str, FileHit] = {}

        # 1) 说清楚了完整路径
        for raw in re.findall(r"(?:[A-Za-z]:[\\/][^\s\"'，,。；;]+|\\\\[^\s\"'，,。；;]+)", text):
            p = Path(raw.strip().rstrip("。，,.；;"))
            if p.exists() and p.is_file():
                hits[str(p).lower()] = FileHit(p, 100.0, "完整路径")
            elif p.parent.is_dir():
                for h in self._scan_name(p.parent, p.stem, depth=1, exts=(p.suffix.lstrip("."),)):
                    hits.setdefault(str(h.path).lower(), h)

        # 2) 在根目录里按名字找
        exts = self._exts_of(text)
        name = self._query_name(text)
        if name and _norm(name) in exts:           # 只说「下载里的 json」：名字就是后缀
            name = ""
        if name or exts:
            roots = self.roots()
            hint = _ROOT_HINT.search(text)
            if hint:
                # 「下载里的」要认出来：中文说法和英文目录名对不上，得先过一遍别名表
                token = hint.group(1).lower()
                aliases = [a.lower() for a in _HOME_ALIASES.get(token, (token,))]
                picked = [
                    r for r in roots
                    if r.name.lower() in aliases or str(r).lower() in aliases
                ]
                roots = picked or roots
            for root in roots:
                for h in self._scan_name(root, name, exts=exts):
                    hits.setdefault(str(h.path).lower(), h)
            if not hits and name and exts:
                # 「文档里的幻灯片」这种：名字里没这两个字，但说了要哪种文件 -> 按后缀兜一遍
                for root in roots:
                    for h in self._scan_name(root, "", exts=exts):
                        hits.setdefault(str(h.path).lower(), h)

        ordered = sorted(hits.values(), key=lambda h: h.score, reverse=True)
        return [h for h in ordered if h.score >= 45][:5]

    def _query_name(self, text: str) -> str:
        """从一句话里抠出「文件名叫什么」。"""
        name = _FILE_STOP.sub(" ", text or "")
        name = re.sub(r"[，,。；;！!？?、]+", " ", name)
        name = _CJK_SPACE.sub("", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name

    def query_name(self, text: str) -> str:
        """给外部用：这句话里的文件名部分（可能为空）。"""
        return self._query_name(text)

    def _exts_of(self, text: str) -> tuple[str, ...]:
        """这句话里点名要哪种文件：'.pdf' / 'json' / '图片' 都认。"""
        t = text or ""
        m = _EXT_HINT.search(t)
        if m:
            return (m.group(1).lower(),)
        m = _EXT_WORD.search(t)
        if m:
            return (m.group(1).lower(),)
        low = t.lower()
        for word, exts in sorted(_CN_EXT.items(), key=lambda kv: -len(kv[0])):
            if word in low:
                return exts
        return ()

    def _scan_name(self, root: Path, name: str, depth: int | None = None,
                   exts: tuple[str, ...] = ()) -> list[FileHit]:
        """在 root 里往下找文件名像 name 的文件。"""
        max_depth = int(depth if depth is not None else self.cfg.file_max_depth)
        budget = int(self.cfg.file_max_scan)
        want = tuple(e.lower().lstrip(".") for e in exts if e)
        q = _norm(name)
        if not q and not want:
            return []
        out: list[FileHit] = []
        root_depth = len(root.parts)
        scanned = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
                here = Path(dirpath)
                if len(here.parts) - root_depth >= max_depth:
                    dirnames[:] = []
                # 跳过明显的垃圾目录，省时间
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d.lower() not in ("node_modules", "venv", "__pycache__")
                ]
                for fn in filenames:
                    scanned += 1
                    if scanned > budget:
                        return out
                    if fn.startswith("~$") or fn.startswith("."):
                        continue
                    p = here / fn
                    score, why = self._score(p, q, want)
                    if score > 0:
                        out.append(FileHit(p, score, why))
        except OSError:
            pass
        out.sort(key=lambda h: h.score, reverse=True)
        return out[:5]

    def _score(self, path: Path, q: str, want_exts: tuple[str, ...]) -> tuple[float, str]:
        stem, full = _norm(path.stem), _norm(path.name)
        suffix = path.suffix.lower().lstrip(".")
        if not q:
            if want_exts and suffix not in want_exts:
                return 0.0, ""
            return 50.0 + self._recent(path), "按后缀找"
        if q == stem or q == full:
            score, why = 100.0, "名字完全一样"
        elif q in full:
            score, why = 76.0 + min(12.0, len(q)), "名字里含这几个字"
        else:
            ratio = difflib.SequenceMatcher(None, q, stem).ratio()
            if len(path.name) <= 24:
                ratio = max(ratio, difflib.SequenceMatcher(None, q, full).ratio())
            score, why = ratio * 68.0, "名字有点像"
        if score < 45:
            return 0.0, ""
        if want_exts:
            if suffix in want_exts:
                score += 14.0
                why += f"、后缀是 .{suffix}"
            else:
                score -= 22.0
        return score + self._recent(path), why

    @staticmethod
    def _recent(path: Path) -> float:
        """刚动过的文件更可能就是他嘴里那个（下载完马上让你看）。"""
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return 0.0
        if age < 3600:
            return 8.0
        if age < 86400:
            return 6.0
        if age < 7 * 86400:
            return 3.0
        if age < 45 * 86400:
            return 1.0
        return 0.0

    def describe(self, hit: FileHit | Path) -> str:
        """把候选说清楚：完整路径 + 大小 + 什么时候改的。用户才能一眼看出对不对。"""
        p = hit.path if isinstance(hit, FileHit) else Path(hit)
        try:
            st = p.stat()
            size = st.st_size
            size_text = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"
            age = time.time() - st.st_mtime
            if age < 3600:
                when = f"{max(1, int(age // 60))} 分钟前"
            elif age < 86400:
                when = f"{int(age // 3600)} 小时前"
            else:
                when = f"{int(age // 86400)} 天前"
            return f"{p}（{size_text}，{when}改的）"
        except OSError:
            return str(p)

    # ------------------------------------------------------------ 读文件
    def read_file(self, path: str | Path) -> tuple[str, str]:
        """读文件，返回 ``(文本, 类型)``。

        类型：``text``（抽出了文本）/ ``image``（图片，得交给视觉模型）/
        ``empty``（空的）。读不了的直接抛 :class:`VisionError`，由调用方念给用户。
        """
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise VisionError(f"找不到这个文件：{p}")
        suffix = p.suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            return "", "image"
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise VisionError(f"读不了这个文件：{exc}") from exc
        if size == 0:
            return "", "empty"
        if size > 40 * 1024 * 1024:
            raise VisionError(f"这个文件有 {size / 1048576:.0f} MB，太大了，我不看。")

        if suffix == ".pdf":
            return self._read_pdf(p), "text"
        if suffix in (".docx", ".doc"):
            return self._read_docx(p), "text"
        return self._read_text(p), "text"

    def _read_pdf(self, p: Path) -> str:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            try:
                from PyPDF2 import PdfReader  # type: ignore
            except ImportError as exc:
                raise VisionError(
                    "读 PDF 需要额外的库，先在终端跑：pip install pypdf"
                ) from exc
        try:
            reader = PdfReader(str(p))
            pages = []
            for i, page in enumerate(reader.pages):
                if i >= 20:
                    pages.append("（后面还有，只看到第 20 页）")
                    break
                pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"这个 PDF 打不开：{exc}") from exc
        text = "\n".join(pages).strip()
        if not text:
            raise VisionError("这个 PDF 里的文字抽不出来，可能是扫描件（那就得用视觉模型看图了）。")
        return text

    def _read_docx(self, p: Path) -> str:
        try:
            import docx  # type: ignore
        except ImportError as exc:
            raise VisionError("读 Word 需要额外的库，先在终端跑：pip install python-docx") from exc
        try:
            doc = docx.Document(str(p))
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"这个 Word 打不开：{exc}") from exc
        return "\n".join(x.text for x in doc.paragraphs).strip()

    def _read_text(self, p: Path) -> str:
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "gbk", "utf-16"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")   # 二进制也给它一段，别直接失败


def _expand(p: Path) -> Path:
    try:
        return p.expanduser().resolve()
    except OSError:
        return p


def _fit(img, max_side: int):
    """等比缩到最长边不超过 max_side（本来就更小则不动）。"""
    max_side = int(max_side or 0)
    if max_side <= 0:
        return img
    long_side = max(img.size)
    if long_side <= max_side:
        return img
    from PIL import Image

    scale = max_side / float(long_side)
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    return img.resize(size, Image.LANCZOS)
