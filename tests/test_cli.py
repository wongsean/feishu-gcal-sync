from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
import requests
from conftest import FakeResponse, FakeSession

from feishu_gcal_sync import cli
from feishu_gcal_sync.errors import FeishuQuotaExhausted, GoogleApiError, GoogleWritesFailed
from feishu_gcal_sync.feishu_auth import GRANT_LIFETIME, REFRESH_MARGIN, Token, TokenStore
from feishu_gcal_sync.fsutil import RunLock


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for name in ("FEISHU_APP_ID", "GOOGLE_CALENDAR_ID", "FGS_STATE_DIR", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "fgs.env"
    path.write_text(
        "FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=s\nGOOGLE_SA_KEY_FILE=sa.json\n"
        f"GOOGLE_CALENDAR_ID=c@group.calendar.google.com\nFGS_STATE_DIR={tmp_path / 'state'}\n"
    )
    monkeypatch.setattr(cli.GoogleCalendar, "from_key_file", classmethod(lambda c, k, i: object()))
    return path


def alerts(tmp_path) -> dict:
    path = tmp_path / "state" / cli.ALERTS_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def save_token(tmp_path, access_left: float = 7200, granted_ago: float = 0) -> None:
    now = time.time()
    TokenStore(tmp_path / "state" / cli.TOKEN_FILE).save(
        Token(
            "a",
            now + access_left,
            "r",
            now + 600000,
            "calendar:calendar:readonly",
            now - granted_ago,
        )
    )


def token_response() -> FakeResponse:
    return FakeResponse(
        200,
        {
            "code": 0,
            "access_token": "u",
            "expires_in": 7200,
            "refresh_token": "r-new",
            "refresh_token_expires_in": 604800,
            "scope": "calendar:calendar:readonly offline_access",
        },
    )


def test_sync_without_authorization_alerts(env_file, tmp_path):
    assert cli.main(["--env-file", str(env_file), "sync"]) == 1
    assert alerts(tmp_path)["feishu_reauth"]["sent"] is not None


def test_dry_run_does_not_alert(env_file, tmp_path):
    assert cli.main(["--env-file", str(env_file), "sync", "--dry-run"]) == 1
    assert alerts(tmp_path) == {}


def test_overlapping_run_is_skipped(env_file, tmp_path):
    with RunLock(tmp_path / "state" / cli.LOCK_FILE):
        assert cli.main(["--env-file", str(env_file), "sync"]) == 0
    assert alerts(tmp_path) == {}


def test_successful_run_clears_failures_and_warns_before_grant_expiry(
    env_file, tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: cli.Plan())
    state = tmp_path / "state"
    state.mkdir()
    (state / cli.ALERTS_FILE).write_text(json.dumps({"feishu_api": {"since": 1, "sent": 1}}))
    now = time.time()
    TokenStore(state / cli.TOKEN_FILE).save(
        Token(
            "a", now + 7200, "r", now + 600000, "calendar:calendar:readonly", now - GRANT_LIFETIME
        )
    )
    assert cli.main(["--env-file", str(env_file), "sync"]) == 0
    assert list(alerts(tmp_path)) == ["reauth_soon"]


def test_refuses_to_run_as_another_user(env_file, tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(cli.os, "geteuid", lambda: 4242)
    assert cli.main(["--env-file", str(env_file), "sync"]) == 2
    assert list((tmp_path / "state").iterdir()) == []


def test_broken_config_still_alerts(env_file, tmp_path):
    (tmp_path / "state").mkdir()
    env_file.write_text(env_file.read_text() + "SYNC_FUTURE_DAYS=sixty\n")
    assert cli.main(["--env-file", str(env_file), "sync"]) == 2
    assert "config" in alerts(tmp_path)


def test_quota_cooldown_reads_but_does_not_write(env_file, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: calls.append(k) or cli.Plan())
    state = tmp_path / "state"
    state.mkdir()
    now = time.time()
    open_alert = {"google_quota": {"since": now - 600, "last": now - 600, "sent": now - 600}}
    (state / cli.ALERTS_FILE).write_text(json.dumps(open_alert))
    TokenStore(state / cli.TOKEN_FILE).save(
        Token("a", now + 7200, "r", now + 600000, "calendar:calendar:readonly", now)
    )
    assert cli.main(["--env-file", str(env_file), "sync"]) == 0
    assert calls[0]["dry_run"] is True
    assert "google_quota" in alerts(tmp_path)


def test_grant_deadline_is_checked_even_when_sync_fails(env_file, tmp_path, monkeypatch):
    def failing(*a, **k):
        raise cli.SyncError("boom")

    monkeypatch.setattr(cli, "run_sync", failing)
    state = tmp_path / "state"
    now = time.time()
    TokenStore(state / cli.TOKEN_FILE).save(
        Token(
            "a", now + 7200, "r", now + 600000, "calendar:calendar:readonly", now - GRANT_LIFETIME
        )
    )
    assert cli.main(["--env-file", str(env_file), "sync"]) == 1
    assert set(alerts(tmp_path)) == {"sync_error", "reauth_soon"}


def test_cooldown_run_clears_other_failures_and_pings(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: cli.Plan())
    pings = []
    monkeypatch.setattr(cli, "_heartbeat", lambda cfg, session: pings.append(1) or True)
    state = tmp_path / "state"
    state.mkdir()
    now = time.time()
    (state / cli.ALERTS_FILE).write_text(
        json.dumps(
            {
                "google_quota": {"since": now - 600, "last": now - 600, "sent": now - 600},
                "feishu_transient": {"since": now - 300, "last": now - 300, "sent": None},
            }
        )
    )
    TokenStore(state / cli.TOKEN_FILE).save(
        Token("a", now + 7200, "r", now + 600000, "calendar:calendar:readonly", now)
    )
    assert cli.main(["--env-file", str(env_file), "sync"]) == 0
    assert list(alerts(tmp_path)) == ["google_quota"]
    assert pings == [1]


@pytest.mark.parametrize(
    ("error", "pings", "left"),
    [
        (GoogleWritesFailed("1 个 Google 写入失败"), [1], {"google_writes"}),
        (GoogleApiError("list failed"), [], {"google_api", "feishu_quota"}),
    ],
)
def test_only_rejected_events_keep_the_heartbeat_and_clear_old_failures(
    env_file, tmp_path, monkeypatch, error, pings, left
):
    def failing(*a, **k):
        raise error

    monkeypatch.setattr(cli, "run_sync", failing)
    sent = []
    monkeypatch.setattr(cli, "_heartbeat", lambda cfg, session: sent.append(1) or True)
    save_token(tmp_path)
    old = {"since": time.time() - 86400, "last": time.time() - 86400, "sent": None}
    (tmp_path / "state" / cli.ALERTS_FILE).write_text(json.dumps({"feishu_quota": old}))
    assert cli.main(["--env-file", str(env_file), "sync"]) == 1
    assert alerts(tmp_path)[error.alert_key]["sent"] is not None
    assert set(alerts(tmp_path)) == left
    assert sent == pings


def test_used_up_feishu_quota_alerts_at_once(env_file, tmp_path, monkeypatch):
    def failing(*a, **k):
        raise FeishuQuotaExhausted("飞书租户本月的 API 调用量已用完")

    monkeypatch.setattr(cli, "run_sync", failing)
    save_token(tmp_path)
    assert cli.main(["--env-file", str(env_file), "sync"]) == 1
    assert alerts(tmp_path)["feishu_quota"]["sent"] is not None


@pytest.mark.parametrize(
    ("access_left", "calls", "alert", "sent_now"),
    [
        # Too short to last a run: renewed up front, so a rejection is no glitch.
        (cli.RUN_TOKEN_MARGIN - 60, ["feishu-token", "google", "feishu"], "feishu_api", True),
        # Issued by an earlier run and rejected now: dropped, the next run renews it.
        (cli.RUN_TOKEN_MARGIN + 60, ["google", "feishu"], "feishu_transient", False),
    ],
)
def test_token_rotates_only_before_google_starts(
    env_file, tmp_path, monkeypatch, access_left, calls, alert, sent_now
):
    seen = []

    class NoMirrors:
        def list_managed(self, time_min, time_max):
            seen.append("google")
            return []

    def handler(method, url, **kwargs):
        if url.endswith("/oauth/v3/token"):
            seen.append("feishu-token")
            return token_response()
        seen.append("feishu")
        return FakeResponse(401, {"code": 99991668, "msg": "invalid access token"})

    monkeypatch.setattr(cli.requests, "Session", lambda: FakeSession(handler))
    monkeypatch.setattr(
        cli.GoogleCalendar, "from_key_file", classmethod(lambda c, k, i: NoMirrors())
    )
    save_token(tmp_path, access_left=access_left)

    assert cli.main(["--env-file", str(env_file), "sync"]) == 1
    assert seen[: len(calls)] == calls
    assert "feishu-token" not in seen[len(calls) :]
    assert TokenStore(tmp_path / "state" / cli.TOKEN_FILE).load().access_expires_at == 0
    assert (alerts(tmp_path)[alert]["sent"] is not None) == sent_now


def test_run_token_margin_outlasts_the_service_timeout():
    unit = Path(__file__).parent.parent / "deploy" / "feishu-gcal-sync.service"
    timeout = re.search(r"^TimeoutStartSec=(\d+)min$", unit.read_text(), re.M)
    # A held token must not hit the ordinary refresh margin before systemd stops the run.
    assert int(timeout.group(1)) * 60 <= cli.RUN_TOKEN_MARGIN - REFRESH_MARGIN


def test_broken_pipe_in_dry_run_is_not_an_alert(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: cli.Plan())
    monkeypatch.setattr(cli, "_print_plan", lambda plan: (_ for _ in ()).throw(BrokenPipeError()))
    monkeypatch.setattr(cli.os, "dup2", lambda *a: None)
    state = tmp_path / "state"
    now = time.time()
    TokenStore(state / cli.TOKEN_FILE).save(
        Token("a", now + 7200, "r", now + 600000, "calendar:calendar:readonly", now)
    )
    assert cli.main(["--env-file", str(env_file), "sync", "--dry-run"]) == 1
    assert alerts(tmp_path) == {}


def run_auth(env_file, monkeypatch, handler) -> int:
    monkeypatch.setattr(cli.requests, "Session", lambda: FakeSession(handler))
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda n: "st")
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "http://localhost:8765/callback?code=c&state=st"
    )
    return cli.main(["--env-file", str(env_file), "auth", "--manual"])


def test_auth_reports_saved_grant_even_if_calendar_check_fails(
    env_file, tmp_path, monkeypatch, capsys
):
    def handler(method, url, **kwargs):
        if url.endswith("/oauth/v3/token"):
            return token_response()
        return requests.ConnectionError("blip")

    assert run_auth(env_file, monkeypatch, handler) == 0
    assert "授权成功，已保存" in capsys.readouterr().out
    assert TokenStore(tmp_path / "state" / cli.TOKEN_FILE).load().refresh_token == "r-new"


def test_auth_remembers_the_calendar_under_the_new_grant(env_file, tmp_path, monkeypatch, capsys):
    def handler(method, url, **kwargs):
        if url.endswith("/oauth/v3/token"):
            return token_response()
        calendars = [{"calendar": {"calendar_id": "cal-new"}}]
        return FakeResponse(200, {"code": 0, "data": {"calendars": calendars}})

    assert run_auth(env_file, monkeypatch, handler) == 0
    assert "主日历：cal-new" in capsys.readouterr().out
    token = TokenStore(tmp_path / "state" / cli.TOKEN_FILE).load()
    remembered = json.loads((tmp_path / "state" / cli.CALENDAR_ID_FILE).read_text())
    assert remembered == {"granted_at": token.granted_at, "calendar_id": "cal-new"}
