"""Diff the desired mirror against what is on Google and decide the writes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .model import event_bounds

log = logging.getLogger(__name__)


@dataclass
class Plan:
    insert: list[dict] = field(default_factory=list)
    update: list[dict] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    unchanged: int = 0

    def summary(self) -> str:
        return (
            f"insert={len(self.insert)} update={len(self.update)} "
            f"delete={len(self.delete)} unchanged={self.unchanged}"
        )


def build_plan(
    wanted: dict[str, dict],
    existing: list[dict],
    window: tuple[datetime, datetime],
    checked: tuple[datetime, datetime],
    protected: frozenset[str] = frozenset(),
) -> Plan:
    """Decide the writes.

    wanted: Google id -> body for every mirrorable Feishu instance read this run; it can
        reach beyond the sync window.
    existing: the managed events Google listed around the window.
    window: new mirrors are only created for instances overlapping it.
    checked: the span Feishu was asked about. A mirror lying fully inside it whose id is not
        wanted has no source there any more (cancelled, declined, deleted, or moved out of
        the span) and is deleted; one moved out comes back when it re-enters the window.
    protected: ids never deleted (their Feishu source could not be read this run).
    """
    plan = Plan()
    current = {event["id"]: event for event in existing}
    for event_id, body in wanted.items():
        event = current.get(event_id)
        if event is None:
            if _overlaps(body, window):
                plan.insert.append(body)
        elif needs_update(event, body):
            plan.update.append(body)
        else:
            plan.unchanged += 1
    for event_id, event in current.items():
        if event_id in wanted or event_id in protected:
            continue
        # A mirror is only provably orphaned when Feishu was asked about its whole time span:
        # instance_view's boundary rule is undocumented.
        if _inside(event, checked):
            plan.delete.append(event_id)
    return plan


def needs_update(event: dict, body: dict) -> bool:
    stored = ((event.get("extendedProperties") or {}).get("private") or {}).get("fsh")
    if stored != body["extendedProperties"]["private"]["fsh"]:
        return True
    return _drifted(event, body)


def _drifted(event: dict, body: dict) -> bool:
    """Whether someone edited the mirror on Google. Description edits are not detected,
    because Google may rewrite descriptions edited in its UI as HTML."""
    if event.get("status", "confirmed") != "confirmed":
        return True
    for key, default in (("summary", ""), ("location", ""), ("transparency", "opaque")):
        if (event.get(key) or default) != (body.get(key) or default):
            return True
    if (event.get("visibility") or "default") != body["visibility"]:
        return True
    try:
        return event_bounds(event) != event_bounds(body) or _is_all_day(event) != _is_all_day(body)
    except (AttributeError, KeyError, TypeError, ValueError):
        return True


def _is_all_day(event: dict) -> bool:
    return bool((event.get("start") or {}).get("date"))


def _overlaps(body: dict, window: tuple[datetime, datetime]) -> bool:
    start, end = event_bounds(body)
    return start < window[1] and end > window[0]


def _inside(event: dict, window: tuple[datetime, datetime]) -> bool:
    try:
        start, end = event_bounds(event)
    except (AttributeError, KeyError, TypeError, ValueError):
        log.warning("cannot read times of Google event %s, leaving it alone", event.get("id"))
        return False
    return window[0] <= start and end <= window[1]
