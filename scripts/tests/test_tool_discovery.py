"""渐进式工具发现自测：目录 / 搜索 / 描述 / 统一调用 / 权限边界。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop.mcp.host import MCPHost                  # noqa: E402
from voice_loop.mcp.protocol import ToolSpec             # noqa: E402
from voice_loop.mcp.servers import meta as meta_srv      # noqa: E402
from voice_loop.tool_presets import authorized, expand    # noqa: E402
from voice_loop.tools import harness_hint                # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, want=True, detail="") -> None:
    ok = got == want
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def fake_host() -> MCPHost:
    host = MCPHost(SimpleNamespace(enabled=False, servers=[]))
    specs = {
        "now": ToolSpec("now", "现在几点 / 今天几号"),
        "read_file": ToolSpec("read_file", "读取工作区文本文件",
                              {"type": "object", "properties": {
                                  "path": {"type": "string"}},
                               "required": ["path"]}),
        "run_command": ToolSpec("run_command", "在工作区执行 shell 命令",
                                {"type": "object", "properties": {
                                    "command": {"type": "string"}},
                                 "required": ["command"]}),
        "mail_send": ToolSpec("mail_send", "真的把邮件发出去"),
        "tool_search": ToolSpec("tool_search", "搜工具"),
    }
    host._all_specs = specs
    host._routes = {k: ("fake", k) for k in ("now", "read_file")}
    host._all_routes = {k: ("fake", k) for k in specs}
    host.meta_allowed = {"now", "read_file"}
    return host


def call(server, name: str, args: dict) -> tuple[str, bool]:
    res = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
    result = res.get("result") or {}
    content = result.get("content") or []
    return str(content[0].get("text") or "") if content else "", bool(result.get("isError"))


def test_catalog() -> None:
    print("\n[1] 工具目录：hidden 也进目录，但 allowed 如实标记")
    host = fake_host()
    cards = host.catalog(allowed=host.meta_allowed)
    names = {c["name"] for c in cards}
    check("目录不含 meta 工具自己", "tool_search" not in names, True, str(sorted(names)))
    check("hidden 的 shell/mail 也在目录里",
          {"run_command", "mail_send"} <= names, True)
    check("分组与风险算对",
          {(c["group"], c["risk"]) for c in cards if c["name"] == "run_command"}
          == {("shell", "high")}, True)
    by_name = {c["name"]: c for c in cards}
    check("allowed 标记来自角色可见集合",
          (by_name["read_file"]["allowed"], by_name["mail_send"]["allowed"]),
          (True, False))
    card = host.tool_describe("read_file")
    check("describe 带完整 schema", card is not None
          and card["input_schema"]["required"] == ["path"], True, str(card))
    check("describe 不存在的工具返回 None", host.tool_describe("nope"), None)
    ok, text = host.invoke("read_file", {})
    check("缺参数被校验挡住", ok is False and "缺少必填参数" in text, True, text)
    ok, text = host.invoke("read_file", {"path": 3})
    check("参数类型不对也被挡住", ok is False and "类型不对" in text, True, text)
    ok, text = host.invoke("mail_send", {}, allowed=host.meta_allowed)
    check("★发现不等于授权：未授权工具 invoke 被拒★",
          ok is False and "不能调用" in text, True, text)


def test_meta_server() -> None:
    print("\n[2] 四个元工具走 JSON-RPC 形状")
    host = fake_host()
    srv = meta_srv.build_server(settings=None, logger=None, host=host)
    names = {t.name for t in srv.tools()}
    check("元工具全注册",
          names == {"tool_groups", "tool_search", "tool_describe", "tool_invoke"}, True,
          str(sorted(names)))
    text, err = call(srv, "tool_groups", {})
    check("groups 给出分组", err is False and "shell: 1" in text and "files: 1" in text,
          True, text)
    text, err = call(srv, "tool_search", {"query": "shell 命令"})
    check("search 找到 run_command", err is False and "run_command" in text, True, text)
    text, err = call(srv, "tool_describe", {"name": "read_file"})
    check("describe 返回 JSON schema", err is False and '"path"' in text, True, text[:80])
    text, err = call(srv, "tool_invoke", {"name": "mail_send", "arguments": {}})
    check("invoke 越权被拒", err is True and "不能调用" in text, True, text)


def test_preset_and_hint() -> None:
    print("\n[3] 预设：渐进式小而稳定，原 harness 保留")
    progressive = expand(["harness-progressive"])
    full = expand(["harness"])
    check("渐进式只有 11 项", len(progressive), 11, str(sorted(progressive)))
    check("四个元工具都在", {"tool_groups", "tool_search", "tool_describe",
                            "tool_invoke"} <= progressive, True)
    check("长尾工具不在固定数组", "run_command" not in progressive
          and "mail_send" not in progressive, True)
    check("原 harness 仍是 31 项", len(full), 31, str(len(full)))
    check("提示词教模型按需发现",
          "tool_search" in harness_hint(progressive | {"ask_user"}), True)


def test_progressive_authorization() -> None:
    print("\n[4] 渐进式：固定暴露 11 项，但授权全集含长尾（否则 discover 后调不动）")
    progressive = expand(["harness-progressive"])
    auth = authorized(["harness-progressive"])
    check("固定数组仍是 11 项（KV cache 优化不动）", len(progressive), 11)
    check("授权全集含完整 harness 31 项 + 4 个元工具（35）", len(auth), 35, str(len(auth)))
    check("harness 31 项一个不漏", expand(["harness"]) <= auth, True)
    check("★run_command / write_file 在授权集里★",
          {"run_command", "write_file"} <= auth, True,
          str(sorted({"run_command", "write_file"} - auth)))
    check("固定集与授权集确实不同", progressive < auth, True,
          f"固定 {len(progressive)} / 授权 {len(auth)}")

    # 闸门边界：没注册的工具（= allow_shell 关掉时）进不了 names()，也调不到。
    host = fake_host()  # 只有 now/read_file/run_command/mail_send/tool_search 注册
    names = host.names(include=auth)
    check("已注册的 run_command 授权后可见", "run_command" in names, True)
    check("★没注册的 write_file 不会因为授权全集而放行★", "write_file" not in names, True)
    host.meta_allowed = names
    ok, text = host.invoke("run_command", {"command": "x"}, allowed=names)
    check("授权后的 run_command 不再被角色闸门挡住",
          "这个角色不能调用" not in text, True, text)
    ok, text = host.invoke("write_file", {"path": "x"}, allowed=names)
    check("没注册的 write_file 仍被挡住", "不能调用" in text or "没有这个工具" in text,
          True, text)

    # 非渐进式预设行为不变
    check("普通 harness 授权 = 展开", authorized(["harness"]), expand(["harness"]))
    check("web 预设授权不变", authorized(["web"]), expand(["web"]))


def main() -> int:
    test_catalog()
    test_meta_server()
    test_preset_and_hint()
    test_progressive_authorization()
    print("\n" + "=" * 60)
    if _failures:
        print("  工具发现自测失败：" + "、".join(_failures))
        return 1
    print("  工具发现自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
