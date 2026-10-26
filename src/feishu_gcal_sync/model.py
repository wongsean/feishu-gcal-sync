"""Turn Feishu instance_view items into Google Calendar event bodies.

Mirrored: title, time, location, description, meeting link, room names and a link back to
Feishu. Attendee and organizer names are deliberately left out.
"""

from __future__ import annotations

import hashlib
import html
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Private extended properties that mark an event as owned by this tool. The sync only ever
# lists, updates or deletes events carrying them.
MARKER_KEY = "src"
MARKER_VALUE = "fgs"
SCHEMA_VERSION = "1"
# Fixed forever: Google event ids are derived from it, so changing it orphans every mirror.
ID_NAMESPACE = uuid.UUID("6f1d3c52-8a0e-4c35-9a57-2f0b6c9e4d21")
# Google publishes no limit for descriptions; ~8k characters is what clients reliably keep.
MAX_DESCRIPTION = 8000
_TRUNCATED = "\n…（内容过长，已截断）"
# Own RSVP for events; for room attendees the same values mean booking failed / room removed.
_DECLINED = ("decline", "removed")


def should_mirror(item: dict, skip_declined: bool) -> bool:
    if item.get("status") == "cancelled":
        return False
    return not (skip_declined and item.get("self_rsvp_status") in _DECLINED)


def google_event_id(feishu_event_id: str) -> str:
    """Deterministic so a retried insert after a lost response cannot create a duplicate.

    uuid5 hex uses only 0-9a-f, a subset of the base32hex alphabet Google requires.
    """
    return uuid.uuid5(ID_NAMESPACE, feishu_event_id).hex


def to_google_event(item: dict) -> dict:
    fsid = item["event_id"]
    start, end = _times(item)
    rooms = _rooms(item)
    body = {
        "id": google_event_id(fsid),
        "summary": (item.get("summary") or "").strip() or "（无主题）",
        "description": _description(item, rooms),
        "location": _location(item, rooms),
        "start": start,
        "end": end,
        "transparency": "transparent" if item.get("free_busy_status") == "free" else "opaque",
        "visibility": "private" if item.get("visibility") == "private" else "default",
        "reminders": {"useDefault": True},
        "status": "confirmed",
    }
    body["extendedProperties"] = {
        "private": {
            MARKER_KEY: MARKER_VALUE,
            "v": SCHEMA_VERSION,
            "fsid": fsid,
            "fsh": content_hash(body),
        }
    }
    return body


def content_hash(body: dict) -> str:
    payload = {k: v for k, v in body.items() if k != "extendedProperties"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def event_bounds(event: dict) -> tuple[datetime, datetime]:
    """Start and end instants of a Google event body; all-day dates count from UTC midnight."""
    return _instant(event["start"]), _instant(event["end"])


def _instant(t: dict) -> datetime:
    if t.get("date"):
        return datetime.combine(date.fromisoformat(t["date"]), datetime.min.time(), UTC)
    return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))


def _times(item: dict) -> tuple[dict, dict]:
    start = item.get("start_time") or {}
    end = item.get("end_time") or {}
    # All-day events carry `date` and no `timestamp` (their timezone is always UTC).
    if start.get("date") and not start.get("timestamp"):
        first = date.fromisoformat(start["date"])
        # Feishu reads back all-day ends as exclusive, like Google; guard against an
        # inclusive or missing end so the event never comes out empty.
        last = date.fromisoformat(end["date"]) if end.get("date") else first
        if last <= first:
            last = first + timedelta(days=1)
        return {"date": first.isoformat()}, {"date": last.isoformat()}
    start_ts = int(start["timestamp"])
    end_ts = max(int(end.get("timestamp") or start_ts), start_ts)
    return (
        _date_time(start_ts, start.get("timezone")),
        _date_time(end_ts, end.get("timezone") or start.get("timezone")),
    )


def _date_time(ts: int, tz: str | None) -> dict:
    out = {"dateTime": datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")}
    if tz and _valid_tz(tz):
        out["timeZone"] = tz
    return out


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _rooms(item: dict) -> list[str]:
    # instance_view only returns meeting rooms in `attendees`.
    rooms = []
    for attendee in item.get("attendees") or []:
        if attendee.get("type") not in (None, "resource"):
            continue
        if attendee.get("rsvp_status") in _DECLINED:
            continue
        name = (attendee.get("display_name") or "").strip()
        if name and name not in rooms:
            rooms.append(name)
    return rooms


def _location(item: dict, rooms: list[str]) -> str:
    loc = item.get("location") or {}
    name = (loc.get("name") or "").strip()
    address = (loc.get("address") or "").strip()
    if name and address and address != name:
        return f"{name}，{address}"
    return name or address or "、".join(rooms)


def _meeting_url(item: dict) -> str:
    vchat = item.get("vchat") or {}
    if vchat.get("vc_type") == "no_meeting":
        return ""
    url = (vchat.get("meeting_url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def _description(item: dict, rooms: list[str]) -> str:
    # Google renders descriptions as HTML while Feishu's are plain text, so free text is
    # escaped (otherwise "Name <mail@x.com>" loses the address). URLs are left as they are.
    footer = []
    if url := _meeting_url(item):
        footer.append(f"会议链接：{url}")
    if rooms:
        footer.append("会议室：" + html.escape("、".join(rooms), quote=False))
    if link := (item.get("app_link") or "").strip():
        footer.append(f"在飞书中打开：{link}")
    footer_text = "\n".join(footer)

    budget = MAX_DESCRIPTION - len(footer_text) - 2
    text = _escaped_within((item.get("description") or "").strip(), budget)
    return "\n\n".join(part for part in (text, footer_text) if part)


def _escaped_within(raw: str, budget: int) -> str:
    text = html.escape(raw, quote=False)
    if len(text) <= budget:
        return text
    limit = max(budget - len(_TRUNCATED), 0)
    while len(text) > limit:
        # Cut the raw text, not the escaped one, so no entity is split in half.
        raw = raw[: len(raw) - (len(text) - limit)]
        text = html.escape(raw, quote=False)
    return text + _TRUNCATED
