"""联网服务器：DeepSeek 官方 Search / Fetch API。

工具（默认 **不露出**，由 API 角色在 ``api_tools`` 里点名 ``web`` 预设才可见）：

- ``mcp__web__search`` —— 搜索网页，返回标题 / 来源 / 摘要
- ``mcp__web__fetch``  —— 读一个网页的正文（Markdown，按配置截断）

为什么不做成「模型内部服务端工具」：官方文档里的 ``deepseek_web_search`` **不能**
和普通 function tools 同时声明（会 400），而这个助手一轮里还要用日程/备忘工具。
所以这里走 **Search / Fetch 原始 JSON API**：它能和现有工具共存，按成功请求计费。

★正文别太长★：语音助手最终要把结果念出来，所以只取摘要 / 前几千字；
完整 URL 只进日志，不进要朗读的文本（念 URL 既难听也没用）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from ...llm import resolve_api_key
from ...settings import Settings, load_settings
from ..server import MCPServer

NAME = "web"

INSTRUCTIONS = (
    "联网搜索与网页读取。问实时新闻、你不确定或可能过时的知识时用 search；"
    "已知具体网址时用 fetch。返回的文字可以直接念给用户听。"
)

_WS = re.compile(r"\s+")
_MD_NOISE = re.compile(r"[#*`~]+")
_MD_BULLET = re.compile(r"^\s*[-•]\s*")


def _clean(text: Any, limit: int = 0) -> str:
    out = _MD_NOISE.sub(" ", str(text or ""))
    out = _MD_BULLET.sub("", out)
    out = _WS.sub(" ", out).strip()
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


def _format_results(query: str, results: list[dict], limit: int) -> str:
    """把搜索结果排成一段**能直接念**的中文（不念 URL）。"""
    if len(results) > limit:
        lines = [f"查到 {len(results)} 条和「{query}」相关的结果，念最相关的 {limit} 条。"]
    else:
        lines = [f"查到 {len(results)} 条和「{query}」相关的结果。"]
    for i, item in enumerate(results[:limit], 1):
        title = _clean(item.get("title")) or "（无标题）"
        site = _clean(item.get("site_name")) or "网络"
        summary = _clean(item.get("summary")) or _clean(item.get("snippet"))
        if i == 1:
            summary = _clean(summary, 280)
        else:
            summary = _clean(summary or item.get("snippet"), 150)
        lines.append(f"{i}. {title}（{site}）：{summary or '（没有摘要）'}")
    return " ".join(lines)


# --------------------------------------------------------------------------- #
def build_server(
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    **_ignored: Any,
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    settings = settings or load_settings()

    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

    def disabled() -> bool:
        cfg = getattr(settings, "api", None)
        return not bool(getattr(cfg, "enabled", True)) or not bool(
            getattr(cfg, "web_enabled", True)
        )

    def search(args: dict) -> dict:
        cfg = settings.api
        if disabled():
            return {"text": "联网搜索现在是关着的（[api] web_enabled = false）。",
                    "is_error": True}
        query = _clean((args or {}).get("query"), 128)
        if not query:
            return {"text": "要搜什么？给我一个查询词。", "is_error": True}
        try:
            count = int((args or {}).get("count") or getattr(cfg, "search_count", 3))
        except (TypeError, ValueError):
            count = int(getattr(cfg, "search_count", 3))
        count = max(1, min(50, count))
        # 语音场景：API 可以多要几条，但真正念出来的最多是配置的 search_count（默认 3）
        spoken = max(1, min(count, int(getattr(cfg, "search_count", 3) or 3)))
        payload: dict = {
            "query": query,
            "count": count,
            "need_content": bool((args or {}).get("need_content", False)),
        }
        question = _clean((args or {}).get("question"), 128)
        if question:
            payload["ori_question"] = question
        try:
            resp = requests.post(
                search_endpoint(cfg), headers=_headers(cfg), json=payload,
                timeout=(10, float(getattr(cfg, "search_timeout", 60.0) or 60.0)),
            )
        except Exception as exc:  # noqa: BLE001 - 服务器不能因为一次搜索崩掉
            log.warning(f"[mcp:web.search] 请求失败：{exc}")
            return {"text": f"联网搜索没成功：{exc}", "is_error": True}
        if resp.status_code != 200:
            why = _api_error(resp)
            log.warning(f"[mcp:web.search] {query} -> {why}")
            return {"text": f"联网搜索没成功（{why}）。", "is_error": True}
        try:
            results = list(resp.json().get("results") or [])
        except ValueError as exc:
            return {"text": f"搜索结果读不出来：{exc}", "is_error": True}
        if not results:
            return {"text": f"网上没有搜到和「{query}」相关的结果。", "is_error": False}
        for item in results[:count]:
            log.info(f"[mcp:web.search] {_clean(item.get('title'), 40)} {item.get('url')}")
        return {"text": _format_results(query, results, spoken), "is_error": False}

    def fetch(args: dict) -> dict:
        cfg = settings.api
        if disabled():
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
            log.warning(f"[mcp:web.fetch] 请求失败：{exc}")
            return {"text": f"网页没读下来：{exc}", "is_error": True}
        if resp.status_code != 200:
            why = _api_error(resp)
            log.warning(f"[mcp:web.fetch] {url} -> {why}")
            return {"text": f"网页没读下来（{why}）。", "is_error": True}
        try:
            page = resp.json()
        except ValueError as exc:
            return {"text": f"网页内容读不出来：{exc}", "is_error": True}
        title = _clean(page.get("title")) or url
        content = _clean(page.get("content"))
        if not content:
            return {"text": f"《{title}》里没有可读的正文。", "is_error": True}
        limit = max(200, int(getattr(cfg, "fetch_max_chars", 1200) or 1200))
        log.info(f"[mcp:web.fetch] {title} {url} ({len(content)} 字)")
        return {"text": f"《{title}》：{_clean(content, limit)}", "is_error": False}

    server.add_tool(
        "search",
        "联网搜索：输入查询词，返回按相关度排序的网页结果（标题 / 来源 / 摘要）。"
        "问实时新闻、你不确定或可能过时的知识时用。query 支持 site:、lang:、after: 等运算符；"
        "question 可以放用户原话（可选）。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（1~128 字）"},
                "question": {"type": "string",
                             "description": "用户原话（可选，能提高检索理解）"},
                "count": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "向 API 要几条（默认按配置）；"
                                         "真正会念出来的最多是 search_count 条"},
                "need_content": {"type": "boolean",
                                 "description": "是否要网页正文（默认 false，摘要就够念）"},
            },
            "required": ["query"],
        },
        search,
    )
    server.add_tool(
        "fetch",
        "读取一个网页的正文（Markdown，按配置截断到适合朗读的长度）。"
        "已知具体网址、或搜索结果里需要细看某一页时用。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整的 http/https 网址"},
            },
            "required": ["url"],
        },
        fetch,
    )
    return server
