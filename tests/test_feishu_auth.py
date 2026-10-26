from __future__ import annotations

import stat
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import FakeResponse, FakeSession, queue

from feishu_gcal_sync.errors import FeishuConfigError, FeishuReauthRequired, FeishuTransientError
from feishu_gcal_sync.feishu_auth import (
    GRANT_LIFETIME,
    FeishuAuth,
    Token,
    TokenStore,
    parse_callback,
)

NOW = 1_800_000_000.0


def token_body(access: str = "u-new", refresh: str = "r-new", scope: str | None = None) -> dict:
    return {
        "code": 0,
        "access_token": access,
        "expires_in": 7200,
        "refresh_token": refresh,
        "refresh_token_expires_in": 604800,
        "token_type": "Bearer",
        "scope": scope if scope is not None else "calendar:calendar:readonly offline_access",
    }


def make_auth(cfg, handler, store=None):
    store = store or TokenStore(cfg.state_dir / "feishu_token.json")
    session = FakeSession(handler)
    sleeps: list[float] = []
    auth = FeishuAuth(cfg, session, store, clock=lambda: NOW, sleep=sleeps.append)
    return auth, session, store


def stored_token(store: TokenStore, *, access_left: float, refresh: str = "r-old") -> Token:
    token = Token(
        access_token="u-old",
        access_expires_at=NOW + access_left,
        refresh_token=refresh,
        refresh_expires_at=NOW + 86400,
        scope="calendar:calendar:readonly offline_access",
        granted_at=NOW - 100 * 86400,
    )
    store.save(token)
    return token


def test_authorize_url_requests_offline_access(cfg):
    auth, _, _ = make_auth(cfg, queue())
    url = urlsplit(auth.authorize_url("xyz"))
    assert url.netloc == "accounts.feishu.cn"
    assert url.path == "/open-apis/authen/v1/authorize"
    query = parse_qs(url.query)
    assert query["scope"] == ["calendar:calendar:readonly offline_access"]
    assert query["state"] == ["xyz"]
    assert query["redirect_uri"] == [cfg.feishu_redirect_uri]


def test_exchange_code_stores_token_privately(cfg):
    auth, session, store = make_auth(cfg, queue(FakeResponse(200, token_body())))
    token = auth.exchange_code("the-code")

    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://accounts.feishu.cn/oauth/v3/token")
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "the-code"
    assert token.granted_at == NOW
    assert token.grant_expires_at == NOW + GRANT_LIFETIME
    assert store.load() == token
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_exchange_without_refresh_token_is_a_config_error(cfg):
    body = token_body()
    del body["refresh_token"]
    auth, _, store = make_auth(cfg, queue(FakeResponse(200, body)))
    with pytest.raises(FeishuConfigError, match="offline_access"):
        auth.exchange_code("c")
    assert store.load() is None


def test_exchange_without_calendar_scope_fails_loudly(cfg):
    auth, _, _ = make_auth(cfg, queue(FakeResponse(200, token_body(scope="offline_access"))))
    with pytest.raises(FeishuConfigError, match="calendar:calendar:readonly"):
        auth.exchange_code("c")


def test_fresh_access_token_is_used_without_refreshing(cfg):
    auth, session, store = make_auth(cfg, queue())
    stored_token(store, access_left=3600)
    assert auth.access_token() == "u-old"
    assert session.calls == []


def test_missing_token_needs_auth(cfg):
    auth, _, _ = make_auth(cfg, queue())
    with pytest.raises(FeishuReauthRequired):
        auth.access_token()


def test_refresh_rotates_and_persists_new_token(cfg):
    auth, session, store = make_auth(cfg, queue(FakeResponse(200, token_body())))
    old = stored_token(store, access_left=60)

    assert auth.access_token() == "u-new"
    form = session.calls[0][2]["data"]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "r-old"
    saved = store.load()
    assert saved.refresh_token == "r-new"
    assert saved.granted_at == old.granted_at


@pytest.mark.parametrize("code", [20026, 20037, 20064, 20073])
def test_dead_refresh_token_needs_reauth(cfg, code):
    body = {"code": code, "error": "invalid_grant", "error_description": "nope"}
    auth, _, store = make_auth(cfg, queue(FakeResponse(400, body)))
    old = stored_token(store, access_left=0)
    with pytest.raises(FeishuReauthRequired):
        auth.access_token()
    assert store.load() == old


def test_lost_refresh_response_is_retried_once_with_same_token(cfg):
    auth, session, store = make_auth(
        cfg, queue(requests.ConnectionError("reset"), FakeResponse(200, token_body()))
    )
    stored_token(store, access_left=0)
    assert auth.access_token() == "u-new"
    assert [c[2]["data"]["refresh_token"] for c in session.calls] == ["r-old", "r-old"]


def test_refresh_consumed_by_lost_response_asks_for_reauth(cfg):
    used = FakeResponse(400, {"code": 20073, "error": "invalid_grant"})
    auth, _, store = make_auth(cfg, queue(requests.ReadTimeout("timeout"), used))
    stored_token(store, access_left=0)
    with pytest.raises(FeishuReauthRequired):
        auth.access_token()


def test_repeated_transient_failure_keeps_token(cfg):
    auth, _, store = make_auth(cfg, queue(FakeResponse(503, None), FakeResponse(503, None)))
    old = stored_token(store, access_left=0)
    with pytest.raises(FeishuTransientError):
        auth.access_token()
    assert store.load() == old


def test_config_errors_keep_token_and_explain(cfg):
    body = {"code": 20074, "error": "unauthorized_client"}
    auth, _, store = make_auth(cfg, queue(FakeResponse(400, body)))
    old = stored_token(store, access_left=0)
    with pytest.raises(FeishuConfigError, match="安全设置"):
        auth.access_token()
    assert store.load() == old


def test_invalidate_forces_refresh(cfg):
    auth, session, store = make_auth(cfg, queue(FakeResponse(200, token_body())))
    stored_token(store, access_left=3600)
    auth.invalidate_access_token()
    assert auth.access_token() == "u-new"
    assert len(session.calls) == 1


def test_parse_callback():
    url = "http://localhost:8765/callback?code=abc&state=s1"
    assert parse_callback(url, "s1") == "abc"
    with pytest.raises(FeishuConfigError, match="state"):
        parse_callback(url, "other")
    with pytest.raises(FeishuConfigError, match="access_denied"):
        parse_callback("http://localhost:8765/callback?error=access_denied&state=s1", "s1")


def test_no_refresh_is_attempted_without_disk_space(cfg, monkeypatch):
    auth, session, store = make_auth(cfg, queue(FakeResponse(200, token_body())))
    old = stored_token(store, access_left=0)

    def no_space(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("feishu_gcal_sync.fsutil.os.fsync", no_space)
    with pytest.raises(OSError):
        auth.access_token()
    monkeypatch.undo()
    assert session.calls == []
    assert store.load() == old
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


def test_refresh_with_odd_scope_only_warns(cfg, caplog):
    auth, _, store = make_auth(cfg, queue(FakeResponse(200, token_body(scope="offline_access"))))
    stored_token(store, access_left=0)
    assert auth.access_token() == "u-new"
    assert "lacks calendar:calendar:readonly" in caplog.text


def test_signals_are_restored_after_refresh(cfg):
    import signal

    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    auth, _, store = make_auth(cfg, queue(FakeResponse(200, token_body())))
    stored_token(store, access_left=0)
    auth.access_token()
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before


def test_hangup_is_deferred_while_the_token_rotates(cfg):
    import signal

    blocked = []

    def handler(method, url, **kwargs):
        blocked.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        return FakeResponse(200, token_body())

    auth, _, store = make_auth(cfg, handler)
    stored_token(store, access_left=0)
    auth.access_token()
    assert {signal.SIGHUP, signal.SIGTERM} <= blocked[0]
