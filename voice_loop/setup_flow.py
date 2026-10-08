"""安装 / 体检 / 升级：把「部署这件事」从一堆手工步骤收成一条命令。

    python main.py setup      环境 → 依赖 → 配置 → 模型 → 自检 → 下一步（幂等，可反复跑）
    python main.py doctor     只读体检（不下载、不写文件、不起进程）
    python main.py upgrade    拉新版本；★动手前先把你的数据备份到 data/backup/★

★为什么是 Python 而不是 shell★：Windows 上 .cmd/.ps1 的中文、引号、编码，
这个仓库已经踩过五次以上（见工程日志）。所以 ``install.ps1`` 只做最难用 Python 做的
那一步（建 venv + pip install + 回调本模块），其余判断全在这里 —— 离线的自测能钉住它们。

★本模块只依赖标准库★：依赖还没装齐时它也要能跑起来，因为这正是 ``setup`` 要解决的问题。
真正的体检交给 ``scripts/check_deploy.py``（它本来就是只读、几秒）—— ★判断只有一份实现★，
这里只负责把它的话翻译成「下一步该敲什么」。

★升级的安全边界（三条，都是血换来的）★

1. **动手前先备份你的数据**：``data/`` 下的 json / personas / knowledge / memory 与
   ``config.toml`` 一起拷到 ``data/backup/upgrade-<时间>-<sha>/``，并写一份 MANIFEST
   （每个文件的 sha256）与 RESTORE.txt（不依赖本工具的还原命令）。备份是**升级的前置条件**，
   不是「顺便做一下」：升级能回滚，数据不能靠猜。
2. **工作区脏就停**：有未提交改动时 ``git pull`` 的结果不可预测，直接拦下并告诉你怎么保命
   （``git stash``）。想强行继续得显式 ``--allow-dirty``。
3. **失败一定给出回去的路**：任何一步失败都打印 ``git reset --hard <升级前的 sha>``
   与备份目录路径 —— 不让人在坏掉的状态里自己想办法。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def child_env() -> dict:
    """跑子进程时统一带上 UTF-8（★这是这个仓库的老坑，栽过不止一次★）。

    Windows 把 stdout 接进管道时，子进程默认按 cp936（GBK）编码输出，
    父进程按 utf-8 解码 —— 中文全变 `?`，**而且不报错**，静悄悄地坏
    （第一次接上 doctor 时，体检里那条中文警告就是这么变成 `????` 的）。
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUTF8"] = "1"      # 不用 setdefault：明确压掉用户环境里的 0
    return env

# --------------------------------------------------------------------------- #
# 常量（★自测会盯着它们与真实来源一致★）
# --------------------------------------------------------------------------- #
MIN_PYTHON = (3, 11)
"""tomllib 是 3.11 进标准库的，而 `voice_loop/settings.py` 直接用了它。"""

MODEL_GROUPS = ("sensevoice", "vad", "piper", "zipvoice", "speaker")
"""`scripts/download_models.py --only` 认的组（自测会去解析那个脚本，防两边跑偏）。"""

DEFAULT_MODEL_GROUPS = ("sensevoice", "vad", "piper")
"""不指定时下哪些：跑起来的最小集（与 download_models.py 的默认一致）。"""

BACKUP_PATTERNS = (
    "data/*.json",          # 日程/备忘/角色索引/唤醒词…
    "data/*.bak",           # 迁移前的旧文件
    "data/personas/**/*",   # 人格文件 + 素材清单 + wav
    "data/knowledge/**/*",  # 世界观 / 资料
    "data/memory/**/*",     # 记忆
    "config.toml",          # ★也是你的数据★：端口、模型路径、角色默认值都在这
)
BACKUP_DIR = "data/backup"
SKIP_SUFFIXES = (".lock", ".tmp")
"""跨进程锁与原子写的临时文件：备份它们没意义（还会把锁带进备份）。"""


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_MARK = {OK: "[ OK ]", WARN: "[警告]", FAIL: "[失败]", INFO: "[提示]"}


@dataclass
class Step:
    """一件事的结论：`level` 决定它是「能用了」还是「得先修」。"""

    name: str
    level: str = INFO
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, level: str = INFO, detail: str = "", fix: str = "") -> Step:
        step = Step(name, level, detail, fix)
        self.steps.append(step)
        return step

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.level == FAIL]

    @property
    def warnings(self) -> list[Step]:
        return [s for s in self.steps if s.level == WARN]

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self, *, indent: str = "  ") -> str:
        lines = []
        for step in self.steps:
            line = f"{indent}{_MARK.get(step.level, '[??]')} {step.name}"
            if step.detail:
                line += f"　{step.detail}"
            lines.append(line)
            if step.fix:
                lines.append(f"{indent}       → {step.fix}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "steps": [dataclasses.asdict(s) for s in self.steps],
                "failed": [s.name for s in self.failed]}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- #
# 环境判定（纯函数 → 自测直接喂假数据）
# --------------------------------------------------------------------------- #
def python_state(version_info: tuple | None = None, executable: str = "") -> Step:
    """Python 版本够不够（这里做纯判断，方便自测喂 3.10）。"""
    ver = tuple(version_info or sys.version_info)[:3]
    exe = executable or sys.executable
    if ver < MIN_PYTHON:
        return Step(
            f"Python {ver[0]}.{ver[1]}.{ver[2]}", FAIL,
            f"需要 ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            f"装个新版 Python 再用它建 venv：{MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上",
        )
    return Step(f"Python {ver[0]}.{ver[1]}.{ver[2]}", OK, exe)


def path_state(root: str | Path) -> Step:
    """项目路径里有没有空格 / 非 ASCII —— 原生库（PortAudio / onnxruntime）最容易栽这儿。"""
    text = str(root)
    problems = []
    if " " in text:
        problems.append("有空格")
    if not text.isascii():
        problems.append("有中文或其它非 ASCII 字符")
    if problems:
        return Step(
            "项目路径", WARN, f"{text}（{'、'.join(problems)}）",
            "原生库偶发加载失败；换到 D:\\local_AI 这种纯英文无空格的路径最省事",
        )
    return Step("项目路径", OK, text)


# --------------------------------------------------------------------------- #
# 体检：复用 scripts/check_deploy.py（判断只有一份实现）
# --------------------------------------------------------------------------- #
def doctor_json(*, strict: bool = False, timeout: float = 180.0) -> dict:
    """调体检脚本拿 JSON。脚本自己崩了也要有话说 —— 不返回空 dict 装死。"""
    script = ROOT / "scripts" / "check_deploy.py"
    if not script.is_file():
        return {"error": f"找不到 {script}", "items": [], "failed": []}
    cmd = [sys.executable, str(script), "--json"] + (["--strict"] if strict else [])
    try:
        got = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout,
                             encoding="utf-8", errors="replace", env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "items": [], "failed": []}
    text = (got.stdout or "").strip()
    if not text:
        return {"error": f"体检脚本没有输出（退出码 {got.returncode}）：{(got.stderr or '')[-300:]}",
                "items": [], "failed": []}
    import json

    try:
        data = json.loads(text)
    except ValueError as exc:
        return {"error": f"体检输出不是 JSON：{exc}", "items": [], "failed": []}
    data["exit_code"] = got.returncode
    return data


def doctor_report(*, strict: bool = False) -> Report:
    """把体检结果翻译成一份 Report（失败项带上「怎么办」）。"""
    rep = Report()
    data = doctor_json(strict=strict)
    if data.get("error"):
        rep.add("体检", FAIL, data["error"], "python scripts/check_deploy.py 看看它的原始输出")
        return rep
    fails = data.get("failed") or []
    warns = [i for i in (data.get("items") or []) if i.get("level") == WARN]
    rep.add("体检", OK if not fails else FAIL,
            f"{len(data.get('items') or [])} 项检查，{len(fails)} 项要处理",
            "" if not fails else "下面这几条是拦路的")
    for item in fails:
        rep.add(f"{item.get('section', '')} · {item.get('name', '')}", FAIL,
                item.get("detail", ""), "")
    for item in warns[:6]:
        rep.add(f"{item.get('section', '')} · {item.get('name', '')}", WARN,
                item.get("detail", ""), "")
    return rep


# --------------------------------------------------------------------------- #
# 模型下载 / 依赖安装（都是调现成脚本，不重写一遍）
# --------------------------------------------------------------------------- #
def model_groups_known() -> tuple[str, ...]:
    """从 download_models.py 里读出它认的组（★防止这里的常量跟那边跑偏★）。"""
    script = ROOT / "scripts" / "download_models.py"
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return MODEL_GROUPS
    hit = re.search(r"choices=\[([^\]]+)\]", text)
    if not hit:
        return MODEL_GROUPS
    return tuple(re.findall(r'"([a-z0-9_]+)"', hit.group(1))) or MODEL_GROUPS


def download_models(groups: list[str] | tuple[str, ...] | None = None, *,
                    force: bool = False, timeout: float = 3600.0) -> tuple[int, str]:
    """下载模型（幂等：已经在的直接跳过）。返回 (退出码, 输出尾巴)。"""
    chosen = list(groups or DEFAULT_MODEL_GROUPS)
    script = ROOT / "scripts" / "download_models.py"
    cmd = [sys.executable, str(script), "--only", *chosen] + (["--force"] if force else [])
    try:
        got = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout,
                             encoding="utf-8", errors="replace", env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    tail = "\n".join(((got.stdout or "") + (got.stderr or "")).strip().splitlines()[-6:])
    return got.returncode, tail


def pip_install(*, timeout: float = 1800.0) -> tuple[int, str]:
    """装/更新依赖：用的是**当前这个解释器**，也就是你跑 main.py 的那个。"""
    req = ROOT / "requirements.txt"
    if not req.is_file():
        return 1, f"找不到 {req}"
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    try:
        got = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout,
                             encoding="utf-8", errors="replace", env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    tail = "\n".join(((got.stdout or "") + (got.stderr or "")).strip().splitlines()[-4:])
    return got.returncode, tail


# --------------------------------------------------------------------------- #
# git（升级要用的那几个动作，都写成小函数方便自测）
# --------------------------------------------------------------------------- #
def git(args: list[str], *, cwd: str | Path = ROOT, timeout: float = 60.0
        ) -> tuple[int, str, str]:
    try:
        got = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                             timeout=timeout, encoding="utf-8", errors="replace",
                             env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return got.returncode, (got.stdout or "").strip(), (got.stderr or "").strip()


def is_git(root: str | Path = ROOT) -> bool:
    return git(["rev-parse", "--is-inside-work-tree"], cwd=root)[0] == 0


def head(root: str | Path = ROOT) -> str:
    rc, out, _ = git(["rev-parse", "HEAD"], cwd=root)
    return out if rc == 0 else ""


def is_dirty(root: str | Path = ROOT) -> bool:
    rc, out, _ = git(["status", "--porcelain"], cwd=root)
    return rc == 0 and bool(out)


def dirty_files(root: str | Path = ROOT, limit: int = 8) -> list[str]:
    rc, out, _ = git(["status", "--porcelain"], cwd=root)
    return [ln for ln in out.splitlines()[:limit]] if rc == 0 else []


def current_branch(root: str | Path = ROOT) -> str:
    rc, out, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    return out if rc == 0 and out != "HEAD" else ""


def remote_url(root: str | Path = ROOT, remote: str = "origin") -> str:
    rc, out, _ = git(["remote", "get-url", remote], cwd=root)
    return out if rc == 0 else ""


def fetch(root: str | Path = ROOT, remote: str = "origin",
          timeout: float = 120.0) -> tuple[bool, str]:
    rc, _, err = git(["fetch", "--prune", remote], cwd=root, timeout=timeout)
    return rc == 0, err[-300:]


def behind(root: str | Path = ROOT, branch: str = "main", remote: str = "origin"
           ) -> tuple[int, str]:
    """落后几个提交（比 `git pull` 安全：只数，不动）。返回 (个数, 人话)。"""
    ref = f"{remote}/{branch}"
    rc, out, err = git(["rev-list", "--count", f"HEAD..{ref}"], cwd=root)
    if rc != 0:
        return -1, err or f"没有 {ref} 这个引用（远端分支不对？）"
    try:
        n = int(out)
    except ValueError:
        return -1, f"看不懂 git 的输出：{out!r}"
    if n == 0:
        return 0, f"和 {ref} 一致"
    rc2, log, _ = git(["log", "--oneline", f"HEAD..{ref}"], cwd=root)
    sample = " / ".join(ln.split(" ", 1)[-1][:40] for ln in (log or "").splitlines()[:3])
    return n, f"{n} 个新提交：{sample}"


# --------------------------------------------------------------------------- #
# 备份 / 还原（升级的前置条件）
# --------------------------------------------------------------------------- #
def backup_candidates(root: str | Path = ROOT) -> list[Path]:
    """该备份哪些文件：**用户数据**（不含 models/、sessions/、备份目录自己）。"""
    root = Path(root)
    found: dict[str, Path] = {}
    for pattern in BACKUP_PATTERNS:
        for path in root.glob(pattern):
            if not path.is_file() or path.suffix in SKIP_SUFFIXES:
                continue
            if BACKUP_DIR in path.as_posix():
                continue
            found[path.relative_to(root).as_posix()] = path
    return [found[k] for k in sorted(found)]


def backup_user_data(root: str | Path = ROOT, *, reason: str = "", tag: str = ""
                     ) -> tuple[Path, int, int]:
    """把用户数据拷到 `data/backup/<reason>-<时间>[-<tag>]/`，并写 MANIFEST + RESTORE。

    返回 `(备份目录, 文件数, 字节数)`。★这份备份不依赖本工具★：MANIFEST.txt 里有每个
    文件的 sha256，RESTORE.txt 里是能直接粘进 PowerShell / bash 的还原命令。
    """
    root = Path(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "-".join(x for x in (reason or "backup", stamp, tag) if x)
    target = root / BACKUP_DIR / name
    target.mkdir(parents=True, exist_ok=True)

    files = backup_candidates(root)
    total = 0
    lines = [f"# 备份 {name}", f"# 时间 {datetime.now().isoformat(timespec='seconds')}",
             f"# 版本 {_version()}", f"# git  {head(root)[:12]}", f"# 原因 {reason or '手动'}", "",
             f"{'sha256':<64}  字节  文件"]
    for src in files:
        rel = src.relative_to(root)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        data = src.read_bytes()
        total += len(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {len(data):>8}  {rel.as_posix()}")
    (target / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "RESTORE.txt").write_text(
        "\n".join([
            f"# 还原 {name}：把这里的东西拷回项目根目录（原样覆盖）",
            "# Windows PowerShell（在项目根目录执行，注意 <项目目录> 换成你的路径）：",
            f"  Copy-Item -Recurse -Force \"{target}\"\\* .",
            "# macOS / Linux：",
            f"  cp -a \"{target}\"/. .",
            "# 说明：MANIFEST.txt 里每个文件的 sha256 可以用来核对拷回来的对不对；",
            "#      这个备份**不含** models/（模型能重新下载）与 sessions/（运行日志）。",
        ]) + "\n", encoding="utf-8")
    return target, len(files), total


def latest_backup(root: str | Path = ROOT) -> Path | None:
    base = Path(root) / BACKUP_DIR
    if not base.is_dir():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def restore_user_data(root: str | Path = ROOT, backup: str | Path | None = None, *,
                      dry_run: bool = True) -> list[str]:
    """把备份拷回项目根。默认 **dry-run**（只列会覆盖什么），`dry_run=False` 才真动。"""
    root = Path(root)
    src = Path(backup) if backup else latest_backup(root)
    if src is None or not src.is_dir():
        raise ValueError("找不到备份目录（先看看 data/backup/ 里有什么）")
    changed: list[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name in ("MANIFEST.txt", "RESTORE.txt"):
            continue
        rel = path.relative_to(src)
        dst = root / rel
        if dst.is_file() and dst.read_bytes() == path.read_bytes():
            continue
        changed.append(rel.as_posix())
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
    return changed


def _version() -> str:
    from . import __version__  # noqa: PLC0415 - 延迟导入，避免与包初始化互相依赖

    return __version__


# --------------------------------------------------------------------------- #
# 服务是否在跑（升级前最好停掉）
# --------------------------------------------------------------------------- #
def service_running(settings=None) -> tuple[bool, str]:
    """唤醒服务在跑吗（升级前最好停掉 —— 它读的是旧源码与旧配置）。"""
    try:
        from . import service_ctl  # noqa: PLC0415

        if settings is None:
            from .settings import load_settings  # noqa: PLC0415

            settings = load_settings()
        st = service_ctl.status(settings)
        return bool(st.running), (f"PID {st.pid}" if st.running else "")
    except Exception as exc:  # noqa: BLE001 - 读不到状态不该拦住升级
        return False, f"（状态读不到：{type(exc).__name__}）"


# --------------------------------------------------------------------------- #
# 高层流程
# --------------------------------------------------------------------------- #
def run_setup(*, model_groups: list[str] | tuple[str, ...] | None = None,
              skip_deps: bool = False, skip_models: bool = False,
              strict: bool = False, echo=print) -> Report:
    """一条命令把机器准备好：环境 → 依赖 → 配置 → 模型 → 自检。

    ★幂等★：每步都自己判断「已经好了就跳过」，所以随时可以再跑一遍。
    ★不动你的数据★：只装依赖、下模型、读体检；人格文件/日程/设置一个字都不改。
    """
    rep = Report()

    # 1) 环境
    rep.steps.append(python_state())
    rep.steps.append(path_state(ROOT))

    # 2) 配置：几个命令都要求 config.toml 在（它在 git 里，压缩包里也带着）
    cfg = ROOT / "config.toml"
    if cfg.is_file():
        rep.add("配置文件 config.toml", OK, f"{cfg.name} · {human(cfg.stat().st_size)}")
    else:
        rep.add("配置文件 config.toml", FAIL, "没有这个文件，其它命令都跑不起来",
                "从发布包/仓库里拿回来：git checkout -- config.toml"
                "（压缩包安装的就把包里的 config.toml 解出来放回项目根）")

    # 3) 依赖（体检里的第 2 项就在查它；--skip-deps 时只在报告里说结论）
    if skip_deps:
        rep.add("依赖", INFO, "按要求跳过了（--skip-deps）")
    else:
        data = doctor_json()
        deps = [i for i in (data.get("items") or []) if str(i.get("section", "")).startswith("2")]
        bad = [i for i in deps if i.get("level") == FAIL]
        if not deps:
            rep.add("依赖", WARN, "体检没给出依赖结论",
                    "python scripts/check_deploy.py 看原始输出")
        elif bad:
            names = "、".join(str(i.get("name", "")) for i in bad[:4])
            rc, tail = pip_install()
            rep.add("依赖", OK if rc == 0 else FAIL,
                    f"缺 {len(bad)} 个（{names}），已尝试 pip install -r requirements.txt",
                    "" if rc == 0 else f"装失败了（退出码 {rc}）：{tail}\n"
                                       f"        → 换镜像再试：{Path(sys.executable).name} -m pip install -r "
                                       f"requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
        else:
            rep.add("依赖", OK, f"{len(deps)} 项都在")

    # 4) 模型
    if skip_models:
        rep.add("模型", INFO, "按要求跳过了（--skip-models）")
    else:
        groups = list(model_groups or DEFAULT_MODEL_GROUPS)
        echo(f"  正在检查/下载模型：{'、'.join(groups)}（已在的直接跳过）…")
        rc, tail = download_models(groups)
        rep.add("模型", OK if rc == 0 else FAIL,
                f"{'、'.join(groups)} 已就绪" if rc == 0 else "下载失败",
                "" if rc == 0 else f"退出码 {rc}：{tail}\n"
                                   f"        → 也可以单独下：python scripts/download_models.py --only "
                                   f"{' '.join(groups)}")

    # 5) 自检（这一步才是「能不能用」的结论）
    echo("  正在自检…")
    rep.steps.extend(doctor_report(strict=strict).steps)

    # 6) 下一步
    if rep.ok:
        rep.add("下一步", OK, "python main.py listen（前台试一次）"
                              " · python main.py ui（网页控制台）· python main.py selftest")
    else:
        rep.add("下一步", FAIL, f"还有 {len(rep.failed)} 项要处理（见上面标 [失败] 的行）",
                "修完再跑一遍 python main.py setup —— 它是幂等的")
    return rep


def upgrade_plan(root: str | Path = ROOT, *, remote: str = "origin",
                 branch: str = "") -> Report:
    """只看看「要不要升级、能不能升」（不 fetch 之外任何写操作）。"""
    root = Path(root)
    rep = Report()
    if not is_git(root):
        rep.add("这份不是 git 检出", INFO,
                "大概是下载的压缩包安装",
                "升级 = 下载新版压缩包，把里面的文件解压覆盖过去；"
                "★config.toml 与 data/ 是你的数据，覆盖时别删★（新版里如果多了配置项，"
                "照 README 的升级说明补一下）")
        return rep
    rep.add("版本", INFO, f"{_version()} · {head(root)[:8]}"
                         f"（分支 {current_branch(root) or '游离 HEAD'}）")
    if is_dirty(root):
        rep.add("工作区", FAIL, f"有 {len(dirty_files(root))}+ 处未提交改动",
                "先 git stash push -u（能捞回来）或 git commit，再升级")
    n, why = behind(root, branch or (current_branch(root) or "main"), remote)
    if n < 0:
        rep.add("远端", WARN, why, f"先 git fetch {remote}；或检查 --remote/--branch")
    elif n == 0:
        rep.add("升级", OK, "已经是最新版本", why)
    else:
        rep.add("升级", INFO, f"可以拉到 {n} 个新提交", why)
        rep.add("要动手的话", INFO, "python main.py upgrade --apply",
                "它会先把你 data/ 与 config.toml 备份到 data/backup/，再 git pull --ff-only")
    return rep


def run_upgrade(*, apply: bool = False, remote: str = "origin", branch: str = "",
                skip_deps: bool = False, allow_dirty: bool = False,
                model_groups: list[str] | tuple[str, ...] | None = None,
                root: str | Path = ROOT, echo=print) -> Report:
    """升级：检查 → 备份 → 拉取 → 依赖 → 模型 → 自检；失败一定给回滚的路。

    默认 **dry-run**（`apply=False` 只报告）；`--apply` 才真动。
    """
    root = Path(root)
    rep = Report()
    if not is_git(root):
        rep.steps.extend(upgrade_plan(root, remote=remote, branch=branch).steps)
        return rep

    before = head(root)
    ref_branch = branch or (current_branch(root) or "main")
    rep.add("升级前", INFO, f"{_version()} · {before[:8]} · 分支 {ref_branch}")

    if is_dirty(root) and not allow_dirty:
        rep.add("工作区有未提交改动", FAIL, "；".join(dirty_files(root, 3)),
                "先 git stash push -u 或 git commit；确认要硬来就加 --allow-dirty")
        return rep

    ok, err = fetch(root, remote)
    if not ok:
        rep.add("git fetch", FAIL, err or "拉不到远端",
                f"检查网络/远端：git remote -v（当前 {remote_url(root, remote) or '没有配置'}）")
        return rep

    n, why = behind(root, ref_branch, remote)
    if n < 0:
        rep.add("远端分支", FAIL, why, "换一个分支名：--branch main")
        return rep
    if n == 0:
        rep.add("升级", OK, "已经是最新版本", why)
        return rep
    rep.add("发现新版本", INFO, f"{n} 个新提交", why)

    if not apply:
        rep.add("这是试运行", INFO, "加 --apply 才会真的升",
                "python main.py upgrade --apply")
        return rep

    running, detail = service_running()
    if running:
        rep.add("提醒：唤醒服务在跑", WARN, detail,
                "建议先 python main.py stop（升级会换源码与配置，跑着的进程读的是旧文件）")

    bak, files, size = backup_user_data(root, reason="upgrade", tag=before[:8])
    rep.add("数据已备份", OK, bak.relative_to(root).as_posix(), f"{files} 个文件 · {human(size)}")

    req_before = _hash_file(root / "requirements.txt")
    rc, out, err = git(["pull", "--ff-only", remote, ref_branch], cwd=root, timeout=300)
    if rc != 0:
        rep.add("git pull", FAIL, err[-300:] or out[-300:],
                f"★回滚★：git reset --hard {before}（数据在 {bak.relative_to(root).as_posix()}，"
                f"要还原看里面的 RESTORE.txt）")
        return rep
    after = head(root)
    rep.add("代码已更新", OK, f"{before[:8]} → {after[:8]}", f"回滚用：git reset --hard {before}")

    if skip_deps:
        rep.add("依赖", INFO, "按要求跳过了（--skip-deps）")
    elif _hash_file(root / "requirements.txt") != req_before:
        rep.add("依赖清单变了", INFO, "requirements.txt 有新内容，正在 pip install…")
        rc2, tail = pip_install()
        rep.add("依赖", OK if rc2 == 0 else FAIL, "" if rc2 == 0 else tail,
                "" if rc2 == 0 else "手动装：python -m pip install -r requirements.txt "
                                    "-i https://pypi.tuna.tsinghua.edu.cn/simple")
    else:
        rep.add("依赖", OK, "requirements.txt 没变，跳过")

    if model_groups:
        rc3, tail = download_models(list(model_groups))
        rep.add("模型", OK if rc3 == 0 else FAIL, tail if rc3 else "按要求补下了模型",
                "" if rc3 == 0 else "python scripts/download_models.py 看看是哪一步不行")
    else:
        rep.add("模型", INFO, "没有补下模型（新版如果需要新模型，下面自检会指出来）")

    echo("  升级后自检…")
    rep.steps.extend(doctor_report().steps)

    if rep.ok:
        rep.add("下一步", OK, "python main.py listen（或 python main.py ui）")
    else:
        rep.add("下一步", FAIL, f"还有 {len(rep.failed)} 项要处理",
                f"要么按上面的提示修，要么回到升级前：git reset --hard {before}"
                f"（数据还原看 {bak.relative_to(root).as_posix()}/RESTORE.txt）")
    return rep


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
