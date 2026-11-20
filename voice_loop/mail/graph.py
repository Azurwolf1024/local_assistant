"""Microsoft Graph 的最小封装：只做邮件这件事，但把坑填好。

填的坑
    - **429 / 503 限流**：按 ``Retry-After`` 退避，最多重试两次（Graph 的限流很快会过去，
      但重试太多会把配额打得更死）。
    - **410 Gone**：delta 游标过期，抛 :class:`DeltaGone` 让上层**全量重来**
      （这是微软文档明确要求的处理方式，不是异常情况）。
    - **401**：token 可能刚好被吊销/过期 → 强制续期一次再试。
    - **超时**：一律带 ``timeout``，不允许无限等。
    - **字段裁剪**：``$select`` 只取要用的，别把整封正文塞进索引。

网络层同样可注入（``request=``），所以离线自测能用一个本地假服务器把全流程跑完。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .auth import GRAPH, Auth, AuthError

#: 列表/同步用到的字段（★别加 body★：几百封一起拉会很慢，正文按需再拉）
LIST_SELECT = (
    "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,isRead,hasAttachments,importance,bodyPreview,webLink,flag,"
    "parentFolderId"
)
#: 读全文只需要这三样
BODY_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,uniqueBody,webLink"

Transport = Callable[..., tuple[int, dict, Any]]


class GraphError(RuntimeError):
    def __init__(self, reason: str, *, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class DeltaGone(GraphError):
    """410 Gone：游标过期，必须丢下游标全量重来。"""

    def __init__(self, reason: str = "delta 游标过期") -> None:
        super().__init__(reason, status=410)


def _requests_transport(method: str, url: str, *, headers: dict, params: dict | None,
                        payload: dict | None, timeout: float) -> tuple[int, dict, Any]:
    import requests  # noqa: PLC0415

    resp = requests.request(method, url, headers=headers, params=params or None,
                            json=payload, timeout=timeout)
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    return resp.status_code, dict(resp.headers or {}), body


def _addr(entry: Any) -> dict[str, str]:
    """Graph 的收件人结构 → ``{"name": ..., "address": ...}``。"""
    if not isinstance(entry, dict):
        return {"name": "", "address": ""}
    email = entry.get("emailAddress") if isinstance(entry.get("emailAddress"), dict) else entry
    return {
        "name": str(email.get("name") or ""),
        "address": str(email.get("address") or "").strip(),
    }


def normalize_message(raw: dict) -> dict[str, Any]:
    """Graph 的 message → 我们索引里那一行（字段名统一成下划线）。"""
    sender = _addr(raw.get("from") or {})
    to = [_addr(x) for x in (raw.get("toRecipients") or [])]
    cc = [_addr(x) for x in (raw.get("ccRecipients") or [])]
    return {
        "id": str(raw.get("id") or ""),
        "conversation_id": str(raw.get("conversationId") or ""),
        "internet_id": str(raw.get("internetMessageId") or ""),
        "subject": str(raw.get("subject") or "").strip(),
        "sender_name": sender["name"],
        "sender": sender["address"].lower(),
        "to": [a["address"].lower() for a in to if a["address"]],
        "cc": [a["address"].lower() for a in cc if a["address"]],
        "received_at": str(raw.get("receivedDateTime") or ""),
        "is_read": bool(raw.get("isRead")),
        "has_attachments": bool(raw.get("hasAttachments")),
        "importance": str(raw.get("importance") or ""),
        "preview": str(raw.get("bodyPreview") or "").strip(),
        "web_link": str(raw.get("webLink") or ""),
        "flagged": bool((raw.get("flag") or {}).get("flagStatus") == "flagged")
        if isinstance(raw.get("flag"), dict) else False,
    }


class Graph:
    """一个账号的 Graph 客户端。所有方法都要先过 :class:`Auth`。"""

    def __init__(
        self,
        auth: Auth,
        *,
        log: logging.Logger | None = None,
        timeout: float = 20.0,
        base: str = GRAPH,
        request: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.auth = auth
        self.log = log or logging.getLogger("voice_loop.mail")
        self.timeout = float(timeout)
        self.base = base.rstrip("/")
        self._request = request or _requests_transport
        self._sleep = sleep or time.sleep

    # ------------------------------------------------------------------ 底层
    def call(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        retries: int = 2,
        absolute: bool = False,
    ) -> Any:
        url = path_or_url if absolute else f"{self.base}{path_or_url}"
        attempt = 0
        refreshed = False
        while True:
            headers = self.auth.headers()
            headers["Content-Type"] = "application/json"
            status, resp_headers, body = self._request(
                method, url, headers=headers, params=params, payload=payload,
                timeout=self.timeout,
            )
            if status in (200, 201, 202, 204):
                return body
            if status == 401 and not refreshed:
                # token 可能被吊销：强制续期一次（续期失败会抛 AuthError）
                refreshed = True
                self.auth.token(force=True)
                continue
            if status == 410:
                raise DeltaGone(f"410 Gone：{url.split('?')[0]}")
            if status in (429, 503) and attempt < retries:
                wait = _retry_after(resp_headers, default=2.0 * (attempt + 1))
                self.log.warning(f"[mail] Graph {status}，{wait:.1f}s 后重试（{method} {path_or_url[:60]}）")
                attempt += 1
                self._sleep(wait)
                continue
            raise GraphError(_why(status, body), status=status)

    # ------------------------------------------------------------------ 账号
    def me(self) -> dict[str, Any]:
        raw = self.call("GET", "/me", params={"$select": "displayName,mail,userPrincipalName"})
        raw = raw or {}
        return {
            "name": str(raw.get("displayName") or ""),
            "mail": str(raw.get("mail") or raw.get("userPrincipalName") or ""),
        }

    # ------------------------------------------------------------------ 同步
    def folder_id(self, name: str = "inbox") -> str:
        name = (name or "inbox").strip()
        if not name or name.lower() == "all":
            return ""
        raw = self.call("GET", f"/me/mailFolders/{name}", params={"$select": "id,displayName"})
        return str((raw or {}).get("id") or "")

    def delta(
        self,
        *,
        folder: str = "inbox",
        cursor: str = "",
        page_size: int = 50,
    ) -> tuple[list[dict], str, str]:
        """拉一页增量。返回 ``(规范化后的消息, next_link, delta_link)``。

        - 有 ``cursor`` 就直接用（那是上次返回的 deltaLink，带着状态令牌）；
        - 没有就从**第一次同步**开始：只取 ``inbox`` 最近的东西（Graph 会给全量分页）。
        """
        folder = (folder or "inbox").strip()
        if cursor:
            url, absolute = cursor, True
        elif folder.lower() == "all":
            url = f"{self.base}/me/messages/delta?$select={LIST_SELECT}&$top={int(page_size)}"
            absolute = True
        else:
            fid = self.folder_id(folder)
            if fid:
                url = (f"{self.base}/me/mailFolders/{fid}/messages/delta"
                       f"?$select={LIST_SELECT}&$top={int(page_size)}")
            else:
                url = (f"{self.base}/me/mailFolders/{folder}/messages/delta"
                       f"?$select={LIST_SELECT}&$top={int(page_size)}")
            absolute = True
        raw = self.call("GET", url, absolute=absolute) or {}
        items = [normalize_message(x) for x in (raw.get("value") or []) if isinstance(x, dict)]
        return items, str(raw.get("@odata.nextLink") or ""), str(raw.get("@odata.deltaLink") or "")

    def delta_cursor_from_now(self, *, folder: str = "inbox", max_pages: int = 30,
                             page_size: int = 200) -> str:
        """拿到「此刻」的游标（= 最后一页的 ``@odata.deltaLink``）。

        ★别再试 ``$deltatoken=latest`` 了★：那是旧版 Outlook 的写法，Graph 的
        ``/messages/delta`` 现在不认 —— 实机上它返回的对象里没有 ``@odata.deltaLink``，
        于是游标一直是空串，**每轮巡检都退化成全量重拉**（276 封 × 6 页，每 3 分钟一次）。
        正确做法只能是把 delta 从头翻到最后一页（``$select=id``，不要正文），
        最后一页才带 ``@odata.deltaLink``。翻不动（超过 ``max_pages``）就返回空串，
        下一轮继续按时间窗口同步，功能不受影响。
        """
        folder = (folder or "inbox").strip()
        if folder and folder.lower() != "all":
            fid = self.folder_id(folder)
            base = (f"/me/mailFolders/{fid}/messages/delta" if fid
                    else "/me/messages/delta")
        else:
            base = "/me/messages/delta"
        url = f"{base}?$select=id&$top={int(page_size)}"
        absolute = False
        for page in range(1, max(1, int(max_pages)) + 1):
            raw = self.call("GET", url, absolute=absolute) or {}
            cursor = str(raw.get("@odata.deltaLink") or "")
            if cursor:
                self.log.info(f"[mail] 取到增量游标（翻了 {page} 页）")
                return cursor
            nxt = str(raw.get("@odata.nextLink") or "")
            if not nxt:
                return ""
            url, absolute = nxt, True
        self.log.info(f"[mail] 信箱太大，{max_pages} 页还没翻到末尾：这轮先按时间窗口同步")
        return ""

    # ------------------------------------------------------------------ 读
    def list_recent(
        self,
        *,
        folder: str = "inbox",
        since_iso: str = "",
        page_size: int = 50,
    ) -> tuple[list[dict], str]:
        """第一次同步用：按时间倒序拉最近一批（``$filter`` + 分页）。

        为什么不直接用 delta 做首次全量：delta 会把**整个文件夹**的变更都吐出来
        （几千封要翻几十页），而我们只要最近 N 天。先用 ``$filter`` 拉窗口，
        再取「从现在开始」的 delta 游标接力，既省流量也不会漏掉新邮件。
        """
        folder = (folder or "inbox").strip()
        base = (f"/me/mailFolders/{folder}/messages" if folder and folder.lower() != "all"
                else "/me/messages")
        params: dict[str, Any] = {"$select": LIST_SELECT, "$top": int(page_size),
                                  "$orderby": "receivedDateTime desc"}
        if since_iso:
            params["$filter"] = f"receivedDateTime ge {since_iso}"
        raw = self.call("GET", base, params=params) or {}
        items = [normalize_message(x) for x in (raw.get("value") or []) if isinstance(x, dict)]
        return items, str(raw.get("@odata.nextLink") or "")

    def next_page(self, url: str) -> tuple[list[dict], str]:
        """跟一页 ``@odata.nextLink``。"""
        raw = self.call("GET", url, absolute=True) or {}
        items = [normalize_message(x) for x in (raw.get("value") or []) if isinstance(x, dict)]
        return items, str(raw.get("@odata.nextLink") or "")

    def message(self, message_id: str) -> dict[str, Any]:
        raw = self.call("GET", f"/me/messages/{message_id}",
                        params={"$select": BODY_SELECT}) or {}
        out = normalize_message(raw)
        body = raw.get("uniqueBody") or raw.get("body") or {}
        out["body_html"] = str(body.get("content") or "") if isinstance(body, dict) else ""
        out["body_type"] = str(body.get("contentType") or "text") if isinstance(body, dict) else "text"
        return out

    # ------------------------------------------------------------------ 写
    def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        cc: list[str] | None = None,
        reply_to: str = "",
    ) -> dict[str, Any]:
        """建草稿。``reply_to`` 给了就走 ``createReply``（保留会话线索）。"""
        if reply_to:
            raw = self.call("POST", f"/me/messages/{reply_to}/createReply", payload={}) or {}
            draft_id = str(raw.get("id") or "")
            if not draft_id:
                raise GraphError("createReply 没给 draft id")
            patch: dict[str, Any] = {"body": {"contentType": "Text", "content": body_text}}
            if subject:
                patch["subject"] = subject
            if to:
                patch["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
            if cc:
                patch["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
            raw = self.call("PATCH", f"/me/messages/{draft_id}", payload=patch) or raw
        else:
            payload: dict[str, Any] = {
                "subject": subject,
                "body": {"contentType": "Text", "content": body_text},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            }
            if cc:
                payload["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
            raw = self.call("POST", "/me/messages", payload=payload) or {}
        return {
            "id": str(raw.get("id") or ""),
            "subject": str(raw.get("subject") or subject),
            "web_link": str(raw.get("webLink") or ""),
        }

    def send_draft(self, draft_id: str) -> None:
        """发送一封**已经存在的**草稿（审批通过后才允许走到这里）。"""
        self.call("POST", f"/me/messages/{draft_id}/send", payload={})

    def delete_message(self, message_id: str) -> None:
        try:
            self.call("DELETE", f"/me/messages/{message_id}")
        except GraphError as exc:
            self.log.warning(f"[mail] 删草稿失败（忽略）：{exc.reason}")


def _retry_after(headers: dict, *, default: float) -> float:
    for key in ("Retry-After", "retry-after"):
        value = headers.get(key)
        if value is None:
            continue
        try:
            return max(0.5, min(60.0, float(value)))
        except (TypeError, ValueError):
            continue
    return default


def _why(status: int, body: Any) -> str:
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif err:
            detail = str(err)
    if status == 401:
        return "Graph 401：token 无效或权限不够（可能需要重新登录 / 补同意权限）"
    if status == 403:
        return f"Graph 403：没有权限（{detail or '检查 [mail] scopes 和账号类型'}）"
    if status == 404:
        return f"Graph 404：找不到这封邮件/这个文件夹（{detail}）".strip()
    return f"Graph HTTP {status}：{detail or '请求失败'}"
