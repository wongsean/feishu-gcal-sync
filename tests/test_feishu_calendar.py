from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from conftest import FakeResponse, FakeSession

from feishu_gcal_sync.errors import FeishuApiError, FeishuTransientError
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


def test_instances_are_queried_in_overlapping_chunks_and_deduplicated():
    end = START + timedelta(days=67)
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
