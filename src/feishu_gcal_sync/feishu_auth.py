"""Feishu user OAuth: authorize URL, code exchange, refresh_token rotation and persistence.

The refresh_token is single-use: every refresh returns a new one and kills the old one
immediately, so the new token is written to disk before it is used for anything.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .config import Config
from .errors import FeishuConfigError, FeishuReauthRequired, FeishuTransientError
from .fsutil import ReservedFile, atomic_write_text

log = logging.getLogger(__name__)

CALENDAR_SCOPE = "calendar:calendar:readonly"
SCOPES = (CALENDAR_SCOPE, "offline_access")
# Refresh when the access token has less than this many seconds left.
REFRESH_MARGIN = 300
# Feishu forces a fresh authorization this long after the user granted access.
GRANT_LIFETIME = 365 * 86400
# The authorization code is only valid for 5 minutes.
CALLBACK_TIMEOUT = 300

# Refresh errors meaning the stored refresh_token is unusable: the user must authorize again.
# 20026 invalid, 20037 expired or past the 365-day limit, 20064 revoked, 20073 already used,
# 20008 user gone, 20066 user status invalid.
_REAUTH_CODES = {20026, 20037, 20064, 20073, 20008, 20066}
# Server-side hiccups worth one retry with the same refresh_token.
_RETRYABLE_CODES = {20050, 20072}
# SIGHUP matters too: sudo forwards it when an SSH session running `fgs sync` drops.
_DEFERRED_SIGNALS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT}
_HINTS = {
    20010: "当前用户不在应用的可用范围内，请在开发者后台把自己加回可用范围并发布版本",
    20074: "开发者后台「安全设置」里的「刷新 user_access_token」开关被关闭，打开后需发布版本",
}


@dataclass
class Token:
    access_token: str
    access_expires_at: float
    refresh_token: str
    refresh_expires_at: float
    scope: str
    granted_at: float

    @classmethod
    def from_response(cls, body: dict, now: float, granted_at: float) -> Token:
        if not body.get("refresh_token"):
            raise FeishuConfigError(
                "token response has no refresh_token: offline_access must be enabled in the "
                "developer console and requested in the authorize URL"
            )
        return cls(
            access_token=body["access_token"],
            access_expires_at=now + int(body.get("expires_in") or 0),
            refresh_token=body["refresh_token"],
            refresh_expires_at=now + int(body.get("refresh_token_expires_in") or 0),
            scope=body.get("scope") or "",
            granted_at=granted_at,
        )

    @property
    def grant_expires_at(self) -> float:
        return self.granted_at + GRANT_LIFETIME


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Token | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return Token(**json.loads(raw))

    def save(self, token: Token) -> None:
        atomic_write_text(self.path, _dump(token))

    @contextmanager
    def reserved(self) -> Iterator[Callable[[Token], None]]:
        """Claim the disk space first; yields a save function that cannot run out of space."""
        with ReservedFile(self.path) as slot:
            yield lambda token: slot.commit(_dump(token))


def _dump(token: Token) -> str:
    return json.dumps(asdict(token), indent=2) + "\n"


class FeishuAuth:
    def __init__(
        self,
        cfg: Config,
        session: requests.Session,
        store: TokenStore,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.session = session
        self.store = store
        self.clock = clock
        self.sleep = sleep

    @property
    def _token_url(self) -> str:
        return f"{self.cfg.feishu_accounts_url}/oauth/v3/token"

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.cfg.feishu_app_id,
                "response_type": "code",
                "redirect_uri": self.cfg.feishu_redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                "prompt": "consent",
            }
        )
        return f"{self.cfg.feishu_accounts_url}/open-apis/authen/v1/authorize?{query}"

    def exchange_code(self, code: str) -> Token:
        form = {
            "grant_type": "authorization_code",
            "client_id": self.cfg.feishu_app_id,
            "client_secret": self.cfg.feishu_app_secret,
            "code": code,
            "redirect_uri": self.cfg.feishu_redirect_uri,
        }
        with _signals_deferred(), self.store.reserved() as save:
            body = self._post_token(form)
            now = self.clock()
            token = Token.from_response(body, now=now, granted_at=now)
            save(token)
        if CALENDAR_SCOPE not in token.scope.split():
            raise FeishuConfigError(
                f"授权结果不包含 {CALENDAR_SCOPE}（实际：{token.scope or '空'}），"
                "请确认开发者后台已开通该权限并发布版本，然后重新运行 `fgs auth`"
            )
        return token

    def access_token(self) -> str:
        token = self.store.load()
        if token is None:
            raise FeishuReauthRequired("没有保存的飞书授权，请运行 `fgs auth`")
        if token.access_expires_at - self.clock() > REFRESH_MARGIN:
            return token.access_token
        return self._refresh(token).access_token

    def invalidate_access_token(self) -> None:
        """Force the next access_token() call to refresh, e.g. after Feishu rejected the token."""
        token = self.store.load()
        if token is not None and token.access_expires_at > 0:
            token.access_expires_at = 0
            self.store.save(token)

    def _refresh(self, token: Token) -> Token:
        form = {
            "grant_type": "refresh_token",
            "client_id": self.cfg.feishu_app_id,
            "client_secret": self.cfg.feishu_app_secret,
            "refresh_token": token.refresh_token,
        }
        # Once Feishu has rotated the token, failing to store the new one loses the grant for
        # good: claim the disk space before asking, and don't die halfway on SIGTERM.
        with _signals_deferred(), self.store.reserved() as save:
            try:
                body = self._post_token(form)
            except FeishuTransientError as exc:
                # The request may have reached Feishu and consumed the refresh_token even though
                # the reply never arrived. One retry with the same token settles it: either it
                # works, or Feishu answers 20073 (already used) and we ask for re-authorization.
                log.warning("token refresh failed (%s), retrying once", exc)
                self.sleep(2)
                body = self._post_token(form)
            new = Token.from_response(body, now=self.clock(), granted_at=token.granted_at)
            save(new)
        log.info("refreshed Feishu user token")
        if CALENDAR_SCOPE not in new.scope.split():
            # Only a warning: if the scope is really gone, the calendar calls fail loudly.
            log.warning("refreshed token scope lacks %s: %r", CALENDAR_SCOPE, new.scope)
        return new

    def _post_token(self, form: dict[str, str]) -> dict:
        try:
            resp = self.session.post(self._token_url, data=form, timeout=30)
        except requests.RequestException as exc:
            raise FeishuTransientError(f"飞书 token 接口请求失败：{type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code = body.get("code")
        if resp.status_code == 200 and code == 0 and body.get("access_token"):
            return body
        detail = (
            f"code={code} error={body.get('error')} "
            f"{body.get('error_description') or ''} (HTTP {resp.status_code})"
        ).strip()
        if code in _REAUTH_CODES:
            raise FeishuReauthRequired(f"飞书授权已失效，需要重新运行 `fgs auth`：{detail}")
        if code in _RETRYABLE_CODES or resp.status_code == 429 or resp.status_code >= 500:
            raise FeishuTransientError(f"飞书 token 接口暂时不可用：{detail}")
        hint = _HINTS.get(code) if isinstance(code, int) else None
        raise FeishuConfigError(f"飞书 token 接口报错：{detail}" + (f"。{hint}" if hint else ""))


@contextmanager
def _signals_deferred() -> Iterator[None]:
    """Hold terminating signals until the block is done; they are delivered right after."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _DEFERRED_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def parse_callback(url: str, expected_state: str) -> str:
    """Return the authorization code from the redirect URL Feishu sent the browser to."""
    params = parse_qs(urlsplit(url).query)
    if "error" in params:
        raise FeishuConfigError(f"授权被拒绝或失败：{params['error'][0]}")
    if params.get("state", [""])[0] != expected_state:
        raise FeishuConfigError("回调里的 state 不匹配，请重新运行 `fgs auth`")
    code = params.get("code", [""])[0]
    if not code:
        raise FeishuConfigError("回调 URL 里没有 code 参数")
    return code


def wait_for_callback(redirect_uri: str, expected_state: str) -> str:
    """Serve the redirect URI once on this machine and return the authorization code."""
    target = urlsplit(redirect_uri)
    host = target.hostname or "localhost"
    bind = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
    port = target.port or 80
    path = target.path or "/"
    result: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if urlsplit(self.path).path != path:
                self.send_error(404)
                return
            try:
                result["code"] = parse_callback(self.path, expected_state)
                message = "授权成功，可以关闭这个页面，回到终端。"
            except FeishuConfigError as exc:
                result["error"] = exc
                message = f"授权失败：{exc}"
            payload = message.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    deadline = time.monotonic() + CALLBACK_TIMEOUT
    with HTTPServer((bind, port), Handler) as server:
        server.timeout = 1
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    if "code" not in result:
        raise FeishuConfigError("5 分钟内没有收到授权回调，请重新运行 `fgs auth`")
    return result["code"]  # type: ignore[return-value]
