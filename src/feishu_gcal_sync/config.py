"""Settings from environment variables, optionally seeded from a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# brand -> (accounts host, open platform host)
HOSTS = {
    "feishu": ("https://accounts.feishu.cn", "https://open.feishu.cn"),
    "lark": ("https://accounts.larksuite.com", "https://open.larksuite.com"),
}

# attribute name -> environment variable
_ENV_NAMES = {
    "feishu_app_id": "FEISHU_APP_ID",
    "feishu_app_secret": "FEISHU_APP_SECRET",
    "google_sa_key_file": "GOOGLE_SA_KEY_FILE",
    "google_calendar_id": "GOOGLE_CALENDAR_ID",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}


class ConfigError(Exception):
    pass


def load_env_file(path: Path, errors: str = "strict") -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors=errors)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8: {exc}") from None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Config:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_brand: str
    feishu_redirect_uri: str
    google_sa_key_file: str
    google_calendar_id: str
    state_dir: Path
    past_days: int
    future_days: int
    skip_declined: bool
    reauth_warn_days: int
    telegram_bot_token: str
    telegram_chat_id: str
    heartbeat_url: str

    @property
    def feishu_accounts_url(self) -> str:
        return HOSTS[self.feishu_brand][0]

    @property
    def feishu_open_url(self) -> str:
        return HOSTS[self.feishu_brand][1]

    def require(self, *fields: str) -> None:
        missing = [_ENV_NAMES.get(f, f) for f in fields if not getattr(self, f)]
        if missing:
            raise ConfigError("missing settings: " + ", ".join(missing))

    @classmethod
    def load(cls, env_file: Path | None = None) -> Config:
        path = env_file or Path(os.environ.get("FGS_ENV_FILE", ".env"))
        env: dict[str, str] = {}
        if path.is_file():
            env.update(load_env_file(path))
        elif env_file is not None:
            raise ConfigError(f"env file not found: {path}")
        env.update(os.environ)

        brand = env.get("FEISHU_BRAND", "feishu").strip().lower() or "feishu"
        if brand not in HOSTS:
            raise ConfigError(f"FEISHU_BRAND must be one of {sorted(HOSTS)}, got {brand!r}")
        past_days = _int(env, "SYNC_PAST_DAYS", 7)
        future_days = _int(env, "SYNC_FUTURE_DAYS", 60)
        if past_days < 0 or future_days < 1:
            raise ConfigError("SYNC_PAST_DAYS must be >= 0 and SYNC_FUTURE_DAYS >= 1")

        return cls(
            feishu_app_id=env.get("FEISHU_APP_ID", "").strip(),
            feishu_app_secret=env.get("FEISHU_APP_SECRET", "").strip(),
            feishu_brand=brand,
            feishu_redirect_uri=env.get("FEISHU_REDIRECT_URI", "").strip()
            or "http://localhost:8765/callback",
            google_sa_key_file=env.get("GOOGLE_SA_KEY_FILE", "").strip(),
            google_calendar_id=env.get("GOOGLE_CALENDAR_ID", "").strip(),
            state_dir=Path(env.get("FGS_STATE_DIR", "").strip() or "data"),
            past_days=past_days,
            future_days=future_days,
            skip_declined=_bool(env, "SKIP_DECLINED", True),
            reauth_warn_days=_int(env, "REAUTH_WARN_DAYS", 14),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
            heartbeat_url=env.get("FGS_HEARTBEAT_URL", "").strip(),
        )
