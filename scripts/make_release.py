"""打一个「能直接下载、能用」的发布包：源码 zip + 校验和 + 更新清单。

为什么要有它：**源码仓库不等于制品**。用户从 Releases 下载的那份东西，应该是
「解压就能跑」的一包文件 —— 里面有 `config.toml`、`requirements.txt`、`install.ps1`、
以及一份能核对完整性的 sha256。这个脚本就负责把仓库变成那一包，并且只用**一个版本号**
（根目录的 `VERSION`）贯穿「压缩包名 / 清单 / 校验和 / 打 tag 的提示」。

    python scripts/make_release.py                    # 打包到 dist/
    python scripts/make_release.py --check            # 只列「会打进去什么」，不写文件
    python scripts/make_release.py --out D:\\dist --notes "修了唤醒词匹配"

★打进去的只有 git 跟踪的文件★（`git ls-files`）—— 于是 `data/`、`models/`、`sessions/`、
`.venv-*` 这些天然被排除，跟你 `.gitignore` 里的判断**永远是同一套**，不会两处打架。
不是 git 检出时退化成「按目录扫 + 一份硬排除表」，并在报告里说清用的是哪种方式。

★它不改你的工作区★：只读，产物写进 `dist/`（已在 .gitignore 里）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop import __version__  # noqa: E402

# 打包时必须有的几个文件：少了就不是「能跑的发布包」（压缩包里也是这几样最要命）
MUST_HAVE = ("main.py", "VERSION", "requirements.txt", "config.toml", "README.md",
             "install.ps1", "voice_loop/__init__.py")

# 不是 git 检出时的兜底排除（按路径片段匹配）
FALLBACK_SKIP = (".git/", ".venv", "venv/", "models/", "sessions/", "dist/", "__pycache__",
                 ".piper-src", ".zipvoice-src", "data/backup/", "data/vision/", "data/memory/",
                 "data/piper/", "data/finetune/", "data/ref_cache/", ".vscode/")


@dataclass
class Plan:
    files: list[str] = field(default_factory=list)
    source: str = ""            # "git ls-files" 还是 "目录扫描"
    note: str = ""


def git(args: list[str]) -> tuple[int, str]:
    try:
        got = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=60,
                             encoding="utf-8", errors="replace")
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return got.returncode, (got.stdout or "").strip()


def collect(plan: Plan) -> Plan:
    """要打进去的文件清单（相对项目根的 posix 路径，已排序）。"""
    # ★带上「已加进来但还没提交」的新文件★：不然刚写的 VERSION / install.ps1
    # 要等到 commit 之后才进得了发布包（而「先打包看看」正是最该试的时候）。
    rc, out = git(["ls-files", "--cached", "--others", "--exclude-standard"])
    if rc == 0 and out:
        plan.files = sorted({ln for ln in out.splitlines() if ln.strip()})
        plan.source = "git ls-files（含未提交的新文件）"
        plan.note = "只有 git 跟踪 / 没被忽略的文件 —— 规则与 .gitignore 完全一致"
        return plan
    files = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(skip in rel for skip in FALLBACK_SKIP):
            continue
        files.append(rel)
    plan.files = files
    plan.source = "目录扫描"
    plan.note = "不是 git 检出（或 git 不可用），按内置排除表扫的；建议用 git 检出打包"
    return plan


def check_plan(plan: Plan) -> list[str]:
    """发布前该拦的：缺关键文件 / 版本号不可用。"""
    problems = []
    if not plan.files:
        problems.append("文件清单是空的（打包出来会是个空壳）")
    for name in MUST_HAVE:
        if name not in plan.files:
            problems.append(f"缺关键文件：{name}")
    if "+unknown" in __version__ or not __version__:
        problems.append(f"版本号不可用（VERSION 文件读不到？现在是 {__version__!r}）")
    for bad, allow in ((".git/", ()), ("models/", ("models/README.md",)),
                       ("sessions/", ()), (".venv", ())):
        for rel in plan.files:
            if rel.startswith(bad) and rel not in allow:
                problems.append(f"清单里混进了不该发布的 {rel}")
    return problems


def sha256_of(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_url_prefix() -> str:
    """从 git remote 推 GitHub Release 的下载前缀（猜不出就留空，别编）。"""
    rc, out = git(["remote", "get-url", "origin"])
    if rc != 0 or "github.com" not in out:
        return ""
    slug = out.rstrip("/").removesuffix(".git")
    slug = slug.split("github.com", 1)[-1].lstrip(":/")
    if "/" not in slug:
        return ""
    return f"https://github.com/{slug}/releases/download/v{__version__}/"


def build(out_dir: Path, plan: Plan, *, notes: str = "", url_prefix: str = "",
          make_zip: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    top = f"local_assistant-{__version__}"
    manifest: dict = {
        "name": "local_assistant",
        "version": __version__,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": ">=3.11",
        "channel": "stable",
        "notes": notes,
        "files": len(plan.files),
        "assets": {},
    }
    rc, sha = git(["rev-parse", "--short", "HEAD"])
    manifest["git"] = sha if rc == 0 else ""

    if make_zip:
        zip_name = f"{top}-source.zip"
        target = out_dir / zip_name
        # ★压缩包里套一层同名目录★：解压不会把几十个文件糊在用户的下载目录里
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in plan.files:
                src = ROOT / rel
                if src.is_file():
                    zf.write(src, f"{top}/{rel}")
            zf.writestr(f"{top}/RELEASE.txt", "\n".join([
                f"local_assistant {__version__}",
                f"打包时间 {manifest['date']}",
                f"来源 {plan.source} · {len(plan.files)} 个文件",
                "",
                "怎么用（详见 README 第 3 节）：",
                "  1) 解压到任意目录（路径尽量别带空格/中文）",
                "  2) Windows 上直接跑：powershell -ExecutionPolicy Bypass -File install.ps1",
                "     其它方式：pip install -r requirements.txt 后 python main.py setup",
                "  3) 装完：python main.py listen（语音服务）/ python main.py ui（网页控制台）",
                "",
                "★数据与配置都在 config.toml 与 data/ 里，升级时覆盖程序文件即可，别删它们★",
            ]) + "\n")
        digest = sha256_of(target)
        (out_dir / f"{zip_name}.sha256").write_text(f"{digest}  {zip_name}\n", encoding="utf-8")
        manifest["assets"]["source-zip"] = {
            "file": zip_name,
            "bytes": target.stat().st_size,
            "sha256": digest,
            "url": (url_prefix + zip_name) if url_prefix else "",
        }

    (out_dir / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def tag_exists(tag: str) -> bool:
    """这个 tag 打过没。★注意★：`git tag --list <不存在的名字>` 退出码是 **0**、
    输出为空 —— 拿退出码当判断会把「没打过」说成「已经打过」（第一版就是这个 bug）。
    """
    rc, out = git(["tag", "--list", tag])
    return rc == 0 and bool(out.strip())


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(description="打包发布（源码 zip + 校验和 + latest.json）")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="产物目录（默认 dist/）")
    ap.add_argument("--check", action="store_true", help="只检查会打进去什么，不写文件")
    ap.add_argument("--no-zip", action="store_true", help="只生成 latest.json")
    ap.add_argument("--notes", default="", help="写进清单的更新说明（一句话）")
    ap.add_argument("--url-prefix", default=None,
                    help="制品下载前缀（默认按 git remote 推 GitHub Releases 的地址）")
    args = ap.parse_args()

    print("=" * 66)
    print(f" 发布打包　local_assistant {__version__}")
    print("=" * 66)

    plan = collect(Plan())
    problems = check_plan(plan)
    print(f"  文件清单：{len(plan.files)} 个（{plan.source}）")
    print(f"  {plan.note}")
    if plan.files:
        print("  前几个：" + "、".join(plan.files[:6]) + ("…" if len(plan.files) > 6 else ""))
    if problems:
        print("\n  × 还不能发布：")
        for item in problems:
            print("    - " + item)
        return 1
    if args.check:
        print("\n  检查通过（--check 只看看）。去掉 --check 就会真的打包。")
        return 0

    url_prefix = release_url_prefix() if args.url_prefix is None else args.url_prefix
    manifest = build(Path(args.out), plan, notes=args.notes, url_prefix=url_prefix,
                     make_zip=not args.no_zip)
    out = Path(args.out)
    print("\n  产物：")
    for asset in manifest["assets"].values():
        print(f"   · {out / asset['file']}　{human(asset['bytes'])}　sha256 {asset['sha256'][:16]}…")
    print(f"   · {out / 'latest.json'}")

    tag = f"v{__version__}"
    print("\n  下一步（发到 GitHub Releases，别人就能一键下载 + 核对校验和）：")
    print(f"   git tag {tag} && git push origin {tag}"
          + ("　★这个 tag 已经存在：改过内容就得先 git tag -d 再重打★" if tag_exists(tag) else ""))
    print(f"   gh release create {tag} {out.name}/*.zip {out.name}/*.sha256 "
          f"{out.name}/latest.json --title \"{tag}\" --notes \"{args.notes or '（写一句更新说明）'}\"")
    if not url_prefix:
        print("   （没有可用的 GitHub remote，latest.json 里的 url 留空了 —— 发布后自己补）")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
