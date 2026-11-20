"""Microsoft 身份认证：device code 登录 + token 缓存 / 续期。

为什么是 **device code + public client**
    - 个人 outlook.com 从 2022-10 起不认 IMAP/SMTP 的明文密码了（微软全面禁用 basic auth），
      只能用 OAuth2；既然横竖要 OAuth2，就走 Graph。
    - device code 是**桌面程序**的官方做法：不需要 client secret、不需要公网回调地址、
      不需要内网穿透 —— 只要在浏览器里输一串短码。
    - 不引入 msal：device code 与 refresh 就是两条表单 POST，用 requests 写清楚
      比多一个依赖更好查（而且自测能整个打桩）。

不变量
    - **整个项目只有这里碰 access token**；其它模块只能拿 ``headers()``。
    - token 存在 ``data/mail/token.json``（已进 .gitignore），非 Windows 上加 0600。
    - refresh token 失效不算崩：抛 :class:`AuthError`，上层提示「需要重新登录」。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGIN_HOST = "https://login.microsoftonline.com"
#: 个人账号用 consumers；M365 工作/学校用 organizations；两个都要用 common
TENANT = "consumers"
#: 最小权限：离线刷新 + 读个人信息 + 读写邮件（草稿）+ 发信
SCOPES = ("offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send")

GRAPH = "https://graph.microsoft.com/v1.0"


class AuthError(RuntimeError):
    """登录 / 续期失败。``reason`` 是给用户看的一句话。"""

    def __init__(self, reason: str, *, needs_login: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.needs_login = needs_login


@dataclass
class DeviceCode:
    """device code 流程第一步的返回（要念给用户的那串码）。"""

    user_code: str
    verification_uri: str
    device_code: str
    expires_in: float
    interval: float = 5.0
    message: str = ""


@dataclass
class Token:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    token_type: str = "Bearer"
    account: str = ""

    @property
    def expired(self) -> bool:
        # 提前 2 分钟算过期：别等卡在请求中间才续
        return not self.access_token or time.time() >= (self.expires_at - 120)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
            "account": self.account,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Token":
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=float(raw.get("expires_at") or 0.0),
            scope=str(raw.get("scope") or ""),
            token_type=str(raw.get("token_type") or "Bearer"),
            account=str(raw.get("account") or ""),
        )


# --------------------------------------------------------------------------- #
# HTTP 打桩点（自测替换它，就不需要真账号）
# --------------------------------------------------------------------------- #
def _post(url: str, data: dict, timeout: float = 20.0) -> tuple[int, dict]:
    import requests  # noqa: PLC0415 - 只有真要登录才 import

    resp = requests.post(url, data=data, timeout=timeout)
    try:
        payload = dict(resp.json() or {})
    except ValueError:
        payload = {"error": f"HTTP {resp.status_code}"}
    return resp.status_code, payload


class Auth:
    """token 的取、存、续。所有网络动作都走注入的 ``post``，方便自测。"""

    def __init__(
        self,
        client_id: str,
        *,
        token_file: str | Path,
        tenant: str = TENANT,
        scopes: tuple[str, ...] = SCOPES,
        log: logging.Logger | None = None,
        post: Callable[[str, dict, float], tuple[int, dict]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.client_id = str(client_id or "").strip()
        self.tenant = str(tenant or TENANT).strip() or TENANT
        self.scopes = tuple(scopes) or SCOPES
        self.token_file = Path(token_file)
        self.log = log or logging.getLogger("voice_loop.mail")
        self._post = post or _post
        self.timeout = float(timeout)
        self._token: Token | None = None

    # ---------------------------------------------------------------- 状态
    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @property
    def scope_text(self) -> str:
        return " ".join(self.scopes)

    def _url(self, tail: str) -> str:
        return f"{LOGIN_HOST}/{self.tenant}/oauth2/v2.0/{tail}"

    def load(self) -> Token | None:
        """从磁盘读 token（不联网）。坏文件当没有。"""
        if self._token is not None:
            return self._token
        try:
            raw = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        self._token = Token.from_dict(raw)
        return self._token

    def save(self, token: Token) -> None:
        self._token = token
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(token.to_dict(), ensure_ascii=False), encoding="utf-8")
        try:
            os.replace(tmp, self.token_file)
            if os.name != "nt":                       # Windows 上 chmod 没意义
                os.chmod(self.token_file, 0o600)
        except OSError as exc:  # noqa: BLE001
            self.log.warning(f"[mail] token 写不进去（{exc}）")

    def forget(self) -> None:
        self._token = None
        try:
            self.token_file.unlink(missing_ok=True)
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        tok = self.load()
        return {
            "configured": self.configured,
            "logged_in": bool(tok and tok.access_token),
            "account": tok.account if tok else "",
            "expires_in": round(max(0.0, (tok.expires_at - time.time())), 0) if tok else 0,
            "scopes": self.scope_text,
            "file": str(self.token_file),
        }

    # ---------------------------------------------------------------- 登录
    def start_device_login(self) -> DeviceCode:
        """第一步：拿 user_code（念给用户，让他在浏览器里输）。"""
        if not self.configured:
            raise AuthError("还没配置 [mail] client_id（见 docs/guide/mail.md）", needs_login=True)
        code, payload = self._post(
            self._url("devicecode"),
            {"client_id": self.client_id, "scope": self.scope_text},
            self.timeout,
        )
        if code != 200 or "device_code" not in payload:
            raise AuthError(
                "拿 device code 失败（HTTP "
                f"{code}）：{payload.get('error_description') or payload.get('error') or payload}",
                needs_login=True,
            )
        return DeviceCode(
            user_code=str(payload.get("user_code") or ""),
            verification_uri=str(
                payload.get("verification_uri") or payload.get("verification_url") or ""),
            device_code=str(payload.get("device_code") or ""),
            expires_in=float(payload.get("expires_in") or 900),
            interval=max(1.0, float(payload.get("interval") or 5)),
            message=str(payload.get("message") or ""),
        )

    def poll_device_login(
        self,
        pending: DeviceCode,
        *,
        sleeper: Callable[[float], None] | None = None,
        deadline: float | None = None,
        on_wait: Callable[[str], None] | None = None,
    ) -> Token:
        """第二步：轮询直到用户完成登录（或超时）。"""
        sleep = sleeper or time.sleep
        end = time.monotonic() + min(float(deadline or pending.expires_in), pending.expires_in)
        interval = max(1.0, pending.interval)
        last = ""
        while time.monotonic() < end:
            code, payload = self._post(
                self._url("token"),
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": pending.device_code,
                },
                self.timeout,
            )
            if code == 200 and payload.get("access_token"):
                return self._store(payload)
            err = str(payload.get("error") or "")
            if err in ("authorization_pending", ""):
                last = err or "还没完成登录"
            elif err == "slow_down":
                interval += 5
                last = "轮询太快，放慢一点"
            elif err in ("expired_token", "authorization_declined", "bad_verification_code"):
                raise AuthError(
                    f"登录没完成（{err}）：{payload.get('error_description') or ''}".strip(),
                    needs_login=True,
                )
            else:
                raise AuthError(
                    f"登录失败（{err or code}）：{payload.get('error_description') or ''}".strip(),
                    needs_login=True,
                )
            if on_wait is not None:
                on_wait(last)
            sleep(interval)
        raise AuthError(f"登录超时（{last or '用户没在时限内完成'}）", needs_login=True)

    def _store(self, payload: dict) -> Token:
        token = Token(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=time.time() + float(payload.get("expires_in") or 3600),
            scope=str(payload.get("scope") or self.scope_text),
            token_type=str(payload.get("token_type") or "Bearer"),
        )
        self.save(token)
        self.log.info(
            f"[mail] 登录成功（token 有效 {round(max(0.0, token.expires_at - time.time()))}s）")
        return token

    # ---------------------------------------------------------------- 续期
    def refresh(self) -> Token:
        tok = self.load()
        if tok is None or not tok.refresh_token:
            raise AuthError("还没有登录过（或 refresh token 丢了）", needs_login=True)
        code, payload = self._post(
            self._url("token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": tok.refresh_token,
                "scope": self.scope_text,
            },
            self.timeout,
        )
        if code != 200 or not payload.get("access_token"):
            err = payload.get("error") or code
            self.forget()
            raise AuthError(
                f"续期失败（{err}）：{payload.get('error_description') or ''}"
                "（需要重新登录：python main.py mail login）",
                needs_login=True,
            )
        account = tok.account
        fresh = self._store(payload)
        fresh.account = account
        self.save(fresh)
        return fresh

    def token(self, *, force: bool = False) -> Token:
        """拿一个能用的 access token（必要时自动续期）。"""
        tok = None if force else self.load()
        if tok is not None and not tok.expired:
            return tok
        return self.refresh()

    def headers(self) -> dict[str, str]:
        tok = self.token()
        return {
            "Authorization": f"{tok.token_type or 'Bearer'} {tok.access_token}",
            "Accept": "application/json",
        }

    # ---------------------------------------------------------------- 账号
    def set_account(self, address: str) -> None:
        tok = self.load()
        if tok is None:
            return
        tok.account = str(address or "")
        self.save(tok)
