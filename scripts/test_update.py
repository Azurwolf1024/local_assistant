"""更新逻辑的自测：``voice_loop/update.py``（★全程离线★，网上下载那一步是注进来的假函数）。

要钉住的是**升级唯一会毁数据的那几件事**：

    - 版本比较不能把 ``1.1.0`` 与 ``1.1.0+unknown`` 判成不同，也不能把 ``1.10`` 判小于 ``1.9``
    - 清单里 url 为空时要能自己拼出来（发布时没配 GitHub remote 也不会变成死路）
    - 合并发布包时 ★data/、config.toml、models/ 一个都不能覆盖★
    - ★sha256 不对就一个文件都不许写★（下载到一半的包不能进项目）
    - 压缩包里的 ``../`` 路径要拒绝（zip-slip：不然解压能写到项目外面）
    - 更新 exe 自己用的是「先改名再写」的顺序（Windows 不允许覆盖运行中的 exe）

    python scripts/test_update.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.pop("LOCAL_AI_ROOT", None)
os.environ.pop("LOCAL_AI_PYTHON", None)

FAILED: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    if want is None:
        ok = bool(got)
        line = f"  {'√' if ok else '×'} {name}" + (f": {detail or got}" if detail else "")
    else:
        ok = got == want
        line = f"  {'√' if ok else '×'} {name}: {got!r}" + (f"（期望 {want!r}）" if not ok else "")
    print(line)
    if not ok:
        FAILED.append(name)


def make_release_zip(version: str = "9.9.9", *, evil: bool = False) -> bytes:
    """造一个发布包：程序文件 + ★故意的 data/ 与 config.toml★（用来验证不会被覆盖）。"""
    buf = io.BytesIO()
    top = f"local_assistant-{version}"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{top}/main.py", f"# v{version}\nprint('hi')\n")
        zf.writestr(f"{top}/VERSION", f"{version}\n")
        zf.writestr(f"{top}/requirements.txt", "fastapi\n")
        zf.writestr(f"{top}/voice_loop/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr(f"{top}/voice_loop/brand_new.py", "# 新版才有的模块\n")
        zf.writestr(f"{top}/scripts/thing.py", "# 新版脚本\n")
        zf.writestr(f"{top}/RELEASE.txt", "说明\n")
        # 这两条是「陷阱」：真被覆盖了，用户数据就没了
        zf.writestr(f"{top}/config.toml", "[app]\n# 发布包里的默认配置（不该覆盖用户的）\n")
        zf.writestr(f"{top}/data/memos.json", '["发布包里的空备忘"]\n')
        if evil:
            zf.writestr(f"{top}/../escaped.py", "# zip-slip\n")
    return buf.getvalue()


def make_manifest(version: str = "9.9.9", *, url: str = "", sha256: str = "",
                  exe: bool = False, repo: str = "owner/name") -> dict:
    return {
        "name": "local_assistant", "version": version, "date": "2026-10-08T12:00:00",
        "python": ">=3.11", "channel": "stable", "notes": "修了几个小问题", "git": "abc1234",
        "assets": {
            "source-zip": {"file": f"local_assistant-{version}-source.zip", "bytes": 1024,
                           "sha256": sha256, "url": url},
            **({"console-exe": {"file": "local-assistant-console.exe", "bytes": 2048,
                                "sha256": "", "url": "https://example.invalid/app.exe"}}
               if exe else {}),
        },
    }


def make_project(tmp: Path, version: str = "1.1.0") -> Path:
    """搭一个「压缩包安装」的项目根（没有 .git）。"""
    (tmp / "data" / "personas").mkdir(parents=True, exist_ok=True)
    (tmp / "voice_loop").mkdir(parents=True, exist_ok=True)
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp / "main.py").write_text("# 老版本\n", encoding="utf-8")
    (tmp / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tmp / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp / "config.toml").write_text("[app]\nproject_root = \".\"\n# ★我的配置★\n",
                                    encoding="utf-8")
    (tmp / "data" / "memos.json").write_text('["买牛奶"]\n', encoding="utf-8")
    (tmp / "voice_loop" / "__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    (tmp / "voice_loop" / "gone.py").write_text("# 上游下个版本会删掉这个\n", encoding="utf-8")
    (tmp / "scripts" / "old.py").write_text("# 上游下个版本会删掉这个\n", encoding="utf-8")
    return tmp


def section_versions() -> None:
    print("\n[1] 版本号比较（1.10 > 1.9 这种坑）")
    from voice_loop import update as upd

    check("1.2.0 > 1.1.0", upd.compare_versions("1.2.0", "1.1.0"), 1)
    check("1.1.0 == 1.1.0+unknown", upd.compare_versions("1.1.0+unknown", "1.1.0"), 0)
    check("1.10 > 1.9（按数字比，不按字符串）", upd.compare_versions("1.10", "1.9"), 1)
    check("1.2 == 1.2.0", upd.compare_versions("1.2", "1.2.0"), 0)
    check("0.0.0+unknown 比 1.1.0 小", upd.compare_versions("0.0.0+unknown", "1.1.0"), -1)


def section_repo() -> None:
    print("\n[2] 仓库地址只有一份（从 install.ps1 读出来，不在这里写死）")
    from voice_loop import update as upd

    repo = upd.default_repo()
    check("读到了 owner/name", "/" in repo, True, detail=repo)
    install_text = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    check("install.ps1 里确实是这个地址", repo in install_text, True, detail=repo)
    check("清单地址走 releases/latest（不用 API，没有限流）",
          upd.manifest_url(repo),
          f"https://github.com/{repo}/releases/latest/download/latest.json")
    check("带版本号时走固定 tag（回滚用）",
          upd.release_url(repo, "x.zip", "1.2.3"),
          f"https://github.com/{repo}/releases/download/v1.2.3/x.zip")


def section_manifest() -> None:
    print("\n[3] 清单解析（url 留空时自己拼）")
    from voice_loop import update as upd

    rel = upd.parse_manifest(make_manifest("9.9.9"), repo="owner/name")
    check("解析出版本", rel.version, "9.9.9")
    check("解析出 zip 名", rel.zip_name, "local_assistant-9.9.9-source.zip")
    check("★url 空 → 按 repo 拼出来★", rel.zip_url,
          "https://github.com/owner/name/releases/download/v9.9.9/local_assistant-9.9.9-source.zip")
    check("有 sha256 就带着", rel.zip_sha256, "")
    rel2 = upd.parse_manifest(make_manifest("9.9.9", url="https://my.host/a.zip",
                                            sha256="a" * 64, exe=True))
    check("清单给了 url 就用它", rel2.zip_url, "https://my.host/a.zip")
    check("认得出有 exe 资产", rel2.has_exe, True)
    check("清单里的说明留着", rel2.notes, "修了几个小问题")


def section_check() -> None:
    print("\n[4] 检查更新：有新版 / 已最新 / 网络炸了（都注入假取数，不联网）")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_project(Path(tmpdir), "1.1.0")

        def fetch_new(url: str, *, timeout: float = 10.0):     # noqa: ANN202
            return json.dumps(make_manifest("9.9.9")).encode("utf-8")

        def fetch_same(url: str, *, timeout: float = 10.0):    # noqa: ANN202
            return json.dumps(make_manifest("1.1.0")).encode("utf-8")

        def fetch_dead(url: str, *, timeout: float = 10.0):    # noqa: ANN202
            raise ValueError("连不上 example.invalid：Name or service not known")

        got = upd.check(tmp, repo="owner/name", fetch_json=fetch_new)
        check("不是 git 检出 → 走 zip 模式", got.mode, "zip")
        check("发现新版", got.behind, True)
        check("当前版本读的是项目根里的 VERSION", got.current, "1.1.0")
        check("远端版本", got.latest, "9.9.9")
        check("原因说得像人话", "9.9.9" in got.reason, True, detail=got.reason)

        upd.forget_cache()
        same = upd.check(tmp, repo="owner/name", fetch_json=fetch_same)
        check("同版本 → 不用更新", (same.ok, same.behind), (True, False))

        upd.forget_cache()
        dead = upd.check(tmp, repo="owner/name", fetch_json=fetch_dead)
        check("网络不通 → ok=False 而不是抛异常", dead.ok, False)
        check("错误信息带上了原因", "Name or service" in dead.error, True, detail=dead.error)

        # 不知道去哪检查时，要把「怎么告诉它」讲出来（★直接调内部函数★：
        # 公开的 check() 会自动用 default_repo() 兜底，根本走不到这个分支）
        nowhere = upd._check_manifest(tmp, "1.1.0", repo="", url="", timeout=1.0,
                                      fetch_json=fetch_new)
        check("没有地址 → 提示用 --url/--repo", "--url" in (nowhere.reason + nowhere.error), True,
              detail=nowhere.reason + nowhere.error)


def section_merge() -> None:
    print("\n[5] 合并发布包：程序文件更新，★用户数据一个都不许动★")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_project(Path(tmpdir), "1.1.0")
        blob = make_release_zip("9.9.9")
        sha = hashlib.sha256(blob).hexdigest()
        rel = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip", sha256=sha),
                                 repo="owner/name")

        lines: list[str] = []
        result = upd.apply(tmp, release=rel, progress=lines.append,
                           fetch_bytes=lambda url, timeout=180.0: blob,
                           install_deps=False, update_exe=False)

        check("更新成功", result.ok, True, detail=result.error)
        check("版本 1.1.0 → 9.9.9", (result.before, result.after), ("1.1.0", "9.9.9"))
        check("main.py 换成了新版的", (tmp / "main.py").read_text(encoding="utf-8"),
              "# v9.9.9\nprint('hi')\n")
        check("★新文件进来了★", (tmp / "voice_loop" / "brand_new.py").is_file(), True)
        check("新增列表里有它", "voice_loop/brand_new.py" in result.added, True)
        check("更新列表里有 main.py", "main.py" in result.updated, True)

        check("★★config.toml 没被覆盖★★",
              (tmp / "config.toml").read_text(encoding="utf-8").endswith("# ★我的配置★\n"), True,
              detail=(tmp / "config.toml").read_text(encoding="utf-8")[-40:])
        check("★★data/memos.json 没被覆盖★★",
              (tmp / "data" / "memos.json").read_text(encoding="utf-8"), '["买牛奶"]\n')
        check("发布包里的 data/ 也没被预先进来",
              (tmp / "data" / "personas").is_dir(), True)

        check("备份先做了", bool(result.backup), True, detail=result.backup)
        check("备份目录真的存在", (tmp / result.backup).is_dir(), True)
        check("备份里有 MANIFEST", (tmp / result.backup / "MANIFEST.txt").is_file(), True)
        check("备份里有 RESTORE 说明", (tmp / result.backup / "RESTORE.txt").is_file(), True)
        check("★备份里没有 models/ 与 sessions/★",
              "models" in (tmp / result.backup / "MANIFEST.txt").read_text(encoding="utf-8"), False)
        check("上游删掉的文件只报告、不动手",
              ("voice_loop/gone.py" in result.removed_upstream,
               (tmp / "voice_loop" / "gone.py").is_file()), (True, True))
        check("进度日志有内容", len(lines) >= 3, True, detail=str(len(lines)))

        # ★同一个包再合一次：应当「什么都不变」（幂等，不然每次更新都像动了几百个文件）
        again = upd.apply(tmp, release=rel, progress=lambda _l: None,
                          fetch_bytes=lambda url, timeout=180.0: blob,
                          install_deps=False, update_exe=False, backup=False)
        check("再合并一次：没有新增", again.added, [])
        check("再合并一次：没有更新", again.updated, [])


def section_verify() -> None:
    print("\n[6] ★校验不过就停手★：sha256 不对时一个文件都不许写")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_project(Path(tmpdir), "1.1.0")
        before = (tmp / "main.py").read_text(encoding="utf-8")
        blob = make_release_zip("9.9.9")
        rel = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip",
                                               sha256="b" * 64))  # 故意不对
        result = upd.apply(tmp, release=rel, progress=lambda _l: None,
                           fetch_bytes=lambda url, timeout=180.0: blob,
                           install_deps=False, update_exe=False, backup=False)
        check("报告失败", result.ok, False)
        check("说清了是校验不过", "校验" in result.error, True, detail=result.error)
        check("★main.py 没被动★", (tmp / "main.py").read_text(encoding="utf-8"), before)
        check("★没有新文件进来★", (tmp / "voice_loop" / "brand_new.py").exists(), False)

        # zip-slip：成员名带 ../ 的包直接拒
        bad = make_release_zip("9.9.9", evil=True)
        rel2 = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip",
                                                sha256=hashlib.sha256(bad).hexdigest()))
        result2 = upd.apply(tmp, release=rel2, progress=lambda _l: None,
                            fetch_bytes=lambda url, timeout=180.0: bad,
                            install_deps=False, update_exe=False, backup=False)
        check("带 ../ 的包被拒", result2.ok, False, detail=result2.error[:80])
        check("★项目外面没多出文件★", (Path(tmpdir).parent / "escaped.py").exists(), False)

        # 不是 zip 的下载内容
        rel3 = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip"))
        result3 = upd.apply(tmp, release=rel3, progress=lambda _l: None,
                            fetch_bytes=lambda url, timeout=180.0: b"<html>404</html>",
                            install_deps=False, update_exe=False, backup=False)
        check("下载到的不是 zip → 失败而不是崩", result3.ok, False)


def section_dry_run() -> None:
    print("\n[7] 试运行：只列步骤（一个文件都不动）")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_project(Path(tmpdir), "1.1.0")
        before = sorted(p.name for p in tmp.iterdir())
        rel = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip"))

        def boom(url: str, *, timeout: float = 10.0):    # noqa: ANN202
            raise AssertionError("试运行不该去下载！")

        result = upd.apply(tmp, release=rel, progress=lambda _l: None, dry_run=True,
                           fetch_bytes=boom, install_deps=False, update_exe=False)
        check("试运行算成功", result.ok, True)
        check("列了步骤（还提到会跳过 data/ 与 config.toml）",
              any("跳过" in line for line in result.lines), True, detail=str(result.lines)[:120])
        check("★没下载、没写盘★", sorted(p.name for p in tmp.iterdir()), before)
        check("没做备份", (tmp / "data" / "backup").exists(), False)

        plan = upd.plan_update(tmp, rel)
        check("计划里说了先备份", any("备份" in line for line in plan), True)


def section_exe_swap() -> None:
    print("\n[8] 换掉 exe 自己：先改名，再写新文件")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        app = Path(tmpdir) / "local-assistant-console.exe"
        app.write_bytes(b"OLD")
        old = upd.replace_exe(app, b"NEW")
        check("新内容就位", app.read_bytes(), b"NEW")
        check("旧文件被改名留着（删不掉就说明没人守着它）", old.read_bytes(), b"OLD")
        check("旧文件名带 .old", old.name.endswith(".old"), True, detail=old.name)
        check("清理掉旧的", upd.cleanup_old_exe(app), [old.name])
        check("清理后旧文件没了", old.exists(), False)

        # 没有 exe（源码运行时）时不该乱动
        check("不是 exe 时 cleanup 返回空", upd.cleanup_old_exe(None), [])


def section_updater() -> None:
    print("\n[9] 界面用的异步更新器（状态机：跑 → 完成，日志能轮询出来）")
    from voice_loop import update as upd

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_project(Path(tmpdir), "1.1.0")
        blob = make_release_zip("9.9.9")
        rel = upd.parse_manifest(make_manifest("9.9.9", url="https://x/y.zip",
                                                sha256=hashlib.sha256(blob).hexdigest()))
        got = upd.Check(ok=True, current="1.1.0", latest="9.9.9", behind=True, mode="zip",
                        release=rel, reason="有新版本", checked_at=time.time())
        # ★下载函数从构造函数注入★（不是去改模块全局）：这样这个状态机也能离线自测
        up = upd.Updater(tmp, fetch_bytes=lambda url, timeout=180.0: blob)
        check("一开始没在跑", up.snapshot()["running"], False)

        real_pip = upd.setup_flow.pip_install
        upd.setup_flow.pip_install = lambda **kw: (0, "（假的，不装）")
        try:
            started, _msg = up.start(check_result=got, install_deps=True, update_exe=False)
            check("启动成功", started, True)
            started2, why = up.start(check_result=got)
            check("重复点会被挡住（同时只能跑一个）", (started2, "已经在更新" in why), (False, True))
            deadline = time.time() + 30
            while time.time() < deadline and up.snapshot()["running"]:
                time.sleep(0.05)
            snap = up.snapshot()
            check("跑完了", snap["running"], False)
            check("结果是成功", snap["ok"], True, detail=str(snap.get("error")))
            check("日志有内容", len(snap["lines"]) >= 3, True, detail=str(len(snap["lines"])))
            check("快照里带上了结果", bool(snap["result"]), True)
            check("结果显示更新成功", (snap["result"] or {}).get("ok"), True)
            # since 参数（前端只取新增的行）
            check("since 只回新行", len(up.snapshot(limit=1)["lines"]), 1)
            check("真更新完了", (tmp / "main.py").read_text(encoding="utf-8"),
                  "# v9.9.9\nprint('hi')\n")
        finally:
            upd.setup_flow.pip_install = real_pip


def main() -> int:
    print("=" * 70)
    print(" 更新自测（★不联网★：下载与取清单都是注进来的假函数）")
    print("=" * 70)
    section_versions()
    section_repo()
    section_manifest()
    section_check()
    section_merge()
    section_verify()
    section_dry_run()
    section_exe_swap()
    section_updater()
    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
