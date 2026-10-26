"""Read side: the user's Feishu primary calendar, with recurring events expanded."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import quote

import requests

from .errors import FeishuApiError, FeishuTransientError

log = logging.getLogger(__name__)

# instance_view rejects spans of 40 days or more; stay well clear of the boundary.
CHUNK = timedelta(days=30)
# Neighbouring queries overlap so an instance on a chunk boundary is returned by at least one
# of them, whatever instance_view's (undocumented) boundary rule is. 30 + 2 * 1 < 40 days.
CHUNK_OVERLAP = timedelta(days=1)
# Below this span a window that still overflows is a real problem, not something to split.
MIN_SPAN = timedelta(hours=2)
MAX_SPLIT_DEPTH = 10

_SPAN_TOO_WIDE = 193103
_TOO_MANY_INSTANCES = 193104
# Gateway rejections of the access token itself; a fresh token is worth one retry.
_TOKEN_REJECTED = {99991663, 99991668, 99991677}
# Gateway throttling, plus calendar per-method / per-app / contention throttling.
_THROTTLED = {99991400, 190004, 190005, 190010}


class TokenSource(Protocol):
    def access_token(self) -> str: ...

    def invalidate_access_token(self) -> None: ...


class FeishuCalendar:
    def __init__(self, base_url: str, session: requests.Session, auth: TokenSource):
        self.base_url = base_url
        self.session = session
        self.auth = auth

    def primary_calendar_id(self) -> str:
        data = self._call(
            "POST", "/open-apis/calendar/v4/calendars/primary", params={"user_id_type": "open_id"}
        )
        for entry in data.get("calendars") or []:
            calendar_id = (entry.get("calendar") or {}).get("calendar_id")
            if calendar_id:
                return calendar_id
        raise FeishuApiError("飞书没有返回主日历")

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
        except _TokenRejected:
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
        if code in _THROTTLED or resp.status_code == 429 or resp.status_code >= 500:
            raise FeishuTransientError(f"飞书接口暂时不可用：{detail}")
        api_code = code if isinstance(code, int) else None
        raise FeishuApiError(f"飞书接口报错：{detail}", code=api_code)


class _TokenRejected(Exception):
    pass
