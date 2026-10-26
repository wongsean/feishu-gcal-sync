from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from conftest import FakeResponse, FakeSession

from feishu_gcal_sync.errors import FeishuApiError, FeishuQuotaExhausted, FeishuTransientError
from feishu_gcal_sync.feishu_calendar import FeishuCalendar

START = datetime(2026, 10, 1, tzinfo=UTC)


class FakeAuth:
    def __init__(self):
        self.tokens = ["t1", "t2", "t3"]
        self.invalidated = 0

    def access_token(self) -> str:
        return self.tokens[self.invalidated]

    def invalidate_access_token(self) -> None:
        self.invalidated += 1


def ok(data: dict) -> FakeResponse:
    return FakeResponse(200, {"code": 0, "msg": "success", "data": data})


def span(kwargs: dict) -> tuple[int, int]:
    params = kwargs["params"]
    return int(params["start_time"]), int(params["end_time"])


def test_primary_calendar_id():
    session = FakeSession(
        lambda m, u, **k: ok(
            {"calendars": [{"calendar": {"calendar_id": "feishu.cn_abc@group.calendar.feishu.cn"}}]}
        )
    )
    cal = FeishuCalendar("https://open.feishu.cn", session, FakeAuth())
    assert cal.primary_calendar_id() == "feishu.cn_abc@group.calendar.feishu.cn"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary"
    assert kwargs["headers"]["Authorization"] == "Bearer t1"


@pytest.mark.parametrize("days", [99, 102])
def test_instances_are_queried_in_overlapping_chunks_and_deduplicated(days):
    # 99 days is the default read span; mirrors ending past the window stretch it a little.
    end = START + timedelta(days=days)
    shared = {"event_id": "multi_0", "summary": "spans a chunk boundary"}

    def handler(method, url, **kwargs):
        lo, hi = span(kwargs)
        assert hi - lo < timedelta(days=40).total_seconds()
        return ok({"items": [shared, {"event_id": f"e_{lo}"}]})

    session = FakeSession(handler)
    items = FeishuCalendar("https://open.feishu.cn", session, FakeAuth()).instances(
        "cal@x", START, end
    )
    spans = [span(c[2]) for c in session.calls]
    assert spans[0][0] == int(START.timestamp())
    assert spans[-1][1] == int(end.timestamp())
    assert all(b[0] < a[1] for a, b in zip(spans, spans[1:], strict=False))
    assert len(spans) == 3
    assert [i["event_id"] for i in items].count("multi_0") == 1
    assert len(items) == 4
    assert unquote(session.calls[0][1]).endswith("/calendars/cal@x/events/instance_view")


def test_calendar_throttling_is_transient():
    session = FakeSession(lambda m, u, **k: FakeResponse(400, {"code": 190005, "msg": "app limit"}))
    cal = FeishuCalendar("https://open.feishu.cn", session, FakeAuth())
    with pytest.raises(FeishuTransientError):
        cal.instances("c", START, START + timedelta(days=1))


def test_used_up_monthly_quota_is_not_mistaken_for_throttling():
    body = {"code": 99991403, "msg": "app tenant api call limit exceeded"}
    session = FakeSession(lambda m, u, **k: FakeResponse(429, body))
    cal = FeishuCalendar("https://open.feishu.cn", session, FakeAuth())
    with pytest.raises(FeishuQuotaExhausted, match="下月 1 号"):
        cal.instances("c", START, START + timedelta(days=1))
    assert not FeishuQuotaExhausted.transient


GRANT = 1_700_000_000.0
DAY = (START, START + timedelta(days=1))


def calendar_server(primary: str, rejected: dict[str, int] | None = None):
    """Answers calendars/primary with `primary`, and instance_view with one item, or with
    the error code `rejected` maps that calendar to."""
    rejected = rejected or {}

    def handler(method, url, **kwargs):
        if url.endswith("/calendars/primary"):
            return ok({"calendars": [{"calendar": {"calendar_id": primary}}]})
        code = rejected.get(queried_calendar(url))
        if code:
            return FakeResponse(400, {"code": code, "msg": "rejected"})
        return ok({"items": [{"event_id": "e_0"}]})

    return handler


def queried_calendar(url: str) -> str:
    if url.endswith("/calendars/primary"):
        return "primary"
    return unquote(url).split("/calendars/")[1].split("/")[0]


def queried(session: FakeSession) -> list[str]:
    return [queried_calendar(url) for _, url, _ in session.calls]


def remember(path, calendar_id: str, granted_at: float = GRANT) -> None:
    path.write_text(json.dumps({"granted_at": granted_at, "calendar_id": calendar_id}))


def primary(session: FakeSession, path) -> FeishuCalendar:
    return FeishuCalendar("https://open.feishu.cn", session, FakeAuth(), path, GRANT)


def test_primary_calendar_id_is_remembered_between_runs(tmp_path):
    remembered = tmp_path / "calendar_id"
    first = FakeSession(calendar_server("cal-a"))
    assert primary(first, remembered).primary_instances(*DAY) == [{"event_id": "e_0"}]
    assert queried(first) == ["primary", "cal-a"]

    second = FakeSession(calendar_server("cal-a"))
    assert primary(second, remembered).primary_instances(*DAY) == [{"event_id": "e_0"}]
    assert queried(second) == ["cal-a"]


def test_calendar_remembered_under_another_grant_is_looked_up_again(tmp_path):
    remembered = tmp_path / "calendar_id"
    remember(remembered, "someone-elses", granted_at=GRANT - 86400)
    session = FakeSession(calendar_server("cal-a"))
    primary(session, remembered).primary_instances(*DAY)
    assert queried(session) == ["primary", "cal-a"]
    assert json.loads(remembered.read_text()) == {"granted_at": GRANT, "calendar_id": "cal-a"}


@pytest.mark.parametrize("code", [191000, 191001, 191002, 191003, 191004])
def test_unusable_remembered_calendar_is_looked_up_again(tmp_path, code):
    remembered = tmp_path / "calendar_id"
    remember(remembered, "cal-gone")
    session = FakeSession(calendar_server("cal-new", rejected={"cal-gone": code}))
    assert primary(session, remembered).primary_instances(*DAY) == [{"event_id": "e_0"}]
    assert queried(session) == ["cal-gone", "primary", "cal-new"]
    assert json.loads(remembered.read_text())["calendar_id"] == "cal-new"


def test_calendar_still_unusable_after_a_fresh_lookup_fails_once(tmp_path):
    remembered = tmp_path / "calendar_id"
    remember(remembered, "cal-x")
    session = FakeSession(calendar_server("cal-x", rejected={"cal-x": 191002}))
    with pytest.raises(FeishuApiError):
        primary(session, remembered).primary_instances(*DAY)
    assert queried(session) == ["cal-x", "primary", "cal-x"]


def test_other_errors_do_not_look_the_calendar_up_again(tmp_path):
    remembered = tmp_path / "calendar_id"
    remember(remembered, "cal-a")
    session = FakeSession(calendar_server("cal-a", rejected={"cal-a": 190002}))
    with pytest.raises(FeishuApiError):
        primary(session, remembered).primary_instances(*DAY)
    assert queried(session) == ["cal-a"]


def test_unusable_fresh_calendar_is_not_retried(tmp_path):
    session = FakeSession(calendar_server("cal-x", rejected={"cal-x": 191002}))
    with pytest.raises(FeishuApiError):
        primary(session, tmp_path / "calendar_id").primary_instances(*DAY)
    assert queried(session) == ["primary", "cal-x"]


@pytest.mark.parametrize("damage", ["directory", "garbage"])
def test_broken_calendar_file_only_costs_a_lookup(tmp_path, damage):
    remembered = tmp_path / "calendar_id"
    if damage == "directory":
        remembered.mkdir()  # unreadable and unwritable
    else:
        remembered.write_bytes(b"\xff not json")
    session = FakeSession(calendar_server("cal-a"))
    assert primary(session, remembered).primary_instances(*DAY) == [{"event_id": "e_0"}]
    assert queried(session) == ["primary", "cal-a"]


def test_too_many_instances_splits_the_window():
    end = START + timedelta(days=10)
    first = int(START.timestamp())

    def handler(method, url, **kwargs):
        lo, hi = span(kwargs)
        if lo == first and hi == int(end.timestamp()):
            return FakeResponse(400, {"code": 193104, "msg": "exceeded the maximum instance"})
        return ok({"items": [{"event_id": f"e_{lo}"}]})

    session = FakeSession(handler)
    items = FeishuCalendar("https://open.feishu.cn", session, FakeAuth()).instances("c", START, end)
    assert len(session.calls) == 3
    assert len(items) == 2


def test_other_api_errors_propagate():
    session = FakeSession(lambda m, u, **k: FakeResponse(400, {"code": 191002, "msg": "no auth"}))
    cal = FeishuCalendar("https://open.feishu.cn", session, FakeAuth())
    with pytest.raises(FeishuApiError) as exc:
        cal.instances("c", START, START + timedelta(days=1))
    assert exc.value.code == 191002


def test_server_errors_are_transient():
    session = FakeSession(lambda m, u, **k: FakeResponse(502, None))
    cal = FeishuCalendar("https://open.feishu.cn", session, FakeAuth())
    with pytest.raises(FeishuTransientError):
        cal.primary_calendar_id()


def test_rejected_token_is_refreshed_once():
    responses = [FakeResponse(401, {"code": 99991668, "msg": "invalid token"}), ok({"items": []})]
    session = FakeSession(lambda m, u, **k: responses.pop(0))
    auth = FakeAuth()
    FeishuCalendar("https://open.feishu.cn", session, auth).instances(
        "c", START, START + timedelta(days=1)
    )
    assert auth.invalidated == 1
    assert [c[2]["headers"]["Authorization"] for c in session.calls] == ["Bearer t1", "Bearer t2"]
