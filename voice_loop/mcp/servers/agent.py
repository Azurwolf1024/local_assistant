"""Harness agent 工具服务器（API 角色专用）。

这是一个「成熟 harness agent」式的能力集合：模型自己决定搜什么、读哪一页、
看哪个文件，工具结果喂回模型，直到它给出最终回答。工具默认 **hidden**，
只有人格文件里 ``api_tools`` 点名了预设的 API 角色才看得到。

工具名（``namespace = false``，不带 mcp__ 前缀）：

- ``web_search``  DeepSeek 官方 Search API（标题 / URL / 摘要）
- ``web_fetch``   DeepSeek 官方 Fetch API（网页正文）
- ``read_file``   读工作区里的文本文件（默认）
- ``list_dir``    列工作区目录（默认）
- ``write_file``  写工作区文件（``[agent] allow_write = true`` 才出现）
- ``run_command`` 执行命令（``[agent] allow_shell = true`` 才出现；★高风险★）

★为什么不用官方的服务端 web tools★：``deepseek_web_search`` 不能和普通
function tools 同时声明（会 400），而一轮里还要用日程/记忆工具。所以走原始
Search / Fetch JSON API；模型自己多轮调用，最终回答由模型综合。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

from ...llm import resolve_api_key
from ...settings import Settings, load_settings
from ..server import MCPServer

NAME = "agent"

INSTRUCTIONS = (
    "联网与工作区工具。工具结果是给你看的资料，不是最终答复："
    "请自己归纳成简短、口语化的中文回答；不要念 URL，不要编造结果。"
    "联网：先 web_search，必要时 web_fetch 读正文。"
)

_WS = re.compile(r"[ \t]+")


def _clip(text: Any, limit: int) -> str:
    out = _WS.sub(" ", str(text or "")).strip()
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _endpoint(cfg, explicit: str, name: str) -> str:
    url = str(getattr(cfg, explicit, "") or "").strip()
    if url:
        return url
    base = str(getattr(cfg, "base_url", "") or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/v1"):
        return f"{base}/tools/{name}"
    return f"{base}/v1/tools/{name}"


def search_endpoint(cfg) -> str:
    return _endpoint(cfg, "search_url", "search")


def fetch_endpoint(cfg) -> str:
    return _endpoint(cfg, "fetch_url", "fetch")


def _headers(cfg) -> dict:
    return {
        "Authorization": f"Bearer {resolve_api_key(cfg)}",
        "Content-Type": "application/json",
    }


def _api_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else ""
        if msg:
            return f"{resp.status_code}：{msg}"
    except ValueError:
        pass
    return f"{resp.status_code}：{(resp.text or '')[:160]}"


# --------------------------------------------------------------------------- #
# 联网工具（模型可读的结构化文本）
# --------------------------------------------------------------------------- #
def _format_search(query: str, results: list[dict], limit: int, max_chars: int,
                   need_content: bool) -> str:
    lines = [f"query: {query}", f"results: {len(results)}（下面列出前 {min(limit, len(results))} 条）"]
    for i, item in enumerate(results[:limit], 1):
        title = _clip(item.get("title"), 200) or "（无标题）"
        url = str(item.get("url") or "").strip()
        site = _clip(item.get("site_name"), 80)
        date = _clip(item.get("date"), 40)
        authority = item.get("authority")
        summary = _clip(item.get("summary") or item.get("snippet"), 900)
        meta = " | ".join(x for x in (
            f"site: {site}" if site else "",
            f"date: {date}" if date else "",
            f"authority: {authority}" if authority is not None else "",
        ) if x)
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   url: {url}")
        if meta:
            lines.append(f"   {meta}")
        if summary:
            lines.append(f"   summary: {summary}")
        if need_content:
            content = _clip(item.get("content"), 2000)
            if content:
                lines.append(f"   content: {content}")
    return _clip("\n".join(lines), max_chars)


def _format_fetch(title: str, url: str, content: str, max_chars: int) -> str:
    body = str(content or "").strip()
    head = f"title: {title}\nurl: {url}\ncontent:\n"
    room = max(200, max_chars - len(head))
    return head + (body[:room] + ("…" if len(body) > room else ""))


def make_search(settings: Settings, log: logging.Logger):
    """返回一个 ``web_search`` 的 MCP handler。"""

    def search(args: dict) -> dict:
        cfg = settings.api
        if not bool(getattr(cfg, "enabled", True)) or not bool(
            getattr(cfg, "web_enabled", True)
        ):
            return {"text": "联网搜索现在是关着的（[api] web_enabled = false）。",
                    "is_error": True}
        query = _clip((args or {}).get("query"), 128)
        if not query:
            return {"text": "要搜什么？给我一个查询词。", "is_error": True}
        try:
            count = int((args or {}).get("count") or getattr(cfg, "search_count", 5))
        except (TypeError, ValueError):
            count = int(getattr(cfg, "search_count", 5))
        count = max(1, min(50, count))
        need_content = bool((args or {}).get("need_content", False))
        payload: dict = {"query": query, "count": count, "need_content": need_content}
        question = _clip((args or {}).get("question"), 128)
        if question:
            payload["ori_question"] = question
        try:
            resp = requests.post(
                search_endpoint(cfg), headers=_headers(cfg), json=payload,
                timeout=(10, float(getattr(cfg, "search_timeout", 60.0) or 60.0)),
            )
        except Exception as exc:  # noqa: BLE001 - 服务器不能因为一次搜索崩掉
            log.warning(f"[mcp:agent.web_search] 请求失败：{exc}")
            return {"text": f"联网搜索没成功：{exc}", "is_error": True}
        if resp.status_code != 200:
            why = _api_error(resp)
            log.warning(f"[mcp:agent.web_search] {query} -> {why}")
            return {"text": f"联网搜索没成功（{why}）。", "is_error": True}
        try:
            results = list(resp.json().get("results") or [])
        except ValueError as exc:
            return {"text": f"搜索结果读不出来：{exc}", "is_error": True}
        if not results:
            return {"text": f"网上没有搜到和「{query}」相关的结果。", "is_error": False}
        for item in results[:count]:
            log.info(f"[mcp:agent.web_search] {_clip(item.get('title'), 40)} {item.get('url')}")
        max_chars = max(500, int(getattr(settings.agent, "max_tool_chars", 6000) or 6000))
        return {"text": _format_search(query, results, count, max_chars, need_content),
                "is_error": False}

    return search


def make_fetch(settings: Settings, log: logging.Logger):
    """返回一个 ``web_fetch`` 的 MCP handler。"""

    def fetch(args: dict) -> dict:
        cfg = settings.api
        if not bool(getattr(cfg, "enabled", True)) or not bool(
            getattr(cfg, "web_enabled", True)
        ):
            return {"text": "网页读取现在是关着的（[api] web_enabled = false）。",
                    "is_error": True}
        url = str((args or {}).get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"text": "给我一个完整的 http/https 网址。", "is_error": True}
        try:
            resp = requests.post(
                fetch_endpoint(cfg), headers=_headers(cfg), json={"url": url},
                timeout=(10, float(getattr(cfg, "search_timeout", 60.0) or 60.0)),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[mcp:agent.web_fetch] 请求失败：{exc}")
            return {"text": f"网页没读下来：{exc}", "is_error": True}
        if resp.status_code != 200:
            why = _api_error(resp)
            log.warning(f"[mcp:agent.web_fetch] {url} -> {why}")
            return {"text": f"网页没读下来（{why}）。", "is_error": True}
        try:
            page = resp.json()
        except ValueError as exc:
            return {"text": f"网页内容读不出来：{exc}", "is_error": True}
        title = _clip(page.get("title"), 200) or url
        content = str(page.get("content") or "").strip()
        if not content:
            return {"text": f"《{title}》里没有可读的正文。", "is_error": True}
        limit = max(500, min(
            int(getattr(cfg, "fetch_max_chars", 6000) or 6000),
            int(getattr(settings.agent, "max_tool_chars", 6000) or 6000),
        ))
        log.info(f"[mcp:agent.web_fetch] {title} {url} ({len(content)} 字)")
        return {"text": _format_fetch(title, url, content, limit), "is_error": False}

    return fetch


# --------------------------------------------------------------------------- #
# 工作区文件 / 命令
# --------------------------------------------------------------------------- #
# ★隐私边界★：即使 workspace 指到项目根，这些也永远不给模型看/写。
#   记忆、会话、知识库、备份、截图都是个人数据；config.local.toml 里有本地路径。
_DENY_DIRS = (
    ".git",
    "sessions",
    "data/memory",
    "data/knowledge",
    "data/vision",
    "data/backup",
)
_DENY_FILES = ("config.local.toml",)


def _denied(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    if rel in _DENY_FILES:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in _DENY_DIRS)


def workspace_of(settings: Settings) -> Path:
    base = Path(str(getattr(settings.agent, "workspace", ".") or "."))
    root = (base if base.is_absolute() else settings.root / base).resolve()
    return root


def _safe(settings: Settings, raw: Any) -> Path:
    root = workspace_of(settings)
    text = str(raw or "").strip() or "."
    p = Path(text)
    target = (p if p.is_absolute() else root / p).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"路径在工作区之外：{text}")
    rel = target.relative_to(root).as_posix()
    if _denied(rel):
        raise ValueError(f"这个路径被隐私保护挡住了：{rel}（记忆 / 会话 / 知识库 / 本地配置不开放）")
    return target


def _read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def make_read_file(settings: Settings, log: logging.Logger):
    def read_file(args: dict) -> dict:
        try:
            path = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        if path.is_dir():
            return {"text": f"{path.name} 是目录，请用 list_dir。", "is_error": True}
        if not path.is_file():
            return {"text": f"文件不存在：{path.name}", "is_error": True}
        try:
            text = _read_text(path)
        except OSError as exc:
            return {"text": f"读不了：{exc}", "is_error": True}
        lines = text.splitlines()
        try:
            start = max(1, int((args or {}).get("start_line") or 1))
            max_lines = max(1, min(2000, int((args or {}).get("max_lines") or 400)))
        except (TypeError, ValueError):
            start, max_lines = 1, 400
        end = min(len(lines), start - 1 + max_lines)
        chunk = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        limit = max(500, int(getattr(settings.agent, "max_file_chars", 6000) or 6000))
        root = workspace_of(settings)
        rel = path.relative_to(root).as_posix()
        head = f"file: {rel}\nlines: {start}-{end} of {len(lines)}\n"
        body = chunk[: max(200, limit - len(head))]
        log.info(f"[mcp:agent.read_file] {rel} ({start}-{end}/{len(lines)})")
        return {"text": head + body, "is_error": False}

    return read_file


def make_list_dir(settings: Settings, log: logging.Logger):
    def list_dir(args: dict) -> dict:
        try:
            path = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        if not path.is_dir():
            return {"text": f"不是目录：{path}", "is_error": True}
        limit = max(10, int(getattr(settings.agent, "max_list_entries", 200) or 200))
        root = workspace_of(settings)
        try:
            entries = [
                p for p in path.iterdir()
                if not _denied(p.relative_to(root).as_posix())
            ]
            entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            return {"text": f"列不了：{exc}", "is_error": True}
        lines = [f"dir: {path.relative_to(root).as_posix()}"]
        for p in entries[:limit]:
            try:
                size = f"  {p.stat().st_size} B" if p.is_file() else "/"
            except OSError:
                size = ""
            lines.append(f"{p.name}{size}")
        if len(entries) > limit:
            lines.append(f"…（还有 {len(entries) - limit} 项未显示）")
        log.info(f"[mcp:agent.list_dir] {path} ({len(entries)} 项)")
        return {"text": "\n".join(lines), "is_error": False}

    return list_dir


def make_write_file(settings: Settings, log: logging.Logger):
    def write_file(args: dict) -> dict:
        if not bool(getattr(settings.agent, "allow_write", False)):
            return {"text": "写文件没开：需要在 config.toml 里设 [agent] allow_write = true。",
                    "is_error": True}
        try:
            path = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        content = str((args or {}).get("content") or "")
        append = bool((args or {}).get("append", False))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a" if append else "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return {"text": f"写不了：{exc}", "is_error": True}
        log.info(f"[mcp:agent.write_file] {path} ({len(content)} 字, append={append})")
        rel = path.relative_to(workspace_of(settings)).as_posix()
        return {"text": f"{'追加' if append else '写入'} {len(content)} 字到 {rel}",
                "is_error": False}

    return write_file


def make_run_command(settings: Settings, log: logging.Logger):
    def run_command(args: dict) -> dict:
        if not bool(getattr(settings.agent, "allow_shell", False)):
            return {"text": "执行命令没开：需要在 config.toml 里设 [agent] allow_shell = true。",
                    "is_error": True}
        command = str((args or {}).get("command") or "").strip()
        if not command:
            return {"text": "要执行什么命令？", "is_error": True}
        try:
            timeout = float((args or {}).get("timeout")
                            or getattr(settings.agent, "shell_timeout", 30.0))
        except (TypeError, ValueError):
            timeout = float(getattr(settings.agent, "shell_timeout", 30.0))
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(workspace_of(settings)),
                capture_output=True, text=True, errors="replace",
                timeout=max(1.0, min(300.0, timeout)),
            )
        except subprocess.TimeoutExpired:
            return {"text": f"命令超时（>{timeout:g}s）：{command}", "is_error": True}
        except Exception as exc:  # noqa: BLE001
            return {"text": f"命令没跑起来：{exc}", "is_error": True}
        limit = max(500, int(getattr(settings.agent, "max_tool_chars", 6000) or 6000))
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = f"exit: {proc.returncode}\nstdout:\n{out[:limit]}\nstderr:\n{err[:limit // 2]}"
        log.info(f"[mcp:agent.run_command] {command} -> {proc.returncode}")
        return {"text": _clip(text, limit * 2), "is_error": proc.returncode != 0}

    return run_command


# --------------------------------------------------------------------------- #
def build_server(
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    **_ignored: Any,
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    settings = settings or load_settings()
    agent_cfg = settings.agent

    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

    def add(name: str, description: str, schema: dict, handler) -> None:
        server.add_tool(name, description, schema, handler)

    add(
        "web_search",
        "联网搜索（DeepSeek Search API）：返回标题、URL、来源、摘要。"
        "问实时新闻、你不确定或可能过时的知识时先调用它；"
        "query 支持 site:、lang:、after: 等运算符。工具结果是资料，请自己归纳后回答。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（1~128 字）"},
                "question": {"type": "string",
                             "description": "用户原话（可选，能提高检索理解）"},
                "count": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "返回几条（默认按配置）"},
                "need_content": {"type": "boolean",
                                 "description": "是否要网页正文（默认 false，摘要通常够）"},
            },
            "required": ["query"],
        },
        make_search(settings, log),
    )
    add(
        "web_fetch",
        "读取一个网页的正文（DeepSeek Fetch API，Markdown）。"
        "搜索结果不够、需要看某一页细节时用。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整的 http/https 网址"},
            },
            "required": ["url"],
        },
        make_fetch(settings, log),
    )
    add(
        "read_file",
        "读取工作区里的文本文件（带行号，可指定起始行与行数）。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内的相对路径"},
                "start_line": {"type": "integer", "minimum": 1,
                               "description": "从第几行开始（默认 1）"},
                "max_lines": {"type": "integer", "minimum": 1,
                              "description": "最多读多少行（默认 400）"},
            },
            "required": ["path"],
        },
        make_read_file(settings, log),
    )
    add(
        "list_dir",
        "列出工作区里的目录内容（目录带 /，文件带大小）。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "工作区内的相对路径（默认根目录）"},
            },
        },
        make_list_dir(settings, log),
    )
    if bool(getattr(agent_cfg, "allow_write", False)):
        add(
            "write_file",
            "写工作区里的文件（可追加）。只允许写 workspace 里面。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                    "append": {"type": "boolean", "description": "true = 追加"},
                },
                "required": ["path", "content"],
            },
            make_write_file(settings, log),
        )
    if bool(getattr(agent_cfg, "allow_shell", False)):
        add(
            "run_command",
            "在工作区里执行一条 shell 命令。★高风险★，只有显式开启才可用。",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "number",
                                "description": "超时秒数（默认按配置）"},
                },
                "required": ["command"],
            },
            make_run_command(settings, log),
        )
    return server
