from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FakeResponse, FakeSession, queue

from feishu_gcal_sync.errors import GoogleApiError, GoogleQuotaError, GoogleTransientError
from feishu_gcal_sync.google_calendar import MAX_ATTEMPTS, GoogleCalendar

CAL = "mirror@group.calendar.google.com"
EVENTS = (
    "https://www.googleapis.com/calendar/v3/calendars/mirror%40group.calendar.google.com/events"
)


def google_error(status: int, reason: str) -> FakeResponse:
    return FakeResponse(
        status, {"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}
    )


def make(handler):
    session = FakeSession(handler)
    sleeps: list[float] = []
    return GoogleCalendar(CAL, session, sleep=sleeps.append), session, sleeps


def test_list_managed_follows_pages_and_filters_by_marker():
    gc, session, _ = make(
        queue(
            FakeResponse(200, {"items": [{"id": "a"}], "nextPageToken": "p2"}),
            FakeResponse(200, {"items": [], "nextPageToken": "p3"}),
            FakeResponse(200, {"items": [{"id": "b"}]}),
        )
    )
    events = gc.list_managed(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert [e["id"] for e in events] == ["a", "b"]
    first = session.calls[0][2]["params"]
    assert session.calls[0][1] == EVENTS
    assert first["privateExtendedProperty"] == "src=fgs"
    assert first["singleEvents"] == "true"
    assert first["timeMin"] == "2026-10-01T00:00:00Z"
    assert [c[2]["params"].get("pageToken") for c in session.calls] == [None, "p2", "p3"]


def test_insert_falls_back_to_update_when_id_exists():
    gc, session, _ = make(queue(google_error(409, "duplicate"), FakeResponse(200, {})))
    gc.insert({"id": "abc123", "summary": "x"})
    assert [(c[0], c[1]) for c in session.calls] == [
        ("POST", EVENTS),
        ("PUT", f"{EVENTS}/abc123"),
    ]
    assert all(c[2]["params"]["sendUpdates"] == "none" for c in session.calls)


def test_delete_of_missing_event_is_fine():
    gc, _, _ = make(queue(google_error(410, "deleted")))
    gc.delete("gone")


def test_rate_limits_are_retried_with_backoff():
    gc, session, sleeps = make(
        queue(
            google_error(403, "rateLimitExceeded"),
            google_error(429, "rateLimitExceeded"),
            FakeResponse(200, {}),
        )
    )
    gc.update({"id": "abc"})
    assert len(session.calls) == 3
    assert len(sleeps) == 3  # two backoffs plus the write pacing
    assert 1 <= sleeps[0] < 2 and 2 <= sleeps[1] < 3


def test_persistent_server_errors_give_up():
    gc, session, _ = make(lambda m, u, **k: FakeResponse(503, None))
    with pytest.raises(GoogleTransientError):
        gc.probe()
    assert len(session.calls) == MAX_ATTEMPTS


def test_usage_limit_stops_immediately():
    gc, session, _ = make(queue(google_error(403, "quotaExceeded")))
    with pytest.raises(GoogleQuotaError):
        gc.insert({"id": "abc"})
    assert len(session.calls) == 1


def test_client_errors_are_not_retried():
    gc, session, _ = make(queue(google_error(404, "notFound")))
    with pytest.raises(GoogleApiError) as exc:
        gc.probe()
    assert exc.value.status == 404
    assert len(session.calls) == 1


def test_probe_requires_write_access():
    gc, _, _ = make(queue(FakeResponse(200, {"accessRole": "writer", "items": []})))
    assert gc.probe() == "writer"
    gc, _, _ = make(queue(FakeResponse(200, {"accessRole": "reader", "items": []})))
    with pytest.raises(GoogleApiError, match="reader"):
        gc.probe()


def test_retryable_token_errors_are_transient():
    from google.auth.exceptions import RefreshError

    gc, session, _ = make(lambda m, u, **k: RefreshError("503 from token endpoint", retryable=True))
    with pytest.raises(GoogleTransientError):
        gc.list_managed(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert len(session.calls) == MAX_ATTEMPTS

    gc, session, _ = make(lambda m, u, **k: RefreshError("invalid_grant"))
    with pytest.raises(GoogleApiError):
        gc.probe()
    assert len(session.calls) == 1
