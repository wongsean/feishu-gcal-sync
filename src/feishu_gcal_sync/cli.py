"""`fgs` command line: auth, sync, status, test-alert."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import Config, ConfigError, load_env_file
from .errors import GoogleQuotaError, GoogleWritesFailed, SyncError
from .feishu_auth import FeishuAuth, TokenStore, parse_callback, wait_for_callback
from .feishu_calendar import FeishuCalendar
from .fsutil import LockBusy, RunLock
from .google_calendar import GoogleCalendar
from .notify import Alerts, LogNotifier, Notifier, TelegramNotifier
from .reconcile import Plan
from .sync import run_sync

log = logging.getLogger("feishu_gcal_sync")

TOKEN_FILE = "feishu_token.json"
CALENDAR_ID_FILE = "feishu_calendar_id"
ALERTS_FILE = "alerts.json"
LOCK_FILE = "run.lock"
# A sync renews the Feishu access token up front if it has less than this left, so it still
# has REFRESH_MARGIN to spare when Feishu is read, after a Google listing that may spend
# minutes in retries.
RUN_TOKEN_MARGIN = 20 * 60
# Transient failures (network, 5xx, rate limits) only alert after lasting this long.
TRANSIENT_GRACE = 3600
# After Google's usage limit trips, keep reading but stop writing for this long.
QUOTA_COOLDOWN = 3 * 3600
_GOOGLE_SETTINGS = ("google_sa_key_file", "google_calendar_id")
_FEISHU_SETTINGS = ("feishu_app_id", "feishu_app_secret")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Keep third-party loggers at INFO even with -v: urllib3's debug lines print request URLs,
    # and the Telegram bot token is part of one.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        log.setLevel(logging.DEBUG)
    try:
        cfg = Config.load(args.env_file)
    except (ConfigError, OSError) as exc:
        return _config_failure(args, exc)
    try:
        return args.func(cfg, args)
    except ConfigError as exc:
        return _config_failure(args, exc)
    except BrokenPipeError:
        # Output piped into `head` or a pager that quit early; not a failure worth a traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _config_failure(args: argparse.Namespace, exc: Exception) -> int:
    print(f"配置错误：{exc}", file=sys.stderr)
    if args.command == "sync":
        _alert_config_failure(args, exc)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fgs", description="飞书日历 → Google 日历单向同步")
    parser.add_argument("--env-file", type=Path, help="配置文件，默认读取 $FGS_ENV_FILE 或 ./.env")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="飞书授权（首次使用、授权失效或满一年时运行）")
    p.add_argument(
        "--manual", action="store_true", help="不启动本地回调，改为手动粘贴浏览器跳转后的地址"
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("sync", help="执行一轮对账")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写 Google")
    p.add_argument(
        "--allow-empty", action="store_true", help="飞书返回空日程时也照常删除 Google 上的镜像"
    )
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="查看授权状态并检查 Google 日历权限")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("test-alert", help="发送一条测试告警（并检查心跳地址）")
    p.set_defaults(func=cmd_test_alert)
    return parser


def make_notifier(cfg: Config, session: requests.Session) -> Notifier:
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        return TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, session)
    return LogNotifier()


def cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    cfg.require(*_FEISHU_SETTINGS)
    _check_state_owner(cfg)
    session = requests.Session()
    auth = FeishuAuth(cfg, session, TokenStore(cfg.state_dir / TOKEN_FILE))
    state = secrets.token_urlsafe(16)
    print("1. 在浏览器中打开下面的链接，登录飞书并同意授权：\n")
    print(auth.authorize_url(state) + "\n")
    try:
        if args.manual:
            print(
                "2. 同意后浏览器会跳到一个打不开的 localhost 页面，"
                "把地址栏里的完整 URL 粘贴到这里（5 分钟内）："
            )
            code = parse_callback(input("> ").strip(), state)
        else:
            port = urlsplit(cfg.feishu_redirect_uri).port or 80
            print(f"2. 正在 {cfg.feishu_redirect_uri} 等待飞书回调（5 分钟内有效）……")
            print(
                f"   如果本命令跑在服务器上，先在本机执行 "
                f"ssh -L {port}:127.0.0.1:{port} <服务器>，再打开上面的链接。"
            )
            code = wait_for_callback(cfg.feishu_redirect_uri, state)
    except SyncError as exc:
        print(f"授权失败：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"无法监听 {cfg.feishu_redirect_uri}：{exc}（可改用 --manual）", file=sys.stderr)
        return 1

    # Only the exchange holds the lock: it waits for a running sync, which could otherwise
    # store a refresh of the old grant over the new token.
    calendar_id_file = cfg.state_dir / CALENDAR_ID_FILE
    try:
        with RunLock(cfg.state_dir / LOCK_FILE, blocking=True):
            # The remembered calendar belongs to the previous grant, maybe another user's.
            calendar_id_file.unlink(missing_ok=True)
            token = auth.exchange_code(code)
    except SyncError as exc:
        print(f"授权失败：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"保存授权失败：{exc}", file=sys.stderr)
        return 1
    print("\n授权成功，已保存。")
    try:
        with RunLock(cfg.state_dir / LOCK_FILE, blocking=True):
            feishu = FeishuCalendar(cfg.feishu_open_url, session, auth, calendar_id_file)
            calendar_id = feishu.primary_calendar_id()
        print(f"  主日历：{calendar_id}")
    except SyncError as exc:
        print(f"  读取主日历失败（授权本身已保存，可稍后用 `fgs sync --dry-run` 检查）：{exc}")
    print(f"  refresh_token 到期：{_fmt(token.refresh_expires_at)}（定时任务运行期间会自动续期）")
    print(f"  365 天授权上限：{_fmt(token.grant_expires_at)}，到期前需再次运行 `fgs auth`")
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    cfg.require(*_FEISHU_SETTINGS, *_GOOGLE_SETTINGS)
    _check_state_owner(cfg)
    session = requests.Session()
    alerts = Alerts(cfg.state_dir / ALERTS_FILE, make_notifier(cfg, session))
    lock = RunLock(cfg.state_dir / LOCK_FILE)
    try:
        lock.__enter__()
    except LockBusy:
        log.info("another fgs run holds the lock, skipping this one")
        return 0
    except OSError as exc:
        log.error("cannot take the run lock: %s", exc)
        if not args.dry_run:
            alerts.fire("crash", f"⚠️ feishu-gcal-sync 无法获取运行锁：{exc}")
        return 1
    try:
        return _locked_sync(cfg, args, session, alerts)
    finally:
        lock.__exit__()


def _locked_sync(
    cfg: Config, args: argparse.Namespace, session: requests.Session, alerts: Alerts
) -> int:
    auth = FeishuAuth(cfg, session, TokenStore(cfg.state_dir / TOKEN_FILE))
    hold_writes = _quota_cooldown(alerts)
    try:
        # Renew the Feishu token before anything Google-side can fail, so a broken Google
        # setup can't let the ~7-day refresh_token lapse. Renew it for the whole run, too,
        # then rule out rotating it later: the first Google request starts a thread.
        auth.access_token(margin=RUN_TOKEN_MARGIN)
        auth.hold_refresh()
        feishu = FeishuCalendar(
            cfg.feishu_open_url, session, auth, cfg.state_dir / CALENDAR_ID_FILE
        )
        google = GoogleCalendar.from_key_file(cfg.google_sa_key_file, cfg.google_calendar_id)
        plan = run_sync(
            cfg, feishu, google, dry_run=args.dry_run or hold_writes, allow_empty=args.allow_empty
        )
    except Exception as exc:
        if isinstance(exc, SyncError):
            log.error("sync failed: %s", exc)
            key, text = exc.alert_key, f"⚠️ feishu-gcal-sync：{exc}"
            grace = TRANSIENT_GRACE if exc.transient else 0
        else:
            log.exception("sync crashed")
            key, text = "crash", f"⚠️ feishu-gcal-sync 异常退出：{type(exc).__name__}: {exc}"
            grace = 0
        if not args.dry_run:
            alerts.fire(key, text, grace=grace)
            _check_grant_deadline(cfg, auth, alerts)
            if isinstance(exc, GoogleWritesFailed):
                # Both calendars were read and the other writes went through, so the job is
                # alive and every other failure is over; the alert above covers the rejected
                # events. Without the ping, one event Google keeps refusing would keep the
                # heartbeat check down for good.
                alerts.resolve_failures(
                    "✅ feishu-gcal-sync 其他问题已恢复（仍有个别事件写入失败）",
                    keep=frozenset({exc.alert_key}),
                )
                _heartbeat(cfg, session)
        return 1

    if args.dry_run:
        _print_plan(plan)
        return 0
    _check_grant_deadline(cfg, auth, alerts)
    if hold_writes:
        # Reading and token renewal worked; only the quota alert stays open.
        log.warning("Google usage limit hit recently, not writing this run (%s)", plan.summary())
        quota = frozenset({GoogleQuotaError.alert_key})
        alerts.resolve_failures("✅ feishu-gcal-sync 已恢复正常", keep=quota)
    else:
        alerts.resolve_failures("✅ feishu-gcal-sync 已恢复正常")
    _heartbeat(cfg, session)
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    healthy = True
    token = TokenStore(cfg.state_dir / TOKEN_FILE).load()
    if token is None:
        print("飞书授权：未授权，请运行 `fgs auth`")
        healthy = False
    else:
        days_left = (token.grant_expires_at - time.time()) / 86400
        print(f"飞书授权：scope={token.scope}")
        print(f"  access_token 到期：{_fmt(token.access_expires_at)}")
        print(f"  refresh_token 到期：{_fmt(token.refresh_expires_at)}")
        print(f"  365 天授权上限：{_fmt(token.grant_expires_at)}（剩 {days_left:.0f} 天）")
        if token.refresh_expires_at < time.time():
            print("  refresh_token 已过期，请运行 `fgs auth`")
            healthy = False

    if all(getattr(cfg, f) for f in _GOOGLE_SETTINGS):
        try:
            google = GoogleCalendar.from_key_file(cfg.google_sa_key_file, cfg.google_calendar_id)
            print(f"Google 日历：Service Account 权限为 {google.probe()}")
        except SyncError as exc:
            print(f"Google 日历：{exc}")
            healthy = False
    else:
        print("Google 日历：未配置 GOOGLE_SA_KEY_FILE / GOOGLE_CALENDAR_ID")
        healthy = False

    for key, entry in Alerts(cfg.state_dir / ALERTS_FILE, LogNotifier()).active().items():
        print(f"未恢复的告警：{key}（自 {_fmt(entry['since'])}）")
    return 0 if healthy else 1


def cmd_test_alert(cfg: Config, args: argparse.Namespace) -> int:
    session = requests.Session()
    notifier = make_notifier(cfg, session)
    ok = True
    if isinstance(notifier, LogNotifier):
        print("Telegram：未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        ok = False
    elif notifier.send("🔔 feishu-gcal-sync 测试告警"):
        print("Telegram：已发送")
    else:
        print("Telegram：发送失败，详见日志", file=sys.stderr)
        ok = False
    if cfg.heartbeat_url:
        if _heartbeat(cfg, session):
            print("心跳地址：正常")
        else:
            print("心跳地址：请求失败，详见日志", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def _check_state_owner(cfg: Config) -> None:
    """Refuse to write state as anyone but its owner: files left behind by a root run would
    lock the timer's user out of its own token and alert records."""
    try:
        owner = cfg.state_dir.stat().st_uid
    except FileNotFoundError:
        return
    if owner != os.geteuid():
        raise ConfigError(
            f"状态目录 {cfg.state_dir} 属于 uid {owner}，请用该用户运行，例如 "
            "sudo -u fgs /opt/feishu-gcal-sync/.venv/bin/fgs --env-file /etc/feishu-gcal-sync/env …"
        )


def _quota_cooldown(alerts: Alerts) -> bool:
    entry = alerts.active().get(GoogleQuotaError.alert_key)
    return bool(entry) and time.time() - entry.get("last", entry["since"]) < QUOTA_COOLDOWN


def _check_grant_deadline(cfg: Config, auth: FeishuAuth, alerts: Alerts) -> None:
    try:
        token = auth.store.load()
    except (OSError, ValueError, TypeError) as exc:
        log.error("cannot read the stored token: %s", exc)
        return
    if token is None:
        return
    days_left = (token.grant_expires_at - time.time()) / 86400
    if days_left <= cfg.reauth_warn_days:
        alerts.fire(
            "reauth_soon",
            f"⏰ 飞书授权将在 {max(days_left, 0):.0f} 天后到期（365 天上限），"
            "请在服务器上运行 `fgs auth` 重新授权",
            repeat=86400,
        )
    else:
        alerts.resolve("reauth_soon")


def _alert_config_failure(args: argparse.Namespace, exc: Exception) -> None:
    """Best effort when the config itself is broken: without it a bad edit to the env file
    would silently stop token renewal until the refresh_token expires."""
    try:
        env: dict[str, str] = {}
        path = args.env_file or Path(os.environ.get("FGS_ENV_FILE", ".env"))
        with contextlib.suppress(OSError, ConfigError):
            env.update(load_env_file(path, errors="replace"))
        env.update(os.environ)
        state_dir = Path(env.get("FGS_STATE_DIR", "").strip() or "data")
        # Never create state as a user other than its owner.
        if not state_dir.is_dir() or state_dir.stat().st_uid != os.geteuid():
            return
        bot, chat = (
            env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            env.get("TELEGRAM_CHAT_ID", "").strip(),
        )
        notifier = (
            TelegramNotifier(bot, chat, requests.Session()) if bot and chat else LogNotifier()
        )
        Alerts(state_dir / ALERTS_FILE, notifier).fire(
            "config", f"⚠️ feishu-gcal-sync 配置错误：{exc}"
        )
    except Exception:
        log.exception("could not send the config-error alert")


def _heartbeat(cfg: Config, session: requests.Session) -> bool:
    """Ping a dead-man's switch so silence (timer stopped, VPS down) also gets noticed."""
    if not cfg.heartbeat_url:
        return True
    try:
        resp = session.get(cfg.heartbeat_url, timeout=10)
    except requests.RequestException as exc:
        log.warning("heartbeat ping failed: %s", type(exc).__name__)
        return False
    if not 200 <= resp.status_code < 300:
        log.warning("heartbeat ping returned HTTP %s", resp.status_code)
        return False
    return True


def _print_plan(plan: Plan) -> None:
    print(plan.summary())
    for body in plan.insert:
        print(f"  + {_when(body)} {body['summary']}")
    for body in plan.update:
        print(f"  ~ {_when(body)} {body['summary']}")
    for event_id in plan.delete:
        print(f"  - {event_id}")


def _when(body: dict) -> str:
    start = body["start"]
    return start.get("date") or start.get("dateTime", "")


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


if __name__ == "__main__":
    sys.exit(main())
