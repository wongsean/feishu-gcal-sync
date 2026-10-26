from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import with_settings
from test_model import timed

from feishu_gcal_sync.errors import FeishuApiError, GoogleApiError, SafetyAbort
from feishu_gcal_sync.model import google_event_id, to_google_event
from feishu_gcal_sync.sync import run_sync

NOW = datetime(2024, 4, 14, tzinfo=UTC)


class FakeFeishu:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.window = None

    def primary_calendar_id(self):
        return "primary-cal"

    def instances(self, calendar_id, start, end):
        if self.error:
            raise self.error
        self.window = (start, end)
        return self.items


class FakeGoogle:
    def __init__(self, existing=None, fail_ids=()):
        self.existing = existing or []
        self.fail_ids = set(fail_ids)
        self.writes = []

    def list_managed(self, time_min, time_max):
        return self.existing

    def _write(self, op, key):
        if key in self.fail_ids:
            raise GoogleApiError(f"bad {key}", status=400)
        self.writes.append((op, key))

    def insert(self, body):
        self._write("insert", body["id"])

    def update(self, body):
        self._write("update", body["id"])

    def delete(self, event_id):
        self._write("delete", event_id)


def orphan(event_id: str) -> dict:
    return {
        "id": event_id,
        "start": {"dateTime": "2024-04-20T01:00:00Z"},
        "end": {"dateTime": "2024-04-20T02:00:00Z"},
        "extendedProperties": {"private": {"src": "fgs", "fsh": "old"}},
    }


def test_applies_plan(cfg):
    keep = timed()
    declined = timed(event_id="declined_0", self_rsvp_status="decline")
    google = FakeGoogle(existing=[orphan("stale"), orphan(google_event_id("declined_0"))])
    feishu = FakeFeishu([keep, declined])

    plan = run_sync(cfg, feishu, google, now=NOW)

    # The sync window (04-07..06-13), read from Feishu with a 31-day lookback and 1-day pad.
    assert feishu.window == (datetime(2024, 3, 7, tzinfo=UTC), datetime(2024, 6, 14, tzinfo=UTC))
    assert [w for w in google.writes if w[0] == "insert"] == [
        ("insert", to_google_event(keep)["id"])
    ]
    assert sorted(w[1] for w in google.writes if w[0] == "delete") == sorted(
        ["stale", google_event_id("declined_0")]
    )
    assert len(plan.delete) == 2


def test_dry_run_writes_nothing(cfg):
    google = FakeGoogle(existing=[orphan("stale")])
    plan = run_sync(cfg, FakeFeishu([timed()]), google, now=NOW, dry_run=True)
    assert google.writes == []
    assert len(plan.insert) == 1 and plan.delete == ["stale"]


def test_feishu_failure_touches_nothing(cfg):
    google = FakeGoogle(existing=[orphan("a")])
    with pytest.raises(FeishuApiError):
        run_sync(cfg, FakeFeishu(error=FeishuApiError("boom")), google, now=NOW)
    assert google.writes == []


def test_empty_feishu_does_not_wipe_the_mirror(cfg):
    google = FakeGoogle(existing=[orphan(f"e{i}") for i in range(5)])
    with pytest.raises(SafetyAbort):
        run_sync(cfg, FakeFeishu([]), google, now=NOW)
    assert google.writes == []

    run_sync(cfg, FakeFeishu([]), google, now=NOW, allow_empty=True)
    assert len(google.writes) == 5


def test_unreadable_instance_is_skipped_but_its_mirror_kept(cfg):
    broken = timed(event_id="broken_0", start_time={"timezone": "UTC"})
    google = FakeGoogle(existing=[orphan(google_event_id("broken_0"))])
    plan = run_sync(cfg, FakeFeishu([timed(), broken]), google, now=NOW)
    assert plan.delete == []
    assert len(plan.insert) == 1


def test_one_failed_write_does_not_block_the_rest(cfg):
    first = timed()
    second = timed(event_id="other_0", summary="另一个")
    google = FakeGoogle(fail_ids={to_google_event(first)["id"]})
    with pytest.raises(GoogleApiError, match="1 个 Google 写入失败"):
        run_sync(cfg, FakeFeishu([first, second]), google, now=NOW)
    assert google.writes == [("insert", to_google_event(second)["id"])]


def test_declined_events_can_be_kept(cfg):
    declined = timed(self_rsvp_status="decline")
    plan = run_sync(
        with_settings(cfg, skip_declined=False),
        FakeFeishu([declined]),
        FakeGoogle(),
        now=NOW,
        dry_run=True,
    )
    assert len(plan.insert) == 1


def test_cancelled_long_event_straddling_the_window_is_removed(cfg):
    trip = {
        "id": google_event_id("trip_0"),
        "start": {"date": "2024-03-30"},
        "end": {"date": "2024-04-20"},
        "extendedProperties": {"private": {"src": "fgs", "fsh": "old"}},
    }
    google = FakeGoogle(existing=[trip])
    feishu = FakeFeishu([timed()])  # the trip is gone from Feishu

    plan = run_sync(cfg, feishu, google, now=NOW, dry_run=True)

    assert feishu.window[0] <= datetime(2024, 3, 29, tzinfo=UTC)
    assert plan.delete == [trip["id"]]


def test_live_long_event_straddling_the_window_is_kept(cfg):
    item = timed(
        event_id="trip_0", start_time={"date": "2024-03-30"}, end_time={"date": "2024-04-20"}
    )
    body = to_google_event(item)
    google = FakeGoogle(existing=[body])
    plan = run_sync(cfg, FakeFeishu([item]), google, now=NOW, dry_run=True)
    assert plan.delete == [] and plan.update == [] and plan.unchanged == 1


def test_google_listing_is_padded_for_all_day_time_zones(cfg):
    google = FakeGoogle()
    google.list_managed = lambda lo, hi: google.writes.append(("list", lo, hi)) or []
    run_sync(cfg, FakeFeishu([timed()]), google, now=NOW, dry_run=True)
    assert google.writes == [
        ("list", datetime(2024, 4, 6, tzinfo=UTC), datetime(2024, 6, 14, tzinfo=UTC))
    ]
