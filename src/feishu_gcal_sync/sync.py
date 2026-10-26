"""One reconcile run: list the Google mirror, read Feishu, apply the difference."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import Config
from .errors import GoogleApiError, SafetyAbort
from .feishu_calendar import FeishuCalendar
from .google_calendar import GoogleCalendar
from .model import event_bounds, google_event_id, should_mirror, to_google_event
from .reconcile import Plan, build_plan

log = logging.getLogger(__name__)

# Google matches all-day events against the listing range in the calendar's time zone, while
# the window is in UTC; list a little wider so every mirror of a wanted event is seen.
LIST_PAD = timedelta(days=1)
# Margin around every mirror's own time span when asking Feishu about it.
FETCH_PAD = timedelta(days=1)
# Feishu is also read this far before the window. If instance_view only returns instances
# that *start* in the queried range (undocumented), multi-day events already in progress
# would otherwise never be found.
LOOKBACK = timedelta(days=31)
# How far beyond the window Feishu is read to vet very long mirrors; beyond it they are kept.
MAX_REACH = timedelta(days=400)
# With Feishu returning nothing at all, refuse to wipe a mirror bigger than this.
EMPTY_SOURCE_GUARD = 3


def sync_window(cfg: Config, now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(days=cfg.past_days), now + timedelta(days=cfg.future_days)


def feishu_span(
    window: tuple[datetime, datetime], existing: list[dict]
) -> tuple[datetime, datetime]:
    """The span to read from Feishu: the window plus LOOKBACK, stretched over every listed
    mirror.

    A long event that started before the window (or ends after it) is then still checked
    against its source, so cancelling or declining it removes the mirror.
    """
    lo, hi = window[0] - LOOKBACK, window[1] + FETCH_PAD
    for event in existing:
        try:
            start, end = event_bounds(event)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        lo, hi = min(lo, start - FETCH_PAD), max(hi, end + FETCH_PAD)
    return max(lo, window[0] - MAX_REACH), min(hi, window[1] + MAX_REACH)


def run_sync(
    cfg: Config,
    feishu: FeishuCalendar,
    google: GoogleCalendar,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> Plan:
    window = sync_window(cfg, now or datetime.now(UTC))
    existing = google.list_managed(window[0] - LIST_PAD, window[1] + LIST_PAD)
    span = feishu_span(window, existing)
    calendar_id = feishu.primary_calendar_id()
    # Any Feishu failure raises before anything is written: a failed read must never be
    # mistaken for an empty calendar.
    items = feishu.instances(calendar_id, *span)

    wanted: dict[str, dict] = {}
    protected: set[str] = set()
    for item in items:
        if not should_mirror(item, cfg.skip_declined):
            continue
        try:
            body = to_google_event(item)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping unreadable Feishu instance %s: %r", item.get("event_id"), exc)
            if item.get("event_id"):
                protected.add(google_event_id(item["event_id"]))
            continue
        wanted[body["id"]] = body

    if not items and len(existing) >= EMPTY_SOURCE_GUARD and not allow_empty:
        raise SafetyAbort(
            f"飞书在同步范围内没有返回任何日程，而 Google 上有 {len(existing)} 个镜像事件，"
            "为防误删本轮未做任何修改。如果确实清空了，请用运行定时任务的同一用户"
            "（如 sudo -u fgs）执行 `fgs sync --allow-empty`"
        )

    plan = build_plan(wanted, existing, window, span, frozenset(protected))
    log.info(
        "feishu=%d (%s..%s) mirrored=%d %s",
        len(items),
        span[0].date(),
        span[1].date(),
        len(existing),
        plan.summary(),
    )
    if dry_run:
        return plan

    failures: list[str] = []
    for op, arg in (
        [(google.insert, body) for body in plan.insert]
        + [(google.update, body) for body in plan.update]
        + [(google.delete, event_id) for event_id in plan.delete]
    ):
        try:
            op(arg)
        except GoogleApiError as exc:
            # One bad event shouldn't block the rest; quota and transient errors still abort.
            log.error("%s failed: %s", op.__name__, exc)
            failures.append(str(exc))
    if failures:
        raise GoogleApiError(f"{len(failures)} 个 Google 写入失败，第一个：{failures[0]}")
    return plan
