from __future__ import annotations

import copy
from datetime import UTC, datetime

from test_model import timed

from feishu_gcal_sync.model import to_google_event
from feishu_gcal_sync.reconcile import build_plan

WINDOW = (datetime(2024, 4, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC))


def as_listed(body: dict) -> dict:
    """What Google hands back: offsets in the calendar's zone, defaults omitted."""
    event = copy.deepcopy(body)
    event["start"] = {"dateTime": "2024-04-15T16:00:00+08:00", "timeZone": "Asia/Shanghai"}
    event["end"] = {"dateTime": "2024-04-15T17:00:00+08:00", "timeZone": "Asia/Shanghai"}
    event.pop("transparency")
    event.pop("visibility")
    event["etag"] = '"1"'
    return event


def mirror(event_id: str, start: str, end: str) -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "extendedProperties": {"private": {"src": "fgs", "fsh": "x"}},
    }


def test_new_event_is_inserted():
    body = to_google_event(timed())
    plan = build_plan({body["id"]: body}, [], WINDOW, WINDOW)
    assert plan.insert == [body]
    assert not plan.update and not plan.delete


def test_unchanged_event_is_left_alone():
    body = to_google_event(timed())
    plan = build_plan({body["id"]: body}, [as_listed(body)], WINDOW, WINDOW)
    assert plan.unchanged == 1
    assert not plan.insert and not plan.update and not plan.delete


def test_feishu_change_updates():
    old = to_google_event(timed())
    new = to_google_event(timed(summary="周会（改期）"))
    plan = build_plan({new["id"]: new}, [as_listed(old)], WINDOW, WINDOW)
    assert plan.update == [new]


def test_edit_on_google_is_overwritten():
    body = to_google_event(timed())
    edited = as_listed(body)
    edited["summary"] = "我改过的标题"
    assert build_plan({body["id"]: body}, [edited], WINDOW, WINDOW).update == [body]

    moved = as_listed(body)
    moved["start"] = {"dateTime": "2024-04-15T17:00:00+08:00"}
    assert build_plan({body["id"]: body}, [moved], WINDOW, WINDOW).update == [body]


def test_orphan_inside_window_is_deleted():
    orphan = mirror("dead", "2024-04-10T01:00:00Z", "2024-04-10T02:00:00Z")
    assert build_plan({}, [orphan], WINDOW, WINDOW).delete == ["dead"]


def test_orphan_crossing_window_edge_is_kept():
    edge = mirror("edge", "2024-03-31T23:00:00Z", "2024-04-01T01:00:00Z")
    assert build_plan({}, [edge], WINDOW, WINDOW).delete == []


def test_protected_ids_are_not_deleted():
    orphan = mirror("unreadable", "2024-04-10T01:00:00Z", "2024-04-10T02:00:00Z")
    assert build_plan({}, [orphan], WINDOW, WINDOW, frozenset({"unreadable"})).delete == []


def test_straddling_orphan_is_deleted_once_its_span_was_checked():
    trip = mirror("trip", "2024-03-25T00:00:00Z", "2024-04-10T00:00:00Z")
    checked = (datetime(2024, 3, 24, tzinfo=UTC), WINDOW[1])
    assert build_plan({}, [trip], WINDOW, checked).delete == ["trip"]


def test_source_outside_window_is_kept_and_followed():
    body = to_google_event(timed())
    later = (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 4, 1, tzinfo=UTC))
    # Not created when it lies outside the window...
    assert build_plan({body["id"]: body}, [], later, WINDOW).insert == []
    # ...but an existing mirror is neither deleted nor left stale.
    stale = as_listed(body)
    stale["summary"] = "旧标题"
    plan = build_plan({body["id"]: body}, [stale], later, WINDOW)
    assert plan.update == [body] and plan.delete == []
