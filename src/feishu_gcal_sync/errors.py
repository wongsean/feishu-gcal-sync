"""Failure categories. Each maps to one alert key so a persistent failure sends one message."""

from __future__ import annotations


class SyncError(Exception):
    alert_key = "sync_error"
    # Transient failures only alert once they have persisted for a while.
    transient = False


class FeishuReauthRequired(SyncError):
    """The stored refresh_token is gone, expired, used or revoked: run `fgs auth` again."""

    alert_key = "feishu_reauth"


class FeishuConfigError(SyncError):
    """App or tenant configuration problem that re-authorizing will not fix."""

    alert_key = "feishu_config"


class FeishuTransientError(SyncError):
    alert_key = "feishu_transient"
    transient = True


class FeishuApiError(SyncError):
    alert_key = "feishu_api"

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class FeishuQuotaExhausted(SyncError):
    """The tenant's monthly OpenAPI allowance (shared by all its self-built apps on the free
    edition) is used up; calls fail until the 1st of next month."""

    alert_key = "feishu_quota"


class GoogleApiError(SyncError):
    alert_key = "google_api"

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GoogleWritesFailed(GoogleApiError):
    """Google rejected some writes; both calendars were read and the other writes went
    through."""

    alert_key = "google_writes"


class GoogleQuotaError(SyncError):
    """Google's calendar usage limit; retrying soon only makes it last longer."""

    alert_key = "google_quota"


class GoogleTransientError(SyncError):
    alert_key = "google_transient"
    transient = True


class SafetyAbort(SyncError):
    """The run looked wrong enough that applying it could wipe the mirror."""

    alert_key = "safety_abort"
