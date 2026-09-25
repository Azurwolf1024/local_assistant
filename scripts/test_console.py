"""控制台端到端自测：**真起一个 HTTP 服务**，用真请求走一遍所有接口（纯离线）。

为什么不用 TestClient：那需要额外装 httpx，而且它不经过真正的 socket/事件循环/
静态文件挂载——SSE 这种「一直挂着慢慢吐」的接口在 TestClient 里测不出真行为。
这里就在后台线程跑真 uvicorn，用 urllib 打真请求。

★全程在临时目录里★（复制一份 config.toml 过去）：绝不碰你真实的
data/events.json、data/memos.json、sessions/ 里的东西。

    python scripts/test_console.py
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


# ---------------------------------------------------------------- HTTP 小工具


def request(base: str, method: str, path: str, body=None, timeout: float = 15.0):
    """返回 (状态码, 解析后的 JSON 或原始 bytes)。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as err:      # 4xx/5xx 也要能读到 body（我们在测错误分支）
        raw = err.read()
        code = err.code
    try:
        return code, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return code, raw


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_workspace(tmp: Path) -> Path:
    """搭一个跟真实仓库同构的临时工作区（只放控制台要用的那几个文件）。"""
    (tmp / "data" / "personas").mkdir(parents=True)
    (tmp / "sessions").mkdir(parents=True)
    shutil.copy(ROOT / "config.toml", tmp / "config.toml")
    (tmp / "data" / "events.json").write_text("[]\n", encoding="utf-8")
    (tmp / "data" / "memos.json").write_text(
        json.dumps({"_说明": "备忘", "items": []}, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "characters.json").write_text(json.dumps({
        "default": "amiya",
        "characters": [{"id": "amiya", "file": "personas/amiya.json", "enabled": True, "default": True}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "personas" / "amiya.json").write_text(json.dumps({
        "id": "amiya", "name": "阿米娅", "title": "测试角色", "background": "b",
        "user_title": "博士", "wake_words": ["阿米娅"], "ack": "我在",
        "style": ["简短"], "lines": [{"scene": "s", "text": "我在，博士。"}],
        "voice_ref": "data/personas/amiya/交谈1.wav", "voice_model": "", "enabled": True,
    }, ensure_ascii=False), encoding="utf-8")
    return tmp


def start_server(settings, port: int):
    import uvicorn

    from voice_loop.console.app import create_app

    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-console", daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, thread, app


# ---------------------------------------------------------------- 各节


def section_meta(base: str, app) -> None:
    print("\n[1] 元信息与静态文件")
    code, meta = request(base, "GET", "/api/meta")
    check("GET /api/meta 是 200", code, 200)
    ids = [p["id"] for p in meta.get("panels", [])]
    check("六个面板都在", ids, ["overview", "schedule", "memos", "logs", "voices", "chat"])
    check("没有面板装载失败", meta.get("panels_failed"), {})

    for path in ("/", "/index.html", "/style.css", "/app.js", "/ui.js",
                 "/panels/overview.js", "/panels/schedule.js", "/panels/memos.js",
                 "/panels/logs.js", "/panels/voices.js", "/panels/chat.js"):
        code, _ = request(base, "GET", path)
        check(f"静态文件 {path}", code, 200)


def section_events(base: str) -> str:
    print("\n[2] 日程/闹钟：一句话添加 → 列表 → 改 → 跳过 → 完成")
    code, _ = request(base, "GET", "/api/events")
    check("初始为空", code == 200)

    # ★跟语音同一套解析★：日期、时间、提前量都该被认出来
    code, created = request(base, "POST", "/api/events", {"text": "明天下午三点开组会，提前半小时"})
    check("添加成功", code, 200)
    item = created.get("item") or {}
    check("标题解析成「开组会」或「组会」", "组会" in str(item.get("title")), True,
          detail=f"title={item.get('title')!r}")
    check("提前量 = [30]（只说「提前半小时」就只提前，不自己加准时）", item.get("remind_before"), [30])
    check("时间是明天 15:00", str(item.get("start", ""))[11:16], "15:00")
    check("带了一句人话回复", bool(created.get("sentence")), True, detail=created.get("sentence", ""))
    eid = item.get("id")
    check("落盘有 id", eid is not None)

    code, listed = request(base, "GET", "/api/events")
    rows = listed.get("items") or []
    check("列表里有 1 条", len(rows), 1)
    check("算出了下一次时间", bool(rows[0].get("next_at")), True, detail=str(rows[0].get("next_at")))
    check("一次性事件不需要确认", rows[0].get("needs_confirm"), False)

    code, patched = request(base, "PATCH", f"/api/events/{eid}", {"fields": {"title": "改过的组会"}})
    check("PATCH 成功", code, 200)
    check("标题改掉了", patched.get("item", {}).get("title"), "改过的组会")

    code, skipped = request(base, "POST", f"/api/events/{eid}/skip", {})
    check("跳过本次成功", code, 200)
    check("state.skipped 有日期", len(skipped.get("state", {}).get("skipped", [])), 1)
    code, done = request(base, "POST", f"/api/events/{eid}/done", {})
    check("标记完成成功", code, 200)
    check("state.done 有记录", len(done.get("state", {}).get("done", [])), 1)
    code, undone = request(base, "POST", f"/api/events/{eid}/undone", {})
    check("取消完成成功", len(undone.get("state", {}).get("done", [])), 0)

    # 重复事件：不改不删要确认（409），带 confirm 才动
    code, rep = request(base, "POST", "/api/events", {"text": "每周三九点上课"})
    rid = (rep.get("item") or {}).get("id")
    check("重复事件建好了", rid is not None)
    code, _ = request(base, "DELETE", f"/api/events/{rid}")
    check("★删重复事件先要确认（409）★", code, 409)
    code, _ = request(base, "PATCH", f"/api/events/{rid}", {"fields": {"title": "x"}})
    check("★改重复事件也先要确认（409）★", code, 409)
    code, ok = request(base, "DELETE", f"/api/events/{rid}?confirm=1")
    check("确认后删掉", code, 200)
    check("确实删了", bool(ok.get("ok")), True)
    return eid


def section_week(base: str) -> None:
    print("\n[3] 周视图")
    code, week = request(base, "GET", "/api/events/week")
    check("周视图 200", code, 200)
    check("给了 7 天", len(week.get("days", [])), 7)
    check("每天都带日期与星期", bool(week["days"][0].get("date")), True,
          detail=str(week["days"][0].get("date")))


def section_memos(base: str) -> None:
    print("\n[4] 备忘")
    code, made = request(base, "POST", "/api/memos", {"text": "记一下买牛奶"})
    check("添加成功", code, 200)
    check("★清洗跟语音一致：「记一下买牛奶」→「买牛奶」★", made.get("content"), "买牛奶")
    code, listed = request(base, "GET", "/api/memos")
    check("列表里 1 条待办", listed.get("open"), 1)
    code, _ = request(base, "PATCH", "/api/memos/1", {"done": True})
    check("勾掉成功", code, 200)
    code, listed = request(base, "GET", "/api/memos")
    check("变成已完成", listed.get("done"), 1)
    code, cleared = request(base, "POST", "/api/memos/clear-done", {})
    check("清理已完成", cleared.get("removed"), 1)
    code, _ = request(base, "DELETE", "/api/memos/99")
    check("删不存在的条目 → 404（不是 500）", code, 404)


def section_misc(base: str, tmp: Path) -> None:
    print("\n[5] 概览 / 角色 / 日志 / 服务状态")
    code, ov = request(base, "GET", "/api/overview")
    check("概览 200", code, 200)
    check("概览里有事件统计", "events" in ov and "total" in ov["events"])
    check("概览里有角色列表", [c["id"] for c in ov["persona"]["characters"]], ["amiya"])
    check("概览里报告服务未运行", ov["service"]["running"], False)

    code, voices = request(base, "GET", "/api/voices")
    check("角色 200", code, 200)
    check("角色名字对", voices["characters"][0]["name"], "阿米娅")
    check("参考音文件不存在时如实标记", voices["characters"][0]["voice_ref_exists"], False)

    code, tail = request(base, "GET", "/api/logs/tail?lines=50")
    check("日志接口 200", code, 200)
    check("日志文件路径在临时目录里", str(tmp) in tail["file"], True, detail=tail["file"])
    check("文件还不存在时也返回结构", tail["exists"], False)

    code, svc = request(base, "GET", "/api/service")
    check("服务状态 200", code, 200)
    check("报告未运行", svc["running"], False)
    check("给了可以启动的标记", svc["actions"]["can_start"], True)

    code, health = request(base, "GET", "/api/health")
    check("健康检查 200", code, 200)
    check("面板清单一致", len(health["panels"]), 6)

    code, chars = request(base, "GET", "/api/chat/characters")
    check("聊天角色列表 200", code, 200)
    check("列表里有角色", [c["id"] for c in chars["items"]], ["amiya"])
    check("报告了服务未运行", chars["service_running"], False)

    code, chat = request(base, "POST", "/api/chat/ask", {"text": "在吗", "character": "amiya"})
    check("指定角色发问不会当成参数错", code, 200)
    check("服务没跑时如实报（不是假装成功）", "服务没在跑" in (chat.get("error") or ""), True,
          detail=str(chat.get("error")))


def section_sse(base: str, tmp: Path) -> None:
    print("\n[6] ★实时通道★：往日志写一行 → 浏览器那侧收到（走 follow→bus→SSE 全链路）")
    log_file = tmp / "sessions" / "listen.log"
    log_file.write_text("12:00:00 I voice_loop | 第一行（历史）\n", encoding="utf-8")

    req = urllib.request.Request(base + "/api/stream?kinds=log")
    got_line = False
    saw_connected = False
    try:
        with urllib.request.urlopen(req, timeout=15) as stream:
            stream.read1(64)                      # 先读第一块（`: connected`）
            saw_connected = True
            # 等跟随线程第一次发现日志文件（它会补发历史行）
            deadline = time.time() + 12
            payload = b""
            while time.time() < deadline and not got_line:
                chunk = stream.read1(4096)
                if chunk:
                    payload += chunk
                    if b"\xe7\xac\xac\xe4\xb8\x80\xe8\xa1\x8c" in payload or "第一行".encode() in payload:
                        got_line = True
    except Exception as exc:  # noqa: BLE001 - 测试失败要给出原因
        check("SSE 连接与读取", False, detail=f"{type(exc).__name__}: {exc}")
        return
    check("SSE 连上了（拿到 connected）", saw_connected)
    check("★收到了日志内容★（follow→bus→SSE 通了）", got_line)

def section_memo_words() -> None:
    print("\n[7] 备忘触发词：控制台那份必须与 skills 的一致（防两边跑偏）")
    # 控制台为了不 import vision（Pillow/OpenCV）自带了一份触发词正则，
    # 这里拿真实的 skills 正则逐句比对：一旦有人只改了一边，这里会失败。
    from voice_loop import event_text as et
    from voice_loop import skills as sk
    from voice_loop.console.panels.memos import strip_trigger

    samples = [
        "记一下买牛奶", "记下带伞", "记一哈交电费", "记住明天开会", "记录一下实验结果",
        "备忘一下买书", "mark 提醒我去取快递", "记：预约牙医", "买牛奶", "提醒我明天交作业",
        "备注带实验报告", "记一下 明天 下午 三点 开会",
    ]
    mismatched = []
    for text in samples:
        m = sk.TRIGGER_MEMO_ADD.search(text)
        want = et.clean_memo_content(m.group("content") if m else text)
        got = et.clean_memo_content(strip_trigger(text))
        if got != want:
            mismatched.append((text, got, want))
    check("两边剥出来的一样", mismatched, [])
    check("「记一下买牛奶」→「买牛奶」", et.clean_memo_content(strip_trigger("记一下买牛奶")), "买牛奶")
    check("不带触发词的「买牛奶」原样", et.clean_memo_content(strip_trigger("买牛奶")), "买牛奶")

def section_registry(tmp: Path) -> None:
    from voice_loop.console.app import create_app
    from voice_loop.settings import load_settings

    settings = load_settings(tmp / "config.toml")
    settings.app.project_root = str(tmp)

    import voice_loop.console.panels as pkg

    original = list(pkg.ALL)
    pkg.ALL = [*original, "does_not_exist_at_all"]
    try:
        app = create_app(settings)
        ids = [p.id for p in app.state.ctx.registry.all()]
        check("坏面板被跳过", "does_not_exist_at_all" not in ids)
        check("好面板照常在", set(ids) == set(original), True, detail=str(ids))
    finally:
        pkg.ALL = original

    # 重复 id 要当场报错（不然两个面板抢同一个标签，前端会静默覆盖）
    from voice_loop.console.registry import Panel, Registry

    reg = Registry()
    reg.add(Panel(id="x", title="X"), owner="a")
    try:
        reg.add(Panel(id="x", title="Y"), owner="b")
        check("重复 id 会报错", False, detail="没报错")
    except ValueError as exc:
        check("重复 id 会报错（并指出是谁）", "a" in str(exc), True, detail=str(exc))


def main() -> int:
    print("=" * 70)
    print(" 控制台端到端自测（真 HTTP，不碰真实数据）")
    print("=" * 70)
    from voice_loop.settings import load_settings

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = make_workspace(Path(tmpdir))
        settings = load_settings(tmp / "config.toml")
        settings.app.project_root = str(tmp)
        port = free_port()
        server, thread, app = start_server(settings, port)
        base = f"http://127.0.0.1:{port}"
        try:
            check("uvicorn 起来了", server.started)
            section_meta(base, app)
            section_events(base)
            section_week(base)
            section_memos(base)
            section_misc(base, tmp)
            section_sse(base, tmp)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        section_registry(tmp)
        section_memo_words()

    print("\n" + "=" * 70)
    if FAILED:
        print(f" 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(" 全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
