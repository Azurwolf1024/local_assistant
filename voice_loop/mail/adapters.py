"""适配器：把项目里已有的能力接到邮件子系统上（**唯一知道外面世界的一个文件**）。

邮件包本身只依赖 settings / log / 路径，方便整个目录搬走；
但「分类用哪个模型」这类事必须由外面注进来，所以收在这一个文件里：

    * :func:`api_classifier` —— 用 ``[api]`` 那套 OpenAI 兼容接口做分类（默认走云端）。
    * :func:`ollama_classifier` —— 用本地 Ollama 做分类（零外发，慢一点）。

两者的签名都是 ``llm_call(system, user) -> str``，和 :class:`voice_loop.mail.triage.Triage`
要的形状一致；自测里直接塞一个假函数就行。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

LlmCall = Callable[[str, str], str]


def api_classifier(
    api_cfg: Any,
    *,
    model: str = "",
    timeout: float = 30.0,
    log: logging.Logger | None = None,
) -> LlmCall | None:
    """用 ``[api]``（OpenAI 兼容）做分类。没配 key / 没开就返回 ``None``。"""
    log = log or logging.getLogger("voice_loop.mail")
    base = str(getattr(api_cfg, "base_url", "") or "").rstrip("/")
    if not base or not bool(getattr(api_cfg, "enabled", True)):
        return None
    try:
        from ..llm import resolve_api_key  # noqa: PLC0415 - 只有真要调用才 import

        key = resolve_api_key(api_cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[mail] 读不到 API key，分类只用规则：{exc}")
        return None
    if not key:
        return None
    url = base if base.endswith("/chat/completions") else (
        f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions")
    picked = str(model or getattr(api_cfg, "model", "") or "").strip()

    def call(system: str, user: str) -> str:
        import requests  # noqa: PLC0415

        payload: dict[str, Any] = {
            "model": picked,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 300,
            "stream": False,
        }
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    return call


def ollama_classifier(
    llm_cfg: Any,
    *,
    model: str = "",
    timeout: float = 60.0,
    log: logging.Logger | None = None,
) -> LlmCall | None:
    """用本地 Ollama 做分类（不出网）。"""
    log = log or logging.getLogger("voice_loop.mail")
    host = str(getattr(llm_cfg, "host", "") or "").rstrip("/")
    if not host:
        return None
    picked = str(model or getattr(llm_cfg, "model", "") or "").strip()

    def call(system: str, user: str) -> str:
        import requests  # noqa: PLC0415

        resp = requests.post(
            f"{host}/api/chat",
            json={
                "model": picked,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        return str((data.get("message") or {}).get("content") or "")

    return call


def json_call(llm: LlmCall, system: str, user: str) -> dict:
    """跑一次并尽力解析出 JSON（给自测/探针用）。"""
    raw = llm(system, user)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"没有 JSON：{raw[:120]}")
    return json.loads(raw[start:end + 1])
