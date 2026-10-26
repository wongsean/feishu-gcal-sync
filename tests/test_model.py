from __future__ import annotations

import re

import pytest

from feishu_gcal_sync.model import (
    MAX_DESCRIPTION,
    content_hash,
    google_event_id,
    should_mirror,
    to_google_event,
)


def timed(**overrides) -> dict:
    item = {
        "event_id": "2cf525f0-1e67-4b04-ad4d-30b7f003903c_1713168000",
        "summary": "周会",
        "description": "议程见文档",
        "start_time": {"timestamp": "1713168000", "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": "1713171600", "timezone": "Asia/Shanghai"},
        "status": "confirmed",
        "free_busy_status": "busy",
        "visibility": "default",
        "location": {"name": "", "address": ""},
        "vchat": {"vc_type": "vc", "meeting_url": "vc.feishu.cn/j/152568231"},
        "attendees": [{"type": "resource", "display_name": "3F-会议室A"}],
        "app_link": "https://applink.feishu.cn/client/calendar/event/detail?x=1",
        "event_organizer": {"display_name": "张三"},
    }
    item.update(overrides)
    return item


def test_timed_event_mapping():
    body = to_google_event(timed())
    assert body["start"] == {"dateTime": "2024-04-15T08:00:00Z", "timeZone": "Asia/Shanghai"}
    assert body["end"] == {"dateTime": "2024-04-15T09:00:00Z", "timeZone": "Asia/Shanghai"}
    assert body["summary"] == "周会"
    assert body["location"] == "3F-会议室A"
    assert body["transparency"] == "opaque"
    assert body["visibility"] == "default"
    assert body["reminders"] == {"useDefault": True}
    assert "attendees" not in body
    private = body["extendedProperties"]["private"]
    assert private["src"] == "fgs"
    assert private["fsid"] == timed()["event_id"]
    assert private["fsh"] == content_hash(body)


def test_description_has_links_and_rooms_but_no_people():
    description = to_google_event(timed())["description"]
    assert description.startswith("议程见文档\n\n")
    assert "会议链接：https://vc.feishu.cn/j/152568231" in description
    assert "会议室：3F-会议室A" in description
    assert "在飞书中打开：https://applink.feishu.cn/" in description
    assert "张三" not in description


def test_no_meeting_link_when_vc_disabled():
    item = timed(vchat={"vc_type": "no_meeting", "meeting_url": "vc.feishu.cn/j/1"})
    assert "会议链接" not in to_google_event(item)["description"]


def test_long_description_is_truncated():
    body = to_google_event(timed(description="字" * 20000))
    assert len(body["description"]) <= MAX_DESCRIPTION
    assert "已截断" in body["description"]
    assert body["description"].endswith(timed()["app_link"])


def test_all_day_end_is_exclusive():
    item = timed(start_time={"date": "2026-10-26"}, end_time={"date": "2026-10-28"})
    body = to_google_event(item)
    assert body["start"] == {"date": "2026-10-26"}
    assert body["end"] == {"date": "2026-10-28"}


@pytest.mark.parametrize("end", [{"date": "2026-10-26"}, {}])
def test_all_day_end_never_empty(end):
    body = to_google_event(timed(start_time={"date": "2026-10-26"}, end_time=end))
    assert body["end"] == {"date": "2026-10-27"}


def test_date_with_timestamp_is_a_timed_event():
    item = timed(start_time={"date": "2024-04-15", "timestamp": "1713168000", "timezone": "UTC"})
    assert "dateTime" in to_google_event(item)["start"]


def test_free_and_private_flags():
    body = to_google_event(timed(free_busy_status="free", visibility="private"))
    assert body["transparency"] == "transparent"
    assert body["visibility"] == "private"


def test_unknown_timezone_is_dropped():
    item = timed(start_time={"timestamp": "1713168000", "timezone": "Mars/Olympus"})
    assert to_google_event(item)["start"] == {"dateTime": "2024-04-15T08:00:00Z"}


def test_location_prefers_feishu_location():
    item = timed(location={"name": "国贸三期", "address": "北京市朝阳区"})
    assert to_google_event(item)["location"] == "国贸三期，北京市朝阳区"


def test_empty_summary_gets_placeholder():
    assert to_google_event(timed(summary=""))["summary"] == "（无主题）"


def test_event_id_is_deterministic_base32hex():
    gid = google_event_id("abc_0")
    assert gid == google_event_id("abc_0") != google_event_id("abc_1")
    assert re.fullmatch(r"[0-9a-v]{5,1024}", gid)


def test_hash_tracks_content():
    assert (
        to_google_event(timed())["extendedProperties"]
        == to_google_event(timed())["extendedProperties"]
    )
    changed = to_google_event(timed(summary="周会（改期）"))
    assert changed["extendedProperties"]["private"]["fsh"] != content_hash(to_google_event(timed()))


def test_should_mirror():
    assert should_mirror(timed(), skip_declined=True)
    assert not should_mirror(timed(status="cancelled"), skip_declined=False)
    assert not should_mirror(timed(self_rsvp_status="decline"), skip_declined=True)
    assert should_mirror(timed(self_rsvp_status="decline"), skip_declined=False)
    assert should_mirror(timed(self_rsvp_status="needs_action"), skip_declined=True)


def test_failed_room_bookings_are_not_shown():
    item = timed(
        attendees=[
            {"type": "resource", "display_name": "3F-A", "rsvp_status": "accept"},
            {"type": "resource", "display_name": "3F-B", "rsvp_status": "decline"},
            {"type": "resource", "display_name": "3F-C", "rsvp_status": "removed"},
        ]
    )
    body = to_google_event(item)
    assert body["location"] == "3F-A"
    assert "3F-B" not in body["description"] and "3F-C" not in body["description"]


def test_description_text_is_html_escaped_but_links_are_not():
    body = to_google_event(timed(description="联系人：张三 <zs@corp.com> & 李四"))
    assert body["description"].startswith("联系人：张三 &lt;zs@corp.com&gt; &amp; 李四\n\n")
    assert timed()["app_link"] in body["description"]


def test_truncation_never_splits_an_entity():
    body = to_google_event(timed(description="<" * 20000))
    text = body["description"].split("\n…")[0]
    assert len(body["description"]) <= MAX_DESCRIPTION
    assert text == "&lt;" * (len(text) // 4)
