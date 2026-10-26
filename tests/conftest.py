from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from feishu_gcal_sync.config import Config


class FakeResponse:
    def __init__(self, status_code: int = 200, body: object = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> object:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Answers requests from a handler and records every call."""

    def __init__(self, handler: Callable[..., FakeResponse | Exception]):
        self.handler = handler
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        result = self.handler(method, url, **kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        return self.request("GET", url, **kwargs)


def queue(*responses: FakeResponse | Exception) -> Callable[..., FakeResponse | Exception]:
    """Handler that returns the given responses in order."""
    pending = list(responses)

    def handler(method: str, url: str, **kwargs: object) -> FakeResponse | Exception:
        assert pending, f"unexpected request {method} {url}"
        return pending.pop(0)

    return handler


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_brand="feishu",
        feishu_redirect_uri="http://localhost:8765/callback",
        google_sa_key_file="sa.json",
        google_calendar_id="mirror@group.calendar.google.com",
        state_dir=tmp_path / "state",
        past_days=7,
        future_days=60,
        skip_declined=True,
        reauth_warn_days=14,
        telegram_bot_token="",
        telegram_chat_id="",
        heartbeat_url="",
    )


def with_settings(cfg: Config, **changes: object) -> Config:
    return replace(cfg, **changes)
