from __future__ import annotations

import requests
from conftest import FakeResponse, FakeSession, queue

from feishu_gcal_sync.notify import Alerts, TelegramNotifier


class Recorder:
    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self.ok = ok

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path, ok=True):
    recorder, clock = Recorder(ok), Clock()
    return Alerts(tmp_path / "alerts.json", recorder, clock=clock), recorder, clock


def test_failure_is_announced_once_then_repeated_later(tmp_path):
    alerts, sent, clock = make(tmp_path)
    alerts.fire("feishu_reauth", "need auth", repeat=3600)
    alerts.fire("feishu_reauth", "need auth", repeat=3600)
    assert sent.sent == ["need auth"]
    clock.now += 3600
    alerts.fire("feishu_reauth", "need auth", repeat=3600)
    assert len(sent.sent) == 2


def test_transient_failures_wait_out_the_grace_period(tmp_path):
    alerts, sent, clock = make(tmp_path)
    alerts.fire("google_transient", "flaky", grace=3600)
    clock.now += 1800
    alerts.fire("google_transient", "flaky", grace=3600)
    assert sent.sent == []
    clock.now += 1800
    alerts.fire("google_transient", "flaky", grace=3600)
    assert sent.sent == ["flaky"]


def test_recovery_only_announced_for_reported_failures(tmp_path):
    alerts, sent, _ = make(tmp_path)
    alerts.fire("google_transient", "flaky", grace=3600)
    alerts.resolve_failures("recovered")
    assert sent.sent == []
    assert alerts.active() == {}

    alerts.fire("feishu_api", "broken")
    alerts.resolve_failures("recovered")
    assert sent.sent == ["broken", "recovered"]


def test_success_keeps_condition_alerts(tmp_path):
    alerts, _, _ = make(tmp_path)
    alerts.fire("reauth_soon", "renew soon")
    alerts.fire("feishu_api", "broken")
    alerts.resolve_failures("recovered")
    assert list(alerts.active()) == ["reauth_soon"]


def test_failed_delivery_is_retried_next_run(tmp_path):
    alerts, sent, _ = make(tmp_path, ok=False)
    alerts.fire("feishu_api", "broken")
    alerts.fire("feishu_api", "broken")
    assert len(sent.sent) == 2


def test_telegram_errors_do_not_leak_the_token():
    url_error = requests.ConnectionError("https://api.telegram.org/botSECRET123/sendMessage down")
    session = FakeSession(queue(url_error, FakeResponse(200, {"ok": True})))
    notifier = TelegramNotifier("SECRET123", "42", session)
    assert notifier.send("hi") is False
    assert notifier.send("hi") is True
    assert session.calls[1][2]["json"] == {
        "chat_id": "42",
        "text": "hi",
        "disable_web_page_preview": True,
    }


def test_recovery_notice_is_retried_when_delivery_fails(tmp_path):
    alerts, sent, _ = make(tmp_path)
    alerts.fire("feishu_api", "broken")
    sent.ok = False
    alerts.resolve_failures("recovered")
    assert "feishu_api" in alerts.active()
    sent.ok = True
    alerts.resolve_failures("recovered")
    assert alerts.active() == {}
    assert sent.sent == ["broken", "recovered", "recovered"]


def test_unreadable_state_does_not_block_alerts(tmp_path):
    alerts, sent, _ = make(tmp_path)
    alerts.path.write_text("{}")
    alerts.path.chmod(0)
    try:
        alerts.fire("feishu_api", "broken")
    finally:
        alerts.path.chmod(0o600)
    assert sent.sent == ["broken"]
