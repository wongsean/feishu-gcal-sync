from __future__ import annotations

from pathlib import Path

import pytest

from feishu_gcal_sync.config import Config, ConfigError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_BRAND",
        "SYNC_FUTURE_DAYS",
        "SKIP_DECLINED",
        "FGS_ENV_FILE",
        "FGS_STATE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_env_file_with_quotes_and_defaults(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nexport FEISHU_APP_ID='cli_a'\nFEISHU_APP_SECRET=\"s3\"\nSKIP_DECLINED=false\n"
    )
    cfg = Config.load(env)
    assert cfg.feishu_app_id == "cli_a"
    assert cfg.feishu_app_secret == "s3"
    assert cfg.skip_declined is False
    assert cfg.future_days == 60
    assert cfg.feishu_open_url == "https://open.feishu.cn"
    assert cfg.state_dir == Path("data")


def test_real_environment_wins(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FEISHU_APP_ID=from_file\n")
    monkeypatch.setenv("FEISHU_APP_ID", "from_env")
    monkeypatch.setenv("FEISHU_BRAND", "lark")
    cfg = Config.load(env)
    assert cfg.feishu_app_id == "from_env"
    assert cfg.feishu_accounts_url == "https://accounts.larksuite.com"


def test_missing_explicit_env_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        Config.load(tmp_path / "missing.env")


def test_invalid_values(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FEISHU_BRAND", "dingtalk")
    with pytest.raises(ConfigError, match="FEISHU_BRAND"):
        Config.load()
    monkeypatch.setenv("FEISHU_BRAND", "feishu")
    monkeypatch.setenv("SYNC_FUTURE_DAYS", "soon")
    with pytest.raises(ConfigError, match="SYNC_FUTURE_DAYS"):
        Config.load()


def test_require_names_missing_settings(cfg):
    from conftest import with_settings

    with pytest.raises(ConfigError, match="GOOGLE_CALENDAR_ID"):
        with_settings(cfg, google_calendar_id="").require("google_calendar_id")


def test_non_utf8_env_file_is_a_config_error(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_bytes("# 飞书\nFEISHU_APP_ID=x\n".encode("gbk"))
    with pytest.raises(ConfigError, match="UTF-8"):
        Config.load(env)
