"""Harness agent 工具服务器（API 角色专用）。

这是一个「成熟 harness agent」式的能力集合：模型自己决定搜什么、读哪一页、
看哪个文件，工具结果喂回模型，直到它给出最终回答。工具默认 **hidden**，
只有人格文件里 ``api_tools`` 点名了预设的 API 角色才看得到。

工具名（``namespace = false``，不带 mcp__ 前缀）：

- ``web_search``  DeepSeek 官方 Search API（标题 / URL / 摘要）
- ``web_fetch``   DeepSeek 官方 Fetch API（网页正文）
- ``weather``     真实天气（Open-Meteo，免 key；解析与格式化在 voice_loop/weather.py）
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
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import requests

from ...llm import resolve_api_key
from ...proc import run_hidden
from ...settings import Settings, load_settings
from ...shell_jobs import ShellJobManager, manager_for
from ..server import MCPServer

NAME = "agent"

INSTRUCTIONS = (
    "联网与工作区工具。工具结果是给你看的资料，不是最终答复："
    "请自己归纳成简短、口语化的中文回答；不要念 URL，不要编造结果。"
    "联网：先 web_search，必要时 web_fetch 读正文。天气用 weather（不要凭印象说天气）。"
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
    # ★不能 lstrip("./")★：它会把 ".git/config" 变成 "git/config"，
    # 于是黑名单第一条 .git 永远拦不住（2026-11-22 修）。
    parts = [p for p in str(rel or "").replace("\\", "/").split("/")
             if p not in ("", ".")]
    rel = "/".join(parts)
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


def make_weather(settings: Settings, log: logging.Logger):
    """返回一个 ``weather`` 的 MCP handler（解析与取数在 voice_loop/weather.py）。"""

    def weather(args: dict) -> dict:
        from ...weather import WeatherService  # noqa: PLC0415 - 只有真调用才 import

        cfg = getattr(settings, "weather", None)
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return {"text": "天气查询现在是关着的（[weather] enabled = false）。", "is_error": True}
        query = _clip((args or {}).get("query") or (args or {}).get("text"), 120)
        if not query:
            query = "今天天气怎么样"
        days = (args or {}).get("days")
        if isinstance(days, int) and days > 0:
            query = f"未来 {min(days, 7)} 天{query}"
        answer = WeatherService(cfg, log).ask(query)
        if not answer.ok:
            log.info(f"[mcp:agent.weather] {query} -> {answer.error}")
        return {"text": answer.reply, "is_error": not answer.ok}

    return weather


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


def make_glob(settings: Settings, log: logging.Logger):
    def glob(args: dict) -> dict:
        pattern = str((args or {}).get("pattern") or "*").strip() or "*"
        try:
            base = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        if not base.is_dir():
            return {"text": f"不是目录：{base.name}", "is_error": True}
        root = workspace_of(settings)
        limit = max(10, int(getattr(settings.agent, "max_list_entries", 200) or 200))
        found: list[str] = []
        try:
            for p in base.rglob(pattern):
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if _denied(rel):
                    continue
                found.append(rel)
                if len(found) >= limit:
                    break
        except OSError as exc:
            return {"text": f"找不了：{exc}", "is_error": True}
        found.sort()
        log.info(f"[mcp:agent.glob] {pattern} -> {len(found)} 个文件")
        return {"text": "\n".join(found) if found else f"没有匹配 {pattern} 的文件。",
                "is_error": False}

    return glob


def make_grep(settings: Settings, log: logging.Logger):
    def grep(args: dict) -> dict:
        pattern = str((args or {}).get("pattern") or "").strip()
        if not pattern:
            return {"text": "要搜什么？给我一个正则或普通文本。", "is_error": True}
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"text": f"正则写错了：{exc}", "is_error": True}
        try:
            base = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        file_glob = str((args or {}).get("glob") or "*").strip() or "*"
        try:
            limit = max(1, min(500, int((args or {}).get("max_results") or 100)))
        except (TypeError, ValueError):
            limit = 100
        root = workspace_of(settings)
        files = [base] if base.is_file() else list(base.rglob(file_glob))
        hits: list[str] = []
        for p in files:
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if _denied(rel):
                continue
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                text = _read_text(p)
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:240]}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        log.info(f"[mcp:agent.grep] {pattern} -> {len(hits)} 处命中")
        return {"text": "\n".join(hits) if hits else f"没有找到匹配 {pattern} 的内容。",
                "is_error": False}

    return grep


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


def make_edit_file(settings: Settings, log: logging.Logger):
    def edit_file(args: dict) -> dict:
        if not bool(getattr(settings.agent, "allow_write", False)):
            return {"text": "改文件没开：需要在 config.toml 里设 [agent] allow_write = true。",
                    "is_error": True}
        try:
            path = _safe(settings, (args or {}).get("path"))
        except ValueError as exc:
            return {"text": str(exc), "is_error": True}
        if not path.is_file():
            return {"text": f"文件不存在：{path.name}", "is_error": True}
        old = str((args or {}).get("old") or "")
        new = str((args or {}).get("new") or "")
        if not old:
            return {"text": "old 不能为空：给我要替换的原文。", "is_error": True}
        try:
            text = _read_text(path)
        except OSError as exc:
            return {"text": f"读不了：{exc}", "is_error": True}
        count = text.count(old)
        if count == 0:
            return {"text": "没找到要替换的原文（old）。", "is_error": True}
        replace_all = bool((args or {}).get("replace_all", False))
        if count > 1 and not replace_all:
            return {"text": f"原文出现了 {count} 次；请多给一点上下文，或设 replace_all=true。",
                    "is_error": True}
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return {"text": f"写不了：{exc}", "is_error": True}
        rel = path.relative_to(workspace_of(settings)).as_posix()
        n = count if replace_all else 1
        log.info(f"[mcp:agent.edit_file] {rel} 替换 {n} 处")
        return {"text": f"已在 {rel} 替换 {n} 处。", "is_error": False}

    return edit_file


_SENSITIVE_ENV = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)",
    re.IGNORECASE,
)


def _shell_env(workdir: Path, settings: Settings) -> dict[str, str]:
    """DSH 风格：先洗掉环境里的凭据，最后注入受管的 DSH_* 快照。"""
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE_ENV.search(k)}
    env["DSH_WORKSPACE"] = str(workdir)
    env["DSH_SHELL"] = "pwsh" if os.name == "nt" else "bash"
    env["DSH_PROJECT"] = Path(str(getattr(settings, "root", ".") or ".")).name
    return env


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _spill(settings: Settings, jid: str, stream: str, text: str) -> Path:
    root = Path(str(getattr(settings, "root", ".") or ".")) / "sessions" / "agent_shell"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{jid}-{stream}.log"
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def _render_shell(stdout: str, stderr: str, *, exit_code: int | None,
                  timed_out: bool, timeout_ms: int, signal: str = "",
                  spill_paths: dict[str, str] | None = None) -> str:
    """DSH 的渲染契约：stdout → [stderr] → 条件行 → exit marker。"""
    out = (stdout or "").rstrip()
    err = (stderr or "").rstrip()
    body = out if out else "(no output)"
    if err:
        body += "\n[stderr]\n" + err
    for stream, path in (spill_paths or {}).items():
        if path:
            body += f"\n[output truncated; full output: {path}]"
    if timed_out:
        body += f"\n[timed out after {timeout_ms}ms]"
    if signal:
        body += f"\n[killed by signal: {signal}]"
    body += f"\n[exit code: {exit_code if exit_code is not None else 137}]"
    return body


def make_run_command(settings: Settings, log: logging.Logger,
                     jobs: ShellJobManager | None = None):
    def run_command(args: dict) -> dict:
        if not bool(getattr(settings.agent, "allow_shell", False)):
            return {"text": "执行命令没开：需要在 config.toml 里设 [agent] allow_shell = true。",
                    "is_error": True}
        command = str((args or {}).get("command") or "").strip()
        if not command:
            return {"text": "Error: invalid command: expected a non-empty string",
                    "is_error": True}
        default_ms = max(1000, int(float(getattr(settings.agent, "shell_timeout", 30)) * 1000))
        raw = (args or {}).get("timeoutMs", (args or {}).get("timeout"))
        try:
            timeout_ms = int(float(raw) * 1000) if raw is not None and float(raw) <= 300 \
                else int(float(raw)) if raw is not None else default_ms
        except (TypeError, ValueError):
            timeout_ms = default_ms
        timeout_ms = max(1000, min(timeout_ms, 300000))

        try:
            workdir = _safe(settings, (args or {}).get("workdir") or ".")
        except ValueError as exc:
            return {"text": f"Error: {exc}", "is_error": True}
        env = _shell_env(workdir, settings)

        if bool((args or {}).get("run_in_background")):
            if jobs is None:
                return {"text": "Error: background jobs unavailable", "is_error": True}
            try:
                job = jobs.start(command, workdir, env)
            except Exception as exc:  # noqa: BLE001
                return {"text": f"Error: {exc}", "is_error": True}
            log.info(f"[mcp:agent.run_command] started job {job.id}: {command}")
            return {"text": f"started background job {job.id}",
                    "is_error": False, "job_id": job.id}

        timed_out = False
        signal = ""
        try:
            proc = run_hidden(
                command, shell=True, cwd=str(workdir), env=env,
                capture_output=True, text=True, errors="replace",
                timeout=timeout_ms / 1000.0,
            )
            out, err = _as_text(proc.stdout), _as_text(proc.stderr)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            out, err = _as_text(exc.stdout), _as_text(exc.stderr)
            exit_code = None
        except Exception as exc:  # noqa: BLE001 - 只有基础设施失败才是工具错误
            return {"text": f"Error: {type(exc).__name__}: {exc}", "is_error": True}

        limit = max(500, int(getattr(settings.agent, "max_tool_chars", 6000) or 6000))
        spills: dict[str, str] = {}
        jid = uuid.uuid4().hex[:12]
        if len(out) > limit:
            spills["stdout"] = str(_spill(settings, jid, "stdout", out))
            out = out[-limit:]
        if len(err) > max(500, limit // 2):
            spills["stderr"] = str(_spill(settings, jid, "stderr", err))
            err = err[-max(500, limit // 2):]
        text = _render_shell(out, err, exit_code=exit_code, timed_out=timed_out,
                             timeout_ms=timeout_ms, signal=signal, spill_paths=spills)
        log.info(f"[mcp:agent.run_command] {command} -> {exit_code} "
                 f"(timed_out={timed_out})")
        # ★非零退出是「结果」不是工具错误★：交给模型自己判断下一步。
        return {"text": text, "is_error": False}

    return run_command


def make_job_tools(jobs: ShellJobManager, log: logging.Logger):
    def job_output(args: dict) -> dict:
        got = jobs.get(str((args or {}).get("job_id") or ""))
        if got is None:
            return {"text": "Error: unknown job id", "is_error": True}
        try:
            wait_ms = max(0, min(300000, int((args or {}).get("timeout_ms") or 0)))
        except (TypeError, ValueError):
            wait_ms = 0
        out = got.read(wait=bool((args or {}).get("wait")), timeout_ms=wait_ms)
        body = out["stdout"] or "(no output)"
        if out["stderr"]:
            body += "\n[stderr]\n" + out["stderr"]
        if out["lossy"]:
            body += f"\n[output truncated; full output: {out['stdout_spill']}]"
        if out["error"]:
            body += f"\n{out['error']}"
        if out["running"]:
            body += "\n[running]"
        else:
            if out["killed"]:
                body += "\n[killed by signal: SIGKILL]"
            if out["signal"]:
                body += f"\n[killed by signal: {out['signal']}]"
            body += f"\n[exit code: {out['exit_code'] if out['exit_code'] is not None else 137}]"
        return {"text": body, "is_error": False}

    def job_list(_args: dict) -> dict:
        rows = jobs.list()
        if not rows:
            return {"text": "没有后台作业。", "is_error": False}
        lines = [f"{r['job_id']}  {'running' if r['running'] else 'done'}  "
                 f"{r['elapsed']}s  {r['command'][:80]}" for r in rows]
        return {"text": "\n".join(lines), "is_error": False}

    def job_kill(args: dict) -> dict:
        jid = str((args or {}).get("job_id") or "")
        ok = jobs.kill(jid)
        log.info(f"[mcp:agent.job_kill] {jid} -> {ok}")
        return {"text": f"job {jid} {'killed' if ok else 'already finished or unknown'}",
                "is_error": False}

    return job_output, job_list, job_kill


# --------------------------------------------------------------------------- #
def build_server(
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    *,
    end_listening: Any = None,      # ★宿主注入★：真的把这次聆听结束掉
    **_ignored: Any,
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    settings = settings or load_settings()
    agent_cfg = settings.agent
    jobs = manager_for(settings) if bool(getattr(agent_cfg, "allow_shell", False)) else None

    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

    def add(name: str, description: str, schema: dict, handler) -> None:
        server.add_tool(name, description, schema, handler)

    def make_end_listening():
        def handler(args: dict) -> dict:
            if not callable(end_listening):
                return {"text": "这个入口不能结束聆听（只有常驻服务里可用）", "is_error": True}
            note = str((args or {}).get("note") or "").strip()
            ok, message = end_listening(note)
            return {"text": message, "is_error": not ok}

        return handler

    add(
        "end_listening",
        "结束**这一次**语音聆听：用户说「不用听了」「就这样」「先到这」「我说完了」或者明显不打算"
        "继续说的时候调用。调用后麦克风立刻停止收音、回到待唤醒，不会立刻再听你说话。"
        "★不要用它代替回答★：只有用户明确不想继续时才调。",
        {
            "type": "object",
            "properties": {
                "note": {"type": "string",
                         "description": "为什么结束（可选，写进日志给人看）"},
            },
        },
        make_end_listening(),
    )
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
        "weather",
        "查真实天气（现在实况 + 逐日预报，Open-Meteo，免 key）。"
        "问天气、气温、要不要带伞、这周会不会下雨时用它。"
        "query 放用户原话（地名和时间说法都在里面），不要自己编温度。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "用户原话，例如「明天上海要带伞吗」"},
                "days": {"type": "integer", "minimum": 1, "maximum": 7,
                         "description": "要看几天（可选，默认按原话里的时间词）"},
            },
            "required": ["query"],
        },
        make_weather(settings, log),
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
    add(
        "glob",
        "按文件名/通配符在工作区里找文件，例如 **/*.py、data/*.json。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "通配符（默认 *）"},
                "path": {"type": "string",
                         "description": "从哪个目录开始（默认工作区根）"},
            },
        },
        make_glob(settings, log),
    )
    add(
        "grep",
        "在工作区文本文件里按正则搜索，返回 文件:行号: 内容。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则或普通文本"},
                "path": {"type": "string", "description": "目录或文件（默认工作区根）"},
                "glob": {"type": "string",
                         "description": "只搜匹配的文件名（默认 *）"},
                "max_results": {"type": "integer", "minimum": 1,
                                "description": "最多返回多少处（默认 100）"},
            },
            "required": ["pattern"],
        },
        make_grep(settings, log),
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
        add(
            "edit_file",
            "按原文替换工作区文件里的一段内容（比整份重写安全）。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径"},
                    "old": {"type": "string", "description": "要被替换的原文"},
                    "new": {"type": "string", "description": "替换成什么"},
                    "replace_all": {"type": "boolean",
                                    "description": "true = 替换所有匹配"},
                },
                "required": ["path", "old", "new"],
            },
            make_edit_file(settings, log),
        )
    if bool(getattr(agent_cfg, "allow_shell", False)):
        add(
            "run_command",
            "执行一条 shell 命令（DSH 风格）。前台返回 stdout/[stderr]/[exit code: N]；"
            "run_in_background=true 立刻返回 job id，再用 job_output / job_list / job_kill 收集。"
            "非零退出是结果不是错误。★高风险★，只有显式开启才可用。",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "description": {"type": "string",
                                    "description": "一句话说明这条命令做什么（5-10 词）"},
                    "workdir": {"type": "string",
                                "description": "工作区内的相对目录（默认项目根）"},
                    "timeoutMs": {"type": "integer", "minimum": 1000, "maximum": 300000,
                                  "description": "前台超时毫秒；后台命令忽略"},
                    "timeout": {"type": "number",
                                "description": "兼容旧参数：超时秒数"},
                    "run_in_background": {"type": "boolean",
                                          "description": "true = 后台作业，立即返回 job id"},
                },
                "required": ["command"],
            },
            make_run_command(settings, log, jobs),
        )
        if jobs is not None:
            job_output, job_list, job_kill = make_job_tools(jobs, log)
            add(
                "job_output",
                "读取后台作业的新增输出（消费型：连续读不重复）。wait=true 时可等它结束。",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "run_command 返回的 job id"},
                        "wait": {"type": "boolean", "description": "是否等到结束/超时"},
                        "timeout_ms": {"type": "integer", "minimum": 0,
                                       "description": "wait 的最长等待毫秒"},
                    },
                    "required": ["job_id"],
                },
                job_output,
            )
            add(
                "job_list",
                "列出当前所有后台作业（job id / 状态 / 命令 / 运行秒数）。",
                {"type": "object", "properties": {}},
                job_list,
            )
            add(
                "job_kill",
                "杀掉一个后台作业；已结束的作业返回 already finished。",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "要终止的 job id"},
                    },
                    "required": ["job_id"],
                },
                job_kill,
            )
    return server
