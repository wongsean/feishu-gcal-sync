"""Alerts: Telegram delivery plus per-failure bookkeeping so a stuck job doesn't spam."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import requests

from .fsutil import atomic_write_text

log = logging.getLogger(__name__)

# A failure that persists is re-announced at most this often.
REPEAT_EVERY = 12 * 3600
# Keys that track a condition rather than a failed run; a successful run doesn't clear them.
CONDITION_KEYS = frozenset({"reauth_soon"})


class Notifier(Protocol):
    def send(self, text: str) -> bool:
        """Deliver `text`; return False if delivery failed (already logged)."""
        ...


class LogNotifier:
    def send(self, text: str) -> bool:
        log.warning("alert (no Telegram configured): %s", text)
        return True


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, session: requests.Session):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        try:
            resp = self.session.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            # The exception text contains the URL, and with it the bot token.
            log.error("Telegram send failed: %s", str(exc).replace(self.bot_token, "***"))
            return False
        if resp.status_code != 200:
            log.error("Telegram send failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True


class Alerts:
    """Remembers which failures are active in a small JSON file in the state directory."""

    def __init__(self, path: Path, notifier: Notifier, clock: Callable[[], float] = time.time):
        self.path = path
        self.notifier = notifier
        self.clock = clock

    def fire(self, key: str, text: str, *, grace: float = 0, repeat: float = REPEAT_EVERY) -> None:
        """Report that `key` is failing. Nothing is sent until it has lasted `grace` seconds."""
        state = self._load()
        now = self.clock()
        entry = state.setdefault(key, {"since": now, "sent": None})
        entry["last"] = now
        due = entry["sent"] is None or now - entry["sent"] >= repeat
        # A failed delivery leaves `sent` unset so the next run tries again.
        if now - entry["since"] >= grace and due and self.notifier.send(text):
            entry["sent"] = now
        self._save(state)

    def resolve(self, key: str, text: str | None = None) -> None:
        state = self._load()
        entry = state.get(key)
        if entry is None:
            return
        if text and entry.get("sent") is not None and not self.notifier.send(text):
            return  # keep it, so the next run retries the notice
        del state[key]
        self._save(state)

    def resolve_failures(self, text: str, keep: frozenset[str] = frozenset()) -> None:
        """A run succeeded: clear every failure but `keep`, announcing recovery if anything
        was reported."""
        state = self._load()
        kept = CONDITION_KEYS | keep
        cleared = {k: v for k, v in state.items() if k not in kept}
        if not cleared:
            return
        reported = any(v.get("sent") is not None for v in cleared.values())
        if reported and not self.notifier.send(text):
            return  # keep them, so the next good run retries the recovery notice
        self._save({k: v for k, v in state.items() if k in kept})

    def active(self) -> dict:
        """Open alerts: key -> {"since": first seen, "last": last seen, "sent": ts | None}."""
        return self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Never let bookkeeping trouble (e.g. a root-owned file) swallow the alert itself.
            log.error("ignoring unreadable alert state %s: %s", self.path, exc)
            return {}

    def _save(self, state: dict) -> None:
        try:
            atomic_write_text(self.path, json.dumps(state, indent=2) + "\n")
        except OSError as exc:
            log.error("cannot write alert state %s: %s", self.path, exc)
