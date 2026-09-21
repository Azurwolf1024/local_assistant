"""导入程序（scripts/import_lines.py）的离线测试。

要测的核心就一条：**只添加、不替换**——已有的内容不许动，同文本不重复写。
顺带把两种清单排版、过滤开关、自动找 json、备份、试运行都过一遍。

    python scripts/test_import_lines.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_lines.py"
sys.path.insert(0, str(ROOT))

from voice_loop.manifest import parse_manifest  # noqa: E402

PASS = 0
FAIL = 0


def check(got, expect, label: str) -> None:
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}：得到 {got!r}，期望 {expect!r}")


def check_true(cond, label: str) -> None:
    check(bool(cond), True, label)


def run(*args: str) -> tuple[int, str]:
    # Windows 控制台默认 cp936，强制子进程用 UTF-8 输出，否则中文断言会因编码而假失败
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# 两种排版：A = 名字紧接正文（实测素材就是这种）；B = 名字 / 空行 / 正文
LAYOUT_A = "任命助理\n博士，请坐。别紧张。\n\n交谈1\n我会定期为你进行理学检查。\n"
LAYOUT_B = "任命助理\n\n博士，请坐。别紧张。\n\n交谈1\n\n我会定期为你进行理学检查。\n"
EXPECT = [("任命助理", "博士，请坐。别紧张。"), ("交谈1", "我会定期为你进行理学检查。")]


def test_parse() -> None:
    print("\n[1] 清单解析（两种排版 + 一行搞定 + 纯段落）")
    check(parse_manifest(LAYOUT_A), EXPECT, "名字紧接正文（实测素材排版）")
    check(parse_manifest(LAYOUT_B), EXPECT, "名字 / 空行 / 正文")
    check(
        parse_manifest("名字\t正文一\n甲：正文二\n"),
        [("名字", "正文一"), ("甲", "正文二")],
        "一行搞定：TAB / 冒号（连续两行都要收）",
    )
    check(
        parse_manifest("第一段正文。\n\n第二段正文。\n"),
        [("", "第一段正文。"), ("", "第二段正文。")],
        "没有名字的纯段落：名字留空",
    )
    # 纯段落里如果有短行，默认会当名字；--plain 就是给这种情况的逃生口
    short = "好的\n\n知道了\n"
    check(parse_manifest(short), [("好的", "知道了")], "短行会被当成名字（默认行为）")
    check(parse_manifest(short, plain=True), [("", "好的"), ("", "知道了")], "--plain：都当正文")


def test_append_only(tmp: Path) -> None:
    print("\n[2] 只添加、不替换")
    txt = tmp / "a.txt"
    txt.write_text(LAYOUT_A, encoding="utf-8")
    js = tmp / "a.json"
    js.write_text(
        json.dumps(
            {
                "id": "a",
                "lines": [
                    {"scene": "被唤醒", "text": "我在。"},
                    {"scene": "旧场景", "text": "博士，请坐。别紧张。"},  # 同文本、不同场景
                ],
                "style": ["冷静"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    before = load(js)

    rc, out = run(str(txt), "--json", str(js), "--apply")
    check(rc, 0, "退出码 0")
    check_true("已写入" in out, "提示已写入")
    after = load(js)

    check([x["text"] for x in after["lines"]][:2], ["我在。", "博士，请坐。别紧张。"], "原有两条一字未动")
    check(len(after["lines"]), 3, "只新增了「交谈1」这一条")
    check(after["lines"][-1], {"scene": "交谈1", "text": "我会定期为你进行理学检查。"}, "新增条目形状对")
    check(after["style"], before["style"], "别的字段没被动")
    check(after["id"], "a", "顶层字段没被动")
    check_true((tmp / "a.json.bak").is_file(), "生成了 .bak 备份")
    check_true(load(tmp / "a.json.bak")["lines"] == before["lines"], "备份内容是改动前的")

    rc2, out2 = run(str(txt), "--json", str(js), "--apply", "--no-backup")
    check(rc2, 0, "再跑一次退出码 0")
    check_true("没有可新增的内容" in out2, "再跑一次没有新增（幂等）")
    check(len(load(js)["lines"]), 3, "再跑一次条数不变")


def test_dry_run(tmp: Path) -> None:
    print("\n[3] 默认只试运行 / --as text / --key")
    txt = tmp / "b.txt"
    txt.write_text(LAYOUT_A, encoding="utf-8")
    js = tmp / "b.json"
    js.write_text(json.dumps({"lines": []}, ensure_ascii=False), encoding="utf-8")

    rc, out = run(str(txt), "--json", str(js))
    check(rc, 0, "退出码 0")
    check_true("试运行" in out and "没有写文件" in out, "默认不写文件")
    check(load(js)["lines"], [], "文件确实没变")

    rc, out = run(str(txt), "--json", str(js), "--key", "style", "--as", "text", "--apply")
    data = load(js)
    check(data["style"], ["博士，请坐。别紧张。", "我会定期为你进行理学检查。"], "--as text 写入纯字符串")
    check(data["lines"], [], "没碰 lines")

    rc, out = run(str(txt), "--json", str(js), "--scene", "随意对话", "--apply", "--no-backup")
    check([x["scene"] for x in load(js)["lines"]], ["随意对话", "随意对话"], "--scene 统一覆盖场景名")


def test_filters(tmp: Path) -> None:
    print("\n[4] --skip / --max-chars")
    txt = tmp / "c.txt"
    txt.write_text(
        "作战中1\nMon3tr。\n\n交谈1\n我会定期为你进行理学检查，记录你的生命征象与意识状态。\n",
        encoding="utf-8",
    )
    js = tmp / "c.json"
    js.write_text(json.dumps({"lines": []}, ensure_ascii=False), encoding="utf-8")

    rc, out = run(str(txt), "--json", str(js), "--skip", "作战", "--apply")
    check([x["scene"] for x in load(js)["lines"]], ["交谈1"], "--skip 命中名字就跳过")
    check_true("--skip 命中" in out, "报告里说明了为什么跳过")

    js.write_text(json.dumps({"lines": []}, ensure_ascii=False), encoding="utf-8")
    rc, out = run(str(txt), "--json", str(js), "--max-chars", "8", "--apply")
    check([x["scene"] for x in load(js)["lines"]], ["作战中1"], "--max-chars 只留短的（Mon3tr。7 字）")
    check_true("> 8" in out, "报告里写了超长")


def test_auto_target(base: Path) -> None:
    print("\n[5] 自动找目标 json / 找不到就报错")
    # ① 同目录 <名字>.json
    d1 = base / "case1"
    d1.mkdir()
    (d1 / "p.txt").write_text(LAYOUT_A, encoding="utf-8")
    (d1 / "p.json").write_text(json.dumps({"lines": []}, ensure_ascii=False), encoding="utf-8")
    rc, out = run(str(d1 / "p.txt"), "--apply")
    check(len(load(d1 / "p.json")["lines"]), 2, "同目录 <名字>.json 被找到并写入")

    # ② 上一级 <名字>.json（实测布局：data/personas/kaltsit/kaltsit.txt -> data/personas/kaltsit.json）
    d2 = base / "case2"
    (d2 / "kaltsit").mkdir(parents=True)
    (d2 / "kaltsit" / "kaltsit.txt").write_text(LAYOUT_A, encoding="utf-8")
    (d2 / "kaltsit.json").write_text(json.dumps({"lines": []}, ensure_ascii=False), encoding="utf-8")
    rc, out = run(str(d2 / "kaltsit" / "kaltsit.txt"), "--apply", "--no-backup")
    check_true(str(d2 / "kaltsit.json") in out, "上一级 <名字>.json 被找到")
    check(len(load(d2 / "kaltsit.json")["lines"]), 2, "写进去了")

    # ③ 找不到
    d3 = base / "case3"
    d3.mkdir()
    (d3 / "nobody.txt").write_text(LAYOUT_A, encoding="utf-8")
    rc, out = run(str(d3 / "nobody.txt"), "--apply")
    check(rc, 2, "找不到目标 json → 退出码 2")
    check_true("--json" in out, "提示用 --json 指定")


def test_bad_input(tmp: Path) -> None:
    print("\n[6] 异常输入")
    rc, out = run(str(tmp / "没有这个文件.txt"))
    check(rc, 2, "清单不存在 → 退出码 2")

    empty = tmp / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")
    rc, out = run(str(empty), "--json", str(tmp / "a.json"))
    check(rc, 2, "清单解析不出条目 → 退出码 2")

    broken = tmp / "broken.json"
    broken.write_text("{ 这不是 json", encoding="utf-8")
    rc, out = run(str(tmp / "a.txt"), "--json", str(broken))
    check(rc, 2, "目标不是合法 json → 退出码 2")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_import_"))
    try:
        test_parse()
        test_append_only(tmp)
        test_dry_run(tmp)
        test_filters(tmp)
        test_auto_target(tmp)
        test_bad_input(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    print("EXIT=" + ("0" if FAIL == 0 else "1"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
