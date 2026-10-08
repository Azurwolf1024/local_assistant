"""安装 / 升级 / 发布这三件事的自测（离线，十几秒）。

★为什么值得一条条钉住★：这三件事都**会动用户的机器** —— 装依赖、下模型、覆盖代码、
还原数据。真出错的代价不是一个函数返回 None，而是「升级把我这几天的日程弄没了」。
所以这里重点钉的不是「功能好用」，而是**边界**：

    非 git 目录里升级要好好说话（不是崩）、备份不能漏文件也不能把 models/ 卷进来、
    还原默认必须是 dry-run、发布包里不能混进 models/sessions/.venv、
    拉不到远端时**一个字节都不许写**。

    python scripts\\test_setup_flow.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import voice_loop  # noqa: E402
from voice_loop import setup_flow as flow  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def _posix(path) -> str:
    return Path(str(path)).as_posix()


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=300, **kw)


def _git(cwd: Path, *args: str) -> str:
    got = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                         encoding="utf-8", errors="replace", timeout=60)
    return (got.stdout or "").strip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
def test_version() -> None:
    print("\n[1] 版本号只有一个地方写")
    text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check("VERSION 文件存在且是一行号", text, voice_loop.__version__)
    got = _run([str(ROOT / "main.py"), "--version"])
    check("main.py --version 打的就是这个号",
          (got.returncode, voice_loop.__version__ in got.stdout), (0, True),
          detail=got.stdout.strip())
    check("版本号看起来像语义化版本",
          bool(__import__("re").match(r"^\d+\.\d+\.\d+", voice_loop.__version__)), True)
    # 最低 Python 版本不能在两个地方各写一个
    deploy = (ROOT / "scripts" / "check_deploy.py").read_text(encoding="utf-8")
    check("体检脚本里的最低版本与 setup_flow 一致", "(3, 11)" in deploy, True)
    check("模型组名从 download_models 读出来的，没跑偏",
          flow.model_groups_known(), tuple(flow.MODEL_GROUPS))


def test_pure_checks() -> None:
    print("\n[2] 环境判定（纯函数，喂假数据就能验）")
    check("Python 3.10 会被拦下", flow.python_state((3, 10, 12), "x").level, flow.FAIL)
    check("Python 3.11 通过", flow.python_state((3, 11, 0), "x").level, flow.OK)
    check("当前解释器通过", flow.python_state().level, flow.OK)
    check("路径有空格会提醒", flow.path_state("D:\\local AI").level, flow.WARN)
    check("路径有中文会提醒", flow.path_state("D:\\项目").level, flow.WARN)
    check("纯英文无空格不啰嗦", flow.path_state("D:\\local_AI").level, flow.OK)
    rep = flow.Report()
    rep.add("能用的东西", flow.OK, "")
    rep.add("坏掉的东西", flow.FAIL, "原因", "这么修")
    check("Report.ok 认失败项", rep.ok, False)
    check("render 里带上了「怎么办」", "这么修" in rep.render(), True)
    check("as_dict 能序列化（给脚本/CI 用）", isinstance(rep.as_dict()["failed"], list), True)
    check("human() 说人话", (flow.human(999), flow.human(2048), flow.human(3 * 1024**3)),
          ("999 B", "2.0 KB", "3.0 GB"))


def _fake_root(tmp: Path) -> Path:
    """造一个「像一个真安装」的目录：数据、模型、会话、旧备份都在。"""
    (tmp / "data" / "personas" / "alice").mkdir(parents=True)
    (tmp / "data" / "memory").mkdir(parents=True)
    (tmp / "data" / "backup" / "old-20260101").mkdir(parents=True)
    (tmp / "models").mkdir(parents=True)
    (tmp / "sessions").mkdir(parents=True)
    (tmp / "data" / "events.json").write_text('{"items": [1]}', encoding="utf-8")
    (tmp / "data" / "characters.json").write_text('{"characters": []}', encoding="utf-8")
    (tmp / "data" / "alarms.json.lock").write_text("12345", encoding="utf-8")
    (tmp / "data" / "personas" / "alice.json").write_text('{"id": "alice"}', encoding="utf-8")
    (tmp / "data" / "personas" / "alice" / "ref.wav").write_bytes(b"RIFF....")
    (tmp / "data" / "memory" / "alice.json").write_text('{"facts": []}', encoding="utf-8")
    (tmp / "data" / "backup" / "old-20260101" / "events.json").write_text("旧的", encoding="utf-8")
    (tmp / "models" / "big.onnx").write_bytes(b"x" * 1024)
    (tmp / "sessions" / "listen.log").write_text("日志", encoding="utf-8")
    (tmp / "config.toml").write_text("[app]\nproject_root = \".\"\n", encoding="utf-8")
    (tmp / "main.py").write_text("# 假装是代码\n", encoding="utf-8")
    return tmp


def test_backup(tmp: Path) -> None:
    print("\n[3] 备份与还原（★升级的前置条件★）")
    root = _fake_root(tmp)
    cands = [_posix(p.relative_to(root)) for p in flow.backup_candidates(root)]
    check("该备份的都在（数据 + 人格 + 记忆 + config）", sorted(cands),
          sorted(["config.toml", "data/characters.json", "data/events.json",
                  "data/memory/alice.json", "data/personas/alice.json",
                  "data/personas/alice/ref.wav"]))
    check("★不备份 models/★", any(c.startswith("models/") for c in cands), False)
    check("★不备份 sessions/★", any(c.startswith("sessions/") for c in cands), False)
    check("★不备份备份目录自己★", any("backup/" in c for c in cands), False)
    check("★不备份跨进程锁（.lock）★", any(c.endswith(".lock") for c in cands), False)

    bak, files, size = flow.backup_user_data(root, reason="自测", tag="abc123")
    check("备份目录建在 data/backup 下", _posix(bak).startswith(_posix(root / "data" / "backup")), True,
          detail=_posix(bak.relative_to(root)))
    check("文件数对得上", files, len(cands))
    check("大小是正数", size > 0, True)
    check("备份里有一模一样的 events.json",
          (bak / "data" / "events.json").read_bytes(), (root / "data" / "events.json").read_bytes())
    manifest = (bak / "MANIFEST.txt").read_text(encoding="utf-8")
    check("MANIFEST 里每个文件都有 sha256",
          manifest.count(_sha256(root / "data" / "events.json")), 1)
    check("★RESTORE.txt 里有不依赖本工具的还原命令★",
          "Copy-Item" in (bak / "RESTORE.txt").read_text(encoding="utf-8"), True)
    check("最近一次备份指的就是它", flow.latest_backup(root), bak)

    # 改坏一个文件 → 默认 dry-run 只报告，加 apply 才真还原
    (root / "data" / "events.json").write_text("弄坏了", encoding="utf-8")
    check("dry-run 列出要覆盖的文件",
          flow.restore_user_data(root, bak), ["data/events.json"])
    check("dry-run 真的没写", (root / "data" / "events.json").read_text(encoding="utf-8"), "弄坏了")
    check("apply 才还原",
          flow.restore_user_data(root, bak, dry_run=False), ["data/events.json"])
    check("内容回到备份那一刻",
          (root / "data" / "events.json").read_bytes(), (bak / "data" / "events.json").read_bytes())
    check("已经一样时不再重复写", flow.restore_user_data(root, bak, dry_run=False), [])
    try:
        flow.restore_user_data(root / "nowhere", None)
        check("找不到备份要报错", False)
    except ValueError as exc:
        check("找不到备份会给人话", "备份" in str(exc), True, detail=str(exc))


def test_git_and_upgrade(tmp: Path) -> None:
    print("\n[4] 升级：不是 git / 工作区脏 / 拉不到远端 —— 三条必须好好说话")
    empty = tmp / "notgit"
    empty.mkdir(parents=True, exist_ok=True)
    plan = flow.upgrade_plan(empty)
    text = plan.render()
    check("非 git 目录不报错，只解释", plan.failed, [])
    check("并且说清楚压缩包怎么升", "压缩包" in text and "别删" in text, True)

    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "main.py").write_text("# v1\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "events.json").write_text("[]", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "v1")
    check("认得出这是 git 检出", flow.is_git(repo), True)
    check("取得到 HEAD", len(flow.head(repo)), 40)
    check("分支名看得懂", flow.current_branch(repo), "main")
    check("干净的工作区不会被误报脏", flow.is_dirty(repo), False)
    (repo / "main.py").write_text("# v1 改了\n", encoding="utf-8")
    check("改了文件就是脏的", flow.is_dirty(repo), True)
    check("并且能列出是哪个文件", any("main.py" in ln for ln in flow.dirty_files(repo)), True)

    n, why = flow.behind(repo, "main", "origin")
    check("没有远端时如实报 -1", n, -1)
    check("并且解释是哪个引用找不到", "origin/main" in why, True)

    # ★这条是最重要的★：拉不到远端时，一个字节都不该写（备份都不建）
    rep = flow.run_upgrade(root=repo, apply=True, allow_dirty=True)
    check("升级在 fetch 失败处停住", [s.name for s in rep.failed], ["git fetch"])
    check("★并且没有偷偷建备份目录★", (repo / "data" / "backup").exists(), False)
    check("原来的数据没动", (repo / "data" / "events.json").read_text(encoding="utf-8"), "[]")

    rep2 = flow.run_upgrade(root=repo, apply=False, allow_dirty=False)
    check("脏工作区默认被拦下", any("未提交改动" in s.name for s in rep2.failed), True)
    check("并且告诉你怎么保命", any("stash" in s.fix for s in rep2.steps), True)


def test_doctor() -> None:
    print("\n[5] 体检接口（复用的是 scripts/check_deploy.py）")
    data = flow.doctor_json()
    check("拿到了 JSON（没崩）", bool(data.get("items")), True, detail=str(data.get("error", ""))[:80])
    check("退出码是 0 或 1", data.get("exit_code") in (0, 1), True)
    check("每项都带 section/level/name",
          all({"section", "level", "name"} <= set(i) for i in data["items"][:10]), True)
    rep = flow.doctor_report()
    # ★别钉「这台机器没问题」★：那取决于机器状态。钉「翻译得对」就够。
    check("翻译成 Report（至少有汇总那一行）", len(rep.steps) >= 1, True)
    check("汇总结论与 failed 自洽", rep.ok, len(rep.failed) == 0)
    check("失败项都带 section（知道是哪一类）",
          all("·" in s.name for s in rep.failed), True)


def test_release(tmp: Path) -> None:
    print("\n[6] 发布打包：zip + 校验和 + 清单")
    got = _run(["scripts/make_release.py", "--check"])
    check("--check 能通过", (got.returncode, "还不能发布" in got.stdout), (0, False),
          detail=got.stdout.strip().splitlines()[-1] if got.stdout else "")

    out = tmp / "dist"
    got = _run(["scripts/make_release.py", "--out", str(out), "--notes", "自测"])
    check("真打包返回 0", got.returncode, 0, detail=(got.stderr or "")[-200:])
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    check("清单里的版本 = VERSION", latest["version"], voice_loop.__version__)
    asset = latest["assets"].get("source-zip") or {}
    zpath = out / asset.get("file", "missing.zip")
    check("zip 真的生成了", zpath.is_file(), True)
    check("清单里的 sha256 与文件对得上", asset.get("sha256"), _sha256(zpath))
    check("清单里的字节数与文件对得上", asset.get("bytes"), zpath.stat().st_size)
    check("连带写了 .sha256 校验文件",
          (out / f"{zpath.name}.sha256").read_text(encoding="utf-8").strip(),
          f"{asset.get('sha256')}  {zpath.name}")

    names = zipfile.ZipFile(zpath).namelist()
    top = f"local_assistant-{voice_loop.__version__}"
    for want in ("main.py", "VERSION", "requirements.txt", "config.toml", "install.ps1",
                 "RELEASE.txt", "voice_loop/setup_flow.py"):
        check(f"zip 里有 {want}", f"{top}/{want}" in names, True)
    check("★zip 里没有 models/（README 除外）★",
          [n for n in names if "/models/" in n and not n.endswith("models/README.md")], [])
    check("★zip 里没有 sessions/★", [n for n in names if "/sessions/" in n], [])
    check("★zip 里没有 .venv / 虚拟环境★", [n for n in names if ".venv" in n], [])
    check("★zip 里没有 dist/ 自己★", [n for n in names if "/dist/" in n], [])
    check("解压不会散一地文件（都套在顶层目录里）",
          all(n.startswith(top + "/") for n in names), True)
    # ★这条踩过★：`git tag --list <不存在的 tag>` 退出码是 0、输出为空 ——
    # 拿退出码判断会把「没打过」说成「已经打过」。
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_release  # noqa: PLC0415

    check("没打过的 tag 要说「没打过」", make_release.tag_exists("v0.0.0-does-not-exist"), False)
    check("有远端信息时能推出下载地址", "github.com" in (make_release.release_url_prefix() or "github.com"),
          True)


def test_gate(tmp: Path) -> None:
    print("\n[7] 闸门：真实仓库没被写")
    watched = [ROOT / "data" / "characters.json", ROOT / "config.toml",
               ROOT / "data" / "personas" / "kaltsit.json", ROOT / "main.py"]
    before = {p: (p.stat().st_size if p.is_file() else -1) for p in watched}
    marks = {p: p.stat().st_mtime_ns for p in watched if p.is_file()}
    # 在临时目录里做一遍「会写文件」的事（备份 + 真还原），再看真实仓库有没有被动
    fake = _fake_root(tmp / "gate")
    bak, _, _ = flow.backup_user_data(fake, reason="自测")
    (fake / "data" / "events.json").write_text("弄坏", encoding="utf-8")
    flow.restore_user_data(fake, bak, dry_run=False)
    check("沙盒里确实写进去了（不是整个测试空跑）",
          (fake / "data" / "events.json").read_text(encoding="utf-8"), '{"items": [1]}')
    after = {p: (p.stat().st_size if p.is_file() else -1) for p in watched}
    for path in watched:
        check(f"{path.relative_to(ROOT).as_posix()} 没被动过", after[path], before[path])
        if path.is_file():
            check(f"{path.name} 的修改时间没变", path.stat().st_mtime_ns, marks[path])
    real_backup = ROOT / "data" / "backup"
    leftovers = sorted(_posix(p.name) for p in real_backup.iterdir()
                       if p.is_dir() and "自测" in p.name) if real_backup.is_dir() else []
    check("真实 data/backup 里没留下测试备份", leftovers, [])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="setup_flow_"))
    try:
        test_version()
        test_pure_checks()
        test_backup(tmp / "backup")
        test_git_and_upgrade(tmp / "git")
        test_doctor()
        test_release(tmp / "release")
        test_gate(tmp / "gate-root")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 安装/升级/发布 自测全部通过")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
