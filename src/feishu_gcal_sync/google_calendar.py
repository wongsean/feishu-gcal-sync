"""Write side: a service account editing one Google calendar shared with it."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .errors import GoogleApiError, GoogleQuotaError, GoogleTransientError
from .model import MARKER_KEY, MARKER_VALUE

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3"
MAX_ATTEMPTS = 5
MAX_BACKOFF = 32
# Spacing between writes; keeps the first backfill well under per-user rate limits.
WRITE_PACING = 0.15

_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class GoogleCalendar:
    def __init__(
        self,
        calendar_id: str,
        session: requests.Session,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.calendar_id = calendar_id
        self.session = session
        self.sleep = sleep

    @classmethod
    def from_key_file(cls, key_file: str, calendar_id: str) -> GoogleCalendar:
        try:
            creds = service_account.Credentials.from_service_account_file(key_file, scopes=[SCOPE])
        except (OSError, ValueError) as exc:
            raise GoogleApiError(f"读取 Service Account 密钥失败（{key_file}）：{exc}") from exc
        return cls(calendar_id, AuthorizedSession(creds))

    @property
    def _events_url(self) -> str:
        return f"{API}/calendars/{quote(self.calendar_id, safe='')}/events"

    def probe(self) -> str:
        """Check the SA may write to the calendar; returns its access role."""
        data = self._request("GET", self._events_url, params={"maxResults": "1"}).json()
        role = data.get("accessRole") or ""
        if role not in ("writer", "owner"):
            raise GoogleApiError(
                f"Service Account 对这个日历的权限是 {role or '未知'}，"
                "需要共享为「做出更改并查看活动详情」"
            )
        return role

    def list_managed(self, time_min: datetime, time_max: datetime) -> list[dict]:
        """Events this tool created that overlap [time_min, time_max)."""
        params = {
            "privateExtendedProperty": f"{MARKER_KEY}={MARKER_VALUE}",
            "singleEvents": "true",
            "showDeleted": "false",
            "maxResults": "2500",
            "timeMin": _rfc3339(time_min),
            "timeMax": _rfc3339(time_max),
        }
        events: list[dict] = []
        page_token = None
        while True:
            page = {**params, "pageToken": page_token} if page_token else params
            data = self._request("GET", self._events_url, params=page).json()
            events.extend(data.get("items") or [])
            # Pages may come back short or even empty while more remain.
            page_token = data.get("nextPageToken")
            if not page_token:
                return events

    def insert(self, body: dict) -> None:
        try:
            self._request("POST", self._events_url, json=body, params={"sendUpdates": "none"})
        except GoogleApiError as exc:
            if exc.status != 409:
                raise
            # The id already exists, normally an earlier mirror that was deleted: Google keeps
            # cancelled events and their ids, so writing the full body back revives it.
            log.info("event %s already exists, updating instead", body["id"])
            self.update(body)
            return
        self.sleep(WRITE_PACING)

    def update(self, body: dict) -> None:
        url = f"{self._events_url}/{body['id']}"
        self._request("PUT", url, json=body, params={"sendUpdates": "none"})
        self.sleep(WRITE_PACING)

    def delete(self, event_id: str) -> None:
        url = f"{self._events_url}/{event_id}"
        # 404/410: already gone, which is what we wanted.
        self._request("DELETE", url, ok=(200, 204, 404, 410), params={"sendUpdates": "none"})
        self.sleep(WRITE_PACING)

    def _request(
        self, method: str, url: str, ok: tuple[int, ...] = (200,), **kwargs: object
    ) -> requests.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except (requests.RequestException, TransportError) as exc:
                error: Exception = GoogleTransientError(
                    f"Google 接口请求失败：{type(exc).__name__}"
                )
            except GoogleAuthError as exc:
                # google-auth flags token-endpoint 5xx/429 as retryable (RefreshError).
                if not getattr(exc, "retryable", False):
                    raise GoogleApiError(f"Service Account 认证失败：{exc}") from exc
                error = GoogleTransientError(
                    f"Service Account 获取 token 暂时失败：{type(exc).__name__}"
                )
            else:
                if resp.status_code in ok:
                    return resp
                reason = _reason(resp)
                detail = f"{method} {url.removeprefix(API)}: HTTP {resp.status_code} {reason}"
                if resp.status_code == 403 and reason == "quotaExceeded":
                    raise GoogleQuotaError(f"Google 日历用量超限，本轮中止：{detail}")
                rate_limited = resp.status_code == 403 and reason in _RATE_REASONS
                if not (rate_limited or resp.status_code in _RETRY_STATUSES):
                    raise GoogleApiError(
                        f"Google 接口报错：{detail} {_message(resp)}".strip(),
                        status=resp.status_code,
                    )
                error = GoogleTransientError(f"Google 接口暂时不可用：{detail}")
            if attempt == MAX_ATTEMPTS - 1:
                raise error
            delay = min(2**attempt, MAX_BACKOFF) + random.random()
            log.warning("%s; retrying in %.1fs", error, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")


def _error_body(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _reason(resp: requests.Response) -> str:
    error = _error_body(resp)
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
        return errors[0]["reason"]
    return error.get("status") or ""


def _message(resp: requests.Response) -> str:
    return _error_body(resp).get("message") or ""


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
