"""Read side: the user's Feishu primary calendar, with recurring events expanded."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import requests

from .errors import FeishuApiError, FeishuQuotaExhausted, FeishuTransientError
from .fsutil import atomic_write_text

log = logging.getLogger(__name__)

# instance_view rejects spans of 40 days or more. 34 days keeps the default read span
# (99 days, a little more when mirrors stretch it) at 3 queries, each counted against the
# tenant's monthly API allowance.
CHUNK = timedelta(days=34)
# Neighbouring queries overlap so an instance on a chunk boundary is returned by at least one
# of them, whatever instance_view's (undocumented) boundary rule is. 34 + 2 * 1 < 40 days.
CHUNK_OVERLAP = timedelta(days=1)
# Below this span a window that still overflows is a real problem, not something to split.
MIN_SPAN = timedelta(hours=2)
MAX_SPLIT_DEPTH = 10

_SPAN_TOO_WIDE = 193103
_TOO_MANY_INSTANCES = 193104
# The calendar id is no good (any more): not found, invalid, no access, deleted, wrong type.
_CALENDAR_UNUSABLE = {191000, 191001, 191002, 191003, 191004}
# Gateway rejections of the access token itself; a fresh token is worth one retry.
_TOKEN_REJECTED = {99991663, 99991668, 99991677}
# Gateway throttling, plus calendar per-method / per-app / contention throttling.
_THROTTLED = {99991400, 190004, 190005, 190010}
# The tenant's monthly API allowance is used up (HTTP 429 too, but not throttling).
_TENANT_QUOTA_EXHAUSTED = 99991403


class TokenSource(Protocol):
    def access_token(self) -> str: ...

    def invalidate_access_token(self) -> None: ...


class FeishuCalendar:
    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        auth: TokenSource,
        calendar_id_file: Path | None = None,
        granted_at: float | None = None,
    ):
        self.base_url = base_url
        self.session = session
        self.auth = auth
        # Remembers the primary calendar id between runs, tagged with the grant (its
        # granted_at) it was looked up under: any other grant, possibly another user's,
        # looks it up afresh.
        self.calendar_id_file = calendar_id_file
        self.granted_at = granted_at

    def primary_calendar_id(self) -> str:
        """Ask Feishu for the user's primary calendar, and remember it in calendar_id_file."""
        data = self._call(
            "POST", "/open-apis/calendar/v4/calendars/primary", params={"user_id_type": "open_id"}
        )
        for entry in data.get("calendars") or []:
            calendar_id = (entry.get("calendar") or {}).get("calendar_id")
            if calendar_id:
                self._remember(calendar_id)
                return calendar_id
        raise FeishuApiError("飞书没有返回主日历")

    def primary_instances(self, start: datetime, end: datetime) -> list[dict]:
        """instances() of the user's primary calendar. Its remembered id saves a call per run;
        one Feishu no longer accepts is looked up again."""
        cached = self._remembered()
        calendar_id = cached or self.primary_calendar_id()
        try:
            return self.instances(calendar_id, start, end)
        except FeishuApiError as exc:
            if not cached or exc.code not in _CALENDAR_UNUSABLE:
                raise
            log.warning("remembered calendar %s is unusable (%s), looking it up again", cached, exc)
        return self.instances(self.primary_calendar_id(), start, end)

    def _remembered(self) -> str | None:
        if self.calendar_id_file is None:
            return None
        try:
            saved = json.loads(self.calendar_id_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self.calendar_id_file, exc)
            return None
        if not isinstance(saved, dict) or saved.get("granted_at") != self.granted_at:
            return None
        calendar_id = saved.get("calendar_id")
        return calendar_id if isinstance(calendar_id, str) and calendar_id else None

    def _remember(self, calendar_id: str) -> None:
        if self.calendar_id_file is None:
            return
        saved = {"granted_at": self.granted_at, "calendar_id": calendar_id}
        try:
            atomic_write_text(self.calendar_id_file, json.dumps(saved) + "\n")
        except OSError as exc:
            # Only costs the lookup again next run.
            log.warning("cannot write %s: %s", self.calendar_id_file, exc)

    def instances(self, calendar_id: str, start: datetime, end: datetime) -> list[dict]:
        """All instances in [start, end), deduplicated across the query chunks."""
        found: dict[str, dict] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + CHUNK, end)
            lo = max(start, cursor - CHUNK_OVERLAP)
            hi = min(end, chunk_end + CHUNK_OVERLAP)
            for item in self._instance_view(calendar_id, lo, hi, depth=0):
                event_id = item.get("event_id")
                if event_id:
                    found.setdefault(event_id, item)
            cursor = chunk_end
        return list(found.values())

    def _instance_view(
        self, calendar_id: str, start: datetime, end: datetime, depth: int
    ) -> list[dict]:
        calendar = quote(calendar_id, safe="")
        path = f"/open-apis/calendar/v4/calendars/{calendar}/events/instance_view"
        params = {
            "start_time": str(int(start.timestamp())),
            "end_time": str(int(end.timestamp())),
            "user_id_type": "open_id",
        }
        try:
            data = self._call("GET", path, params=params)
        except FeishuApiError as exc:
            splittable = exc.code in (_SPAN_TOO_WIDE, _TOO_MANY_INSTANCES)
            if not splittable or end - start <= MIN_SPAN or depth >= MAX_SPLIT_DEPTH:
                raise
            mid = start + (end - start) / 2
            margin = min(timedelta(hours=1), (end - start) / 4)
            log.info("instance_view %s for %s..%s, splitting", exc.code, start, end)
            return self._instance_view(
                calendar_id, start, mid + margin, depth + 1
            ) + self._instance_view(calendar_id, mid - margin, end, depth + 1)
        return data.get("items") or []

    def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        try:
            return self._call_once(method, path, params)
        except _TokenRejected as exc:
            log.warning("Feishu rejected the access token: %s", exc)
            self.auth.invalidate_access_token()
            try:
                return self._call_once(method, path, params)
            except _TokenRejected as exc:
                raise FeishuApiError(f"飞书拒绝了刚刷新的 access_token：{exc}") from None

    def _call_once(self, method: str, path: str, params: dict | None) -> dict:
        headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
        kwargs: dict = {"params": params, "headers": headers, "timeout": 30}
        if method == "POST":
            kwargs["json"] = {}
        try:
            resp = self.session.request(method, self.base_url + path, **kwargs)
        except requests.RequestException as exc:
            raise FeishuTransientError(f"飞书接口请求失败：{type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code = body.get("code")
        if code == 0:
            return body.get("data") or {}
        detail = f"{path}: code={code} msg={body.get('msg')} (HTTP {resp.status_code})"
        if code in _TOKEN_REJECTED:
            raise _TokenRejected(detail)
        if code == _TENANT_QUOTA_EXHAUSTED:
            raise FeishuQuotaExhausted(
                "飞书租户本月的 API 调用量已用完（基础免费版所有自建应用共用每月上限），"
                "下月 1 号恢复前无法同步，用量见管理后台「费用中心 → 权益数据」：" + detail
            )
        if code in _THROTTLED or resp.status_code == 429 or resp.status_code >= 500:
            raise FeishuTransientError(f"飞书接口暂时不可用：{detail}")
        api_code = code if isinstance(code, int) else None
        raise FeishuApiError(f"飞书接口报错：{detail}", code=api_code)


class _TokenRejected(Exception):
    pass
