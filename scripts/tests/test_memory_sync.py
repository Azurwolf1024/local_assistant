"""记忆云端同步自测：结构 / 镜像 / 合并 / 隐私闸门（★不联网★，用 file:// 当远端）。

为什么要一条条钉住：这个功能会**把隐私数据推到网上**。所以这里全程用临时目录 +
一个本地 bare 仓库当远端，最后还有一道闸门：跑完比对真实 `data/memory/` 的字节数。

    python scripts\\tests\test_memory_sync.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.memory import sync as msync          # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []
_UNSET = object()


def check(name: str, got, want=_UNSET, detail: str = "") -> None:
    ok = bool(got) if want is _UNSET else got == want
    tail = "" if ok or want is _UNSET else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    got = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return got.returncode, (got.stdout or "").strip()


def git_dir(gitdir: Path, args: list[str]) -> tuple[int, str]:
    """★bare 仓库要用 --git-dir 显式指过去★：新版 git 的
    `safe.bareRepository=explicit` 会拒绝「cd 进 bare 目录再跑 git」。"""
    got = subprocess.run(["git", f"--git-dir={gitdir}", *args], cwd=str(gitdir.parent),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return got.returncode, (got.stdout or "").strip()


def ident(path: Path) -> None:
    """给临时仓库一个身份（这台机器的全局 git 身份是空的，不设就 commit 不了）。"""
    git(["config", "user.name", "self-test"], path)
    git(["config", "user.email", "self-test@localhost"], path)


def episode(rid: str, ts: str, title: str, updated: str = "") -> dict:
    return {"id": rid, "ts": ts, "kind": "talk", "title": title, "summary": title,
            "detail": title, "who": [], "keywords": [], "salience": 0.6, "confidence": 0.8,
            "source": "", "turn": 0, "event_id": None, "image": "", "links": [],
            "recalls": 0, "last_recall": "", "merged": 1,
            "created_at": ts, "updated_at": updated or ts}


def fact(key: str, value: str, seen: str, **extra) -> dict:
    row = {"key": key, "value": value, "about": "user", "confidence": 0.6, "evidence": 1,
           "pinned": False, "first_seen": seen, "last_seen": seen, "sources": [], "note": ""}
    row.update(extra)
    return row


def write_memory(root: Path, cid: str, episodes: list[dict], facts: list[dict],
                 sessions: dict) -> None:
    room = root / cid
    room.mkdir(parents=True, exist_ok=True)
    (room / "episodes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in episodes), encoding="utf-8")
    (room / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    (room / "sessions.json").write_text(json.dumps(sessions, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    # 锁文件与临时文件必须被忽略（它们是跨进程锁，不是记忆）
    (room / "facts.json.lock").write_text("", encoding="utf-8")
    (room / "episodes.jsonl.tmp").write_text("", encoding="utf-8")


def settings_for(tmp: Path, *, sync_dir: str, remote: str) -> SimpleNamespace:
    return SimpleNamespace(
        root=str(tmp),
        skills=SimpleNamespace(data_dir="data"),
        memory=SimpleNamespace(enabled=True, dir="", sync_dir=sync_dir, sync_remote=remote,
                               sync_branch="main", sync_timeout=60, sync_on_close=False),
    )


def seed_project(tmp: Path) -> Path:
    memory = tmp / "data" / "memory"
    write_memory(memory, "alpha",
                 [episode("ep-a1", "2026-11-01 10:00:00", "第一天"),
                  episode("ep-a2", "2026-11-02 10:00:00", "第二天")],
                 [fact("user.称呼", "博士", "2026-11-01 10:00:00", pinned=True)],
                 {"session-1.jsonl": {"extracted": True, "extracted_at": "2026-11-01 10:05:00",
                                      "turns": 2}})
    write_memory(memory, "beta",
                 [episode("ep-b1", "2026-11-03 09:00:00", "另一位的记忆")],
                 [fact("user.喜好.饮料", "乌龙茶", "2026-11-03 09:00:00")],
                 {})
    (memory / "alpha" / "knowledge").mkdir(parents=True, exist_ok=True)
    (memory / "alpha" / "knowledge" / "设定.md").write_text("# 设定\n\n海里的鱼。\n", encoding="utf-8")
    (memory / "alpha" / "knowledge" / "跳过.bin").write_bytes(b"\x00\x01")
    return memory


def test_layout(tmp: Path) -> None:
    print("\n[1] 导出：结构照第 15 节（每角色一份），锁文件不上云")
    seed_project(tmp)
    remote = tmp / "remote.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp)
    sync = msync.MemorySync(settings_for(tmp, sync_dir="cloud", remote=remote.as_uri()))
    report = sync.sync()
    clone = tmp / "cloud"
    check("同步成功", report.ok, True, report.error)
    check("推上去了", report.pushed, True)
    check("仓库里的目录结构与本地设计一致",
          sorted(p.name for p in (clone / "memory").iterdir()), ["alpha", "beta"])
    check("L2 情景在", (clone / "memory" / "alpha" / "episodes.jsonl").is_file(), True)
    check("L3 事实在", (clone / "memory" / "alpha" / "facts.json").is_file(), True)
    check("L1 索引在", (clone / "memory" / "alpha" / "sessions.json").is_file(), True)
    check("知识库跟着走", (clone / "memory" / "alpha" / "knowledge" / "设定.md").is_file(), True)
    check("★非文本知识库文件不上云★",
          (clone / "memory" / "alpha" / "knowledge" / "跳过.bin").exists(), False)
    check("★锁文件不上云★", (clone / "memory" / "alpha" / "facts.json.lock").exists(), False)
    check("★临时文件不上云★", (clone / "memory" / "alpha" / "episodes.jsonl.tmp").exists(), False)
    manifest = json.loads((clone / "manifest.json").read_text(encoding="utf-8"))
    check("manifest 记了角色与条数",
          (manifest["counts"]["characters"], manifest["counts"]["episodes"],
           manifest["counts"]["facts"]), (2, 3, 2))
    check("manifest 写明 L1 不同步", "不同步" in manifest["note"], True)
    check("README 建了一次（提醒这是私人仓库）",
          "Private" in (clone / "README.md").read_text(encoding="utf-8"), True)
    check("远端真的收到了（bare 里有提交）", bool(git_dir(remote, ["log", "--oneline"])[1]), True)
    check("提交信息不带本机绝对路径",
          str(tmp) not in git(["log", "-1", "--format=%B"], clone)[1], True)
    # 第二次：内容没变就不该再产生提交
    before = git(["rev-parse", "HEAD"], clone)[1]
    again = sync.sync()
    check("★没变化就不提交★", (again.commit, git(["rev-parse", "HEAD"], clone)[1]), ("", before))
    check("没变化时如实说", any("没有变化" in n for n in again.notes), True)


def test_mirror(tmp: Path) -> None:
    print("\n[2] 本地→云端是镜像：本地删掉的，云端也删（旧值留在 git 历史里）")
    seed_project(tmp)
    remote = tmp / "remote2.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp)
    sync = msync.MemorySync(settings_for(tmp, sync_dir="cloud2", remote=remote.as_uri()))
    sync.sync()
    local = tmp / "data" / "memory" / "alpha" / "episodes.jsonl"
    rows = [json.loads(line) for line in local.read_text(encoding="utf-8").splitlines() if line]
    local.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                             for r in rows if r["id"] != "ep-a2"), encoding="utf-8")
    report = sync.sync()
    cloud_file = tmp / "cloud2" / "memory" / "alpha" / "episodes.jsonl"
    check("云端跟着删", len(cloud_file.read_text(encoding="utf-8").strip().splitlines()), 1)
    check("这次有提交", bool(report.commit), True)
    check("★报告里的文件名没被切掉头一个字母★",
          "memory/alpha/episodes.jsonl" in report.changed, True, report.changed[:3])
    check("删掉的那条在历史里还能捞",
          "ep-a2" in git(["log", "-p", "--all"], tmp / "cloud2")[1], True)


def test_pull(tmp: Path) -> None:
    print("\n[3] 云端→本地是合并：取新、留本地独有的")
    seed_project(tmp)
    remote = tmp / "remote3.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp)
    settings = settings_for(tmp, sync_dir="cloud3", remote=remote.as_uri())
    sync = msync.MemorySync(settings)
    sync.sync()

    # 另一台机器：在同一份云端上加了经历/事实，并改了一条事实
    other = tmp / "other"
    git(["clone", "--branch", "main", remote.as_uri(), str(other)], tmp)
    ident(other)
    room = other / "memory" / "alpha"
    rows = [json.loads(line) for line in (room / "episodes.jsonl").read_text(
        encoding="utf-8").splitlines() if line]
    rows.append(episode("ep-a3", "2026-11-05 10:00:00", "在另一台机器上说的"))
    (room / "episodes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    facts = json.loads((room / "facts.json").read_text(encoding="utf-8"))
    facts = [f for f in facts if f["key"] != "user.称呼"]
    facts.append(fact("user.称呼", "老师", "2026-11-06 08:00:00", pinned=True))
    facts.append(fact("user.习惯.睡觉", "一点睡", "2026-11-06 09:00:00"))
    (room / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    git(["add", "-A"], other)
    git(["commit", "-m", "另一台机器的记忆"], other)
    git(["push", "origin", "HEAD:main"], other)

    report = sync.pull()
    local = tmp / "data" / "memory" / "alpha"
    got_episodes = [json.loads(line) for line in (local / "episodes.jsonl").read_text(
        encoding="utf-8").splitlines() if line]
    ids = [r["id"] for r in got_episodes]
    check("云端的新经历合并进来了", "ep-a3" in ids, True)
    check("本地独有的经历没被删", ids.count("ep-a2"), 1)
    got_facts = {f["key"]: f for f in json.loads((local / "facts.json").read_text(encoding="utf-8"))}
    check("★同一键取新的★", got_facts["user.称呼"]["value"], "老师")
    check("云端新增的事实合并进来", "user.习惯.睡觉" in got_facts, True)
    check("pinned 不会被合并弄丢", got_facts["user.称呼"]["pinned"], True)
    check("合并前留了本地备份",
          any(p.name.startswith("facts.json.bak-sync-") for p in local.iterdir()), True)
    check("pinned 事实的 first_seen 取更早的那次",
          got_facts["user.称呼"]["first_seen"], "2026-11-01 10:00:00")
    check("报告说了改了哪些", any("facts" in c for c in report.changed), True)
    # 再拉一次：都已在本地 → 不该再动文件
    again = sync.pull()
    check("★第二次拉取不再改文件★", again.changed, [])


def test_public_guard(tmp: Path) -> None:
    print("\n[4] 公开仓库闸门：认出来就拦下（记忆是隐私）")
    seed_project(tmp)
    remote = tmp / "remote4.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp)
    sync = msync.MemorySync(settings_for(tmp, sync_dir="cloud4", remote=remote.as_uri()))
    real = msync.remote_visibility
    msync.remote_visibility = lambda url, timeout=5.0: ("public", "★公开仓库★")   # noqa: ARG005
    try:
        report = sync.sync()
    finally:
        msync.remote_visibility = real
    check("拦下了（ok=False）", report.ok, False)
    check("说的是「公开」这件事", "公开" in report.error, True)
    check("★一条都没推上去★", git_dir(remote, ["rev-parse", "--verify", "HEAD"])[0] != 0, True)
    check("但本地照样提交了（历史里有，改成私有后能补推）", bool(report.commit), True)

    real = msync.remote_visibility
    msync.remote_visibility = lambda url, timeout=5.0: ("unknown", "查不到")      # noqa: ARG005
    try:
        fixed = sync.sync()
    finally:
        msync.remote_visibility = real
    check("查不到可见性时不拦（网络问题不该挡住同步）", fixed.ok, True, fixed.error)
    check("补推成功", fixed.pushed, True)


def test_status_and_edges(tmp: Path) -> None:
    print("\n[5] 状态 / 空仓库 / 不是 git 的目录")
    seed_project(tmp)
    empty = tmp / "empty.git"
    git(["init", "--bare", "-b", "main", str(empty)], tmp)
    sync = msync.MemorySync(settings_for(tmp, sync_dir="cloud5", remote=empty.as_uri()))
    report = sync.sync()
    check("★远端是空仓库也能推★", (report.ok, report.pushed), (True, True), report.error)
    check("空仓库那条提示说清楚了", any("空的" in n for n in report.notes), True)
    status = sync.status()
    check("状态：干净", any("未提交的改动 无" in n for n in status.notes), True)
    check("状态里带上条数", status.characters.get("alpha", {}).get("episodes"), 2)

    busy = tmp / "notgit"
    busy.mkdir(parents=True, exist_ok=True)
    (busy / "junk.txt").write_text("x", encoding="utf-8")
    bad = msync.MemorySync(settings_for(tmp, sync_dir="notgit", remote=empty.as_uri()))
    got = bad.sync()
    check("目录里已有非 git 文件 → 明确拒绝而不是乱写", got.ok, False)
    check("拒绝时说清了怎么办", "换个目录" in got.error, True)


def test_guards(tmp: Path) -> None:
    print("\n[5b] 防手滑：本地空目录不动云端 / 云端独有角色不凭空造")
    remote = tmp / "remote5.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp)
    seed_project(tmp)
    settings = settings_for(tmp, sync_dir="c5", remote=remote.as_uri())
    sync = msync.MemorySync(settings)
    sync.sync()

    # 模拟「记忆目录指错了」：本地一个角色都没有
    empty_root = tmp / "empty-data" / "memory"
    empty_root.mkdir(parents=True, exist_ok=True)
    broken = msync.MemorySync(SimpleNamespace(
        root=str(tmp), skills=SimpleNamespace(data_dir="empty-data"),
        memory=SimpleNamespace(enabled=True, dir="", sync_dir="c5", sync_remote=remote.as_uri(),
                               sync_branch="main", sync_timeout=60, sync_on_close=False)))
    got = broken.sync()
    check("★本地没有记忆时拒绝同步（不会把云端清空）★", got.ok, False)
    check("拒绝时说清了怎么办", "pull" in got.error, True)
    check("云端真的一条没少",
          len(git_dir(remote, ["show", "main:memory/alpha/facts.json"])[1]) > 0, True)

    # 云端有、本地没有的角色：只提示，不往本地造目录
    local_room = tmp / "data" / "memory" / "beta"
    shutil.rmtree(local_room)
    pulled = sync.pull()
    check("云端独有角色不凭空出现在本地",
          (tmp / "data" / "memory" / "beta").exists(), False)
    check("但会明确提示", any("beta" in w for w in pulled.warnings), True)
    check("本地还留着的角色照常合并", pulled.ok, True, pulled.error)


def test_gate(tmp: Path) -> None:  # noqa: ARG001
    print("\n[6] 闸门：真实仓库没被动过")
    watched = [ROOT / "data" / "memory",
               ROOT / "config.toml",
               ROOT / "data" / "characters.json"]
    before = {}
    for path in watched:
        if path.is_dir():
            before[path] = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        else:
            before[path] = path.stat().st_size if path.is_file() else -1
    seed_project(tmp)                       # 只在临时目录里造记忆
    after = {}
    for path in watched:
        if path.is_dir():
            after[path] = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        else:
            after[path] = path.stat().st_size if path.is_file() else -1
    for path, size in before.items():
        check(f"{path.relative_to(ROOT).as_posix()} 没被自测改到", after[path], size)
    check("临时目录里真的写了（不是空跑）",
          (tmp / "data" / "memory" / "alpha" / "facts.json").is_file(), True)


def _room(tmp: Path, name: str) -> Path:
    path = tmp / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    if shutil.which("git") is None:
        print("找不到 git —— 这个自测要用它当远端（装 Git for Windows 再跑）")
        return 2
    with tempfile.TemporaryDirectory(prefix="memsync-") as raw:
        tmp = Path(raw)
        test_layout(_room(tmp, "layout"))
        test_mirror(_room(tmp, "m2"))
        test_pull(_room(tmp, "p"))
        test_public_guard(_room(tmp, "g"))
        test_status_and_edges(_room(tmp, "s"))
        test_guards(_room(tmp, "guard"))
        test_gate(_room(tmp, "gate"))
    print("\n" + "=" * 66)
    if _failures:
        print(f"  记忆同步自测：{len(_failures)} 条没过：" + "、".join(_failures[:6]))
        return 1
    print("  记忆同步自测：全部通过 √")
    return 0


if __name__ == "__main__":
    sys.exit(main())
