"""看图功能的离线测试（不需要麦克风、不需要视觉模型）。

覆盖：
    [1] 找文件：模糊匹配、后缀过滤、最近改过优先、找不到时不瞎猜
    [2] 确认流程：先报完整路径 -> 「是」才读；不是/取消/选第几个/超时
    [3] 读文件：utf-8 / gbk / 空文件 / docx / 图片走视觉模型
    [4] 编码：长边被压到配置值以内，base64 能解回一张真图
    [5] 路由：「看看我的屏幕上是什么」-> 截图；「读一下 xxx」-> 先问；闲聊不受影响
    [6] 缺视觉模型时报清楚怎么装（这条最容易踩：用文本模型看图只会编）

用法：
    python scripts/test_vision.py
为了不污染真实数据，摄像头的存图目录与技能 json 都指到临时目录。
"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging  # noqa: E402

from voice_loop.llm import OllamaClient, OllamaError  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, expect=None, contains: str | None = None) -> None:
    ok = True
    if expect is not None:
        ok = got == expect
    elif contains is not None:
        ok = contains in str(got)
    print(f"  {PASS if ok else FAIL} {name}: {got!r}" + ("" if ok else f"   (期望 {expect or contains!r})"))
    if not ok:
        _failures.append(name)


def make_settings(tmp: Path):
    settings = load_settings()
    settings.skills.data_dir = str(tmp)
    settings.skills.alarm_file = str(tmp / "alarms.json")
    settings.skills.memo_file = str(tmp / "memos.json")
    settings.skills.schedule_file = str(tmp / "schedule.json")
    settings.vision.save_dir = str(tmp / "shots")
    settings.vision.file_roots = [str(tmp / "roots")]
    return settings


def build(tmp: Path):
    from voice_loop.skills import Skills
    from voice_loop.vision import Vision

    settings = make_settings(tmp)
    log = logging.getLogger("voice_loop")
    return settings, Vision(settings.vision, settings.root, log), Skills(settings, log)


# --------------------------------------------------------------------------- #
def test_find_file() -> None:
    print("\n[1] 找文件（模糊匹配）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, vision, _skills = build(tmp)
    root = Path(settings.vision.file_roots[0])
    (root / "报告").mkdir(parents=True)
    (root / "报告" / "2026年春季学期实验报告.docx").write_text("x", encoding="utf-8")
    (root / "课程笔记.txt").write_text("笔记", encoding="utf-8")
    (root / "课表.csv").write_text("a,b", encoding="utf-8")
    (root / "下载的论文.pdf").write_text("%PDF-1.4", encoding="utf-8")
    # 造一个「一小时前改过」的旧文件，验证「刚改过的更占优」
    old = root / "旧笔记.txt"
    old.write_text("旧", encoding="utf-8")
    past = time.time() - 30 * 86400
    import os

    os.utime(old, (past, past))

    hits = vision.find_file("读一下课程笔记")
    check("按名字找到「课程笔记.txt」", hits[0].name if hits else None, "课程笔记.txt")
    hits = vision.find_file("看看那个实验报告")
    check("名字部分匹配到实验报告", hits[0].name if hits else None, "2026年春季学期实验报告.docx")
    hits = vision.find_file("打开下载的论文.pdf")
    check("说清后缀时优先带这个后缀", hits[0].name if hits else None, "下载的论文.pdf")
    hits = vision.find_file("看看课表")
    check("短名字也能对上（课表.csv）", hits[0].name if hits else None, "课表.csv")
    check("没对上的不进候选", [h.name for h in vision.find_file("读一下量子力学讲义")], [])
    check("描述里带完整路径和大小", str(vision.describe(hits[0])), contains=str(root))
    check("同分时刚改过的排前面", [h.name for h in vision.find_file("笔记")][0] in ("课程笔记.txt", "旧笔记.txt"), True)
    names = [h.name for h in vision.find_file("笔记")]
    check("「笔记」把两个都列出来", len(names), 2)
    check("刚改过的排在旧的前面", names[0], "课程笔记.txt")
    check("query_name 会把废话去掉", vision.query_name("读一下那个课程笔记"), "课程笔记")

    print("    · 只说「哪种文件」时按后缀找（真人说话经常不带名字）")
    (root / "照片.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "表格数据.xlsx").write_text("x", encoding="utf-8")
    (root / "笔记.json").write_text("{}", encoding="utf-8")
    hits = vision.find_file("看看我的图片")
    check("「图片」按图片后缀找", hits[0].name if hits else None, "照片.png")
    hits = vision.find_file("念一下表格")
    check("「表格」既按名字也按后缀", hits[0].name if hits else None, "表格数据.xlsx")
    hits = vision.find_file("读一下那个 json 文件")
    check("不带点号的后缀词也认（json）", hits[0].name if hits else None, "笔记.json")

    print("    · 说「下载里的」就只在下载里找")
    dl = tmp / "Downloads"
    dl.mkdir(parents=True, exist_ok=True)
    (dl / "轨迹数据.json").write_text("{}", encoding="utf-8")
    settings.vision.file_roots = [str(root), str(dl)]
    hits = vision.find_file("看看下载里的 json")
    check("中文「下载」能对上英文 Downloads 目录",
          str(dl) in str(hits[0].path) if hits else None, True)
    check("这时不会把别的目录的文件也端出来",
          all(str(dl) in str(h.path) for h in hits), True)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_confirm_flow() -> None:
    print("\n[2] 文件确认流程（先说清楚是哪个，再读）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, _vision, skills = build(tmp)
    root = Path(settings.vision.file_roots[0])
    root.mkdir(parents=True, exist_ok=True)
    target = root / "会议纪要.txt"
    target.write_text("第一次会议：确定了三个人分工。\n第二次会议：改到周五。", encoding="utf-8")

    r = skills.handle("读一下会议纪要")
    print(f"        {r.reply}")
    check("先问一句，不直接读", getattr(r, "action", ""), "vision_ask")
    check("确认里带完整路径", str(target) in r.reply, True)
    check("这时还没真的读", skills._vision_pending is not None, True)  # noqa: SLF001

    r = skills.handle("不是")
    check("说「不是」就作罢", getattr(r, "action", ""), "vision_cancel")
    check("作罢后清掉待确认", skills._vision_pending, None)

    skills.handle("读一下会议纪要")
    r = skills.handle("是")
    check("说「是」才真的去看", getattr(r, "action", ""), "vision")
    check("文本文件不需要图片", r.data.get("images"), [])
    check("prompt 里带上了文件内容", "第二次会议" in str(r.data.get("prompt")), True)
    check("读文件时上下文放大", r.data.get("num_ctx"), settings.vision.file_num_ctx)
    check("说了看图前的提示语", r.data.get("note"), settings.vision.say_first)

    skills.handle("读一下会议纪要")
    r = skills.handle("今天有什么课")
    check("下一句是新指令时不误当成确认", getattr(r, "action", ""), "schedule_query")
    check("新指令后待确认也清掉", skills._vision_pending, None)

    # 多个候选 -> 让用户挑
    (root / "会议纪要备份.txt").write_text("备份", encoding="utf-8")
    r = skills.handle("读一下会议纪要")
    print(f"        {r.reply}")
    check("两个候选时问是哪个", getattr(r, "action", ""), "vision_ask")
    r = skills.handle("第二个")
    check("说「第二个」能选中", getattr(r, "action", ""), "vision")
    check("选中的是备份那个", "会议纪要备份" in str(r.data.get("shot")), True)

    skills.handle("读一下会议纪要")
    r = skills.handle("会议纪要备份")
    check("直接再报一遍名字也能选中", "会议纪要备份" in str(r.data.get("shot")), True)

    # 超时作废
    skills.handle("读一下会议纪要")
    skills._vision_pending["at"] = time.monotonic() - 999  # noqa: SLF001
    r = skills.handle("是")
    check("确认超时后不再当确认", getattr(r, "action", None), None)
    check("超时清掉待确认", skills._vision_pending, None)

    r = skills.handle("读一下量子力学讲义")
    check("找不到文件时说明白在哪找过", getattr(r, "action", ""), "vision_miss")
    check("提示里列出根目录", "roots" in r.reply, True)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_read_file() -> None:
    print("\n[3] 读文件（编码 / 类型）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, vision, _skills = build(tmp)
    root = Path(settings.vision.file_roots[0])
    root.mkdir(parents=True, exist_ok=True)

    utf8 = root / "中文.txt"
    utf8.write_text("你好，这是 UTF-8 的中文。", encoding="utf-8")
    gbk = root / "老文件.txt"
    gbk.write_bytes("你好，这是 GBK 的中文。".encode("gbk"))
    empty = root / "空的.txt"
    empty.write_text("", encoding="utf-8")
    pic = root / "照片.png"
    from PIL import Image

    Image.new("RGB", (40, 40), (10, 200, 30)).save(pic)

    text, kind = vision.read_file(utf8)
    check("utf-8 中文读得对", (kind, text.strip()), ("text", "你好，这是 UTF-8 的中文。"))
    text, _kind = vision.read_file(gbk)
    check("gbk 中文也读得对", text.strip(), "你好，这是 GBK 的中文。")
    check("空文件单独标出来", vision.read_file(empty)[1], "empty")
    check("图片交给视觉模型", vision.read_file(pic)[1], "image")

    docx_path = root / "说明.docx"
    try:
        import docx

        d = docx.Document()
        d.add_paragraph("这是 Word 里的第一段。")
        d.save(str(docx_path))
        text, kind = vision.read_file(docx_path)
        check("Word 能抽文本", (kind, "第一段" in text), ("text", True))
    except ImportError:
        text, kind = vision.read_file(docx_path) if docx_path.exists() else ("", "skip")
        print("    · 没装 python-docx，跳过 Word 检查")

    pdf = root / "论文.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%not really a pdf\n")
    try:
        vision.read_file(pdf)
        print("    · 装了 pypdf，PDF 分支已走过（内容不是真 PDF，能走到就算过）")
    except Exception as exc:  # noqa: BLE001
        check("没装 pypdf 时提示怎么装", "pypdf" in str(exc) or "打不开" in str(exc), True)

    other = root / "神秘文件.xyz"
    other.write_bytes(b"\x00\x01\x02binary")
    text, kind = vision.read_file(other)
    check("未知后缀也尽量给出文本而不是崩", (kind, isinstance(text, str)), ("text", True))
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_encode() -> None:
    print("\n[4] 图片编码（降采样 + base64）")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, vision, _skills = build(tmp)
    big = settings.sessions_dir / "big.png"
    from PIL import Image

    Image.new("RGB", (4000, 3000), (200, 30, 30)).save(big)
    b64 = vision.encode(big)
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    check("长边压到 1568 以内（截图档）", max(img.size) <= settings.vision.screen_max_side, True)
    check("保持宽高比", round(img.width / img.height, 2), round(4000 / 3000, 2))
    small = settings.sessions_dir / "small.png"
    Image.new("RGB", (100, 80), (0, 0, 0)).save(small)
    img2 = Image.open(io.BytesIO(base64.b64decode(vision.encode(small))))
    check("小图不会被放大", img2.size, (100, 80))
    check("base64 不带 data: 前缀", b64[:5] in ("/9j/4", "/9j/4A"), True)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_routing() -> None:
    print("\n[5] 路由：什么话才算「看图」")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, vision, skills = build(tmp)
    root = Path(settings.vision.file_roots[0])
    root.mkdir(parents=True, exist_ok=True)

    # 屏幕/摄像头：真的去截 / 去拍，拍不到也应给出人话（不抛异常）
    r = skills.handle("看看我的屏幕上是什么")
    print(f"        屏幕上 -> [{getattr(r, 'action', '')}] {str(getattr(r, 'reply', ''))[:60]}")
    check("「看看我的屏幕上是什么」看屏幕", getattr(r, "action", "") in ("vision", "vision_error"), True)
    if getattr(r, "action", "") == "vision":
        check("截屏结果带图", len(r.data.get("images") or []), 1)
        check("告诉模型看的是屏幕", "屏幕" in str(r.data.get("what")), True)

    r = skills.handle("摄像头看看这是什么")
    print(f"        摄像头 -> [{getattr(r, 'action', '')}] {str(getattr(r, 'reply', ''))[:60]}")
    check("提到摄像头就走摄像头", getattr(r, "action", "") in ("vision", "vision_error"), True)

    skills.vision.last = None              # 装作还没看过任何东西
    r = skills.handle("刚才那张图是什么")
    check("没拍过时说清楚没图可看", getattr(r, "action", ""), "vision_miss")

    r = skills.handle("看看我的屏幕上是什么")
    if getattr(r, "action", "") == "vision":      # 有显示器才跑得通
        r = skills.handle("刚才那张图是什么")
        check("看过之后可以「再看刚才那张」", getattr(r, "action", ""), "vision")
        check("用的是存下来的那张，没重新截", "screen" in str(r.data.get("shot")), True)

    # 不是看图的句子要原样交给别的技能 / LLM
    check("「看看今天的日程」还是日程查询",
          getattr(skills.handle("看看今天的日程"), "action", "").startswith("schedule"), True)
    check("「关屏幕」还是关屏而不是看图",
          getattr(skills.handle("关屏幕"), "action", ""), "screen_off")
    check("「看看新闻」不该去拍照", skills.handle("看看新闻"), None)
    check("「今天有什么课」不受影响",
          getattr(skills.handle("今天有什么课"), "action", ""), "schedule_query")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_missing_vision_model() -> None:
    print("\n[6] 没有视觉模型时要说清楚怎么装")
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_vis_"))
    settings, _vision, _skills = build(tmp)
    llm = OllamaClient(settings.llm)
    try:
        models = llm.list_models()
    except OllamaError as exc:
        print(f"    ! Ollama 没起来（{exc}），跳过")
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        return
    try:
        llm.resolve_model("definitely-not-a-real-model:1b")
        check("不存在的模型要报错", "没报错", "应该报错")
    except OllamaError as exc:
        check("不存在的模型会报错", "ollama pull" in str(exc), True)
        check("错误里说明「纯文本模型看图只会编」", "编" in str(exc), True)
    if any(m.split(":")[0] == settings.vision.model.split(":")[0] for m in models):
        name = llm.resolve_model(settings.vision.model)
        check("已装视觉模型时能解析到", name, settings.vision.model)
    else:
        print(f"    · 本机还没装 {settings.vision.model}，看图前先 ollama pull 一下")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 66)
    print(" 看图功能离线测试（摄像头 / 屏幕 / 文件）")
    print("=" * 66)
    test_find_file()
    test_confirm_flow()
    test_read_file()
    test_encode()
    test_routing()
    test_missing_vision_model()
    print("\n" + "=" * 66)
    if _failures:
        print(f" 失败 {len(_failures)} 项：{_failures}")
        print("=" * 66)
        return 1
    print(" 全部通过 √")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
