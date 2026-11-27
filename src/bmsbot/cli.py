from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from bmsbot.admin.auth import hash_password
from bmsbot.bms.client import BmsClient, BmsError, NoProxyAvailable
from bmsbot.bms.http import AsyncRateLimiter
from bmsbot.config import get_settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import AdminUser, Monitor, Region
from bmsbot.db.repositories.alerts import AlertRepository
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.proxies import ProxyRepository
from bmsbot.db.repositories.settings import SettingsRepository
from bmsbot.db.repositories.users import UserRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.db.session import close_db, init_db, session_factory, session_scope
from bmsbot.logging import configure_logging
from bmsbot.monitor.registry import sync_watch_monitors
from bmsbot.proxies.pool import ProxyPool
from bmsbot.proxies.sources import fetch_all_sources
from bmsbot.proxies.validator import ProxyValidator
from bmsbot.services.regions import sync_regions
from bmsbot.services.runtime import RuntimeFlags

log = logging.getLogger(__name__)


async def _client() -> BmsClient:
    settings = get_settings()
    pool = ProxyPool(session_factory, settings)
    flags = RuntimeFlags(session_factory)
    return BmsClient(
        settings,
        pool,
        limiter=AsyncRateLimiter(settings.max_requests_per_sec),
        proxy_enabled=flags.use_proxy,
    )


async def cmd_init_db(_args: argparse.Namespace) -> int:
    await init_db()
    print("Database tables created (development bootstrap).")
    return 0


async def cmd_create_admin(args: argparse.Namespace) -> int:
    await init_db()
    username = args.username or input("Admin username: ").strip()
    password = getpass.getpass("Password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 2
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 2
    async with session_scope() as session:
        result = await session.execute(select(AdminUser))
        existing = result.scalars().first()
        if existing is not None:
            print(
                f"An admin already exists ({existing.username}). "
                "Delete it from the DB if you need to start over.",
                file=sys.stderr,
            )
            return 1
        session.add(AdminUser(username=username, password_hash=hash_password(password)))
    print(f"Created super admin {username!r}.")
    return 0


async def cmd_seed_regions(_args: argparse.Namespace) -> int:
    client = await _client()
    try:
        async with session_scope() as session:
            count = await sync_regions(session, client)
        print(f"Synced {count} regions.")
        return 0
    except NoProxyAvailable:
        print("No active proxy. Start the proxy worker first.", file=sys.stderr)
        return 2
    finally:
        await close_db()


async def cmd_proxy_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    validator = ProxyValidator(settings)
    if args.url:
        result = await validator.validate(args.url, iterations=args.iterations)
        print(
            f"{args.url}: {result.successes}/{result.iterations} ok, "
            f"latencies={result.latencies}, exit_ip={result.exit_ip}, errors={result.errors[:3]}"
        )
        return 0 if result.successes >= settings.proxy_validation_required else 1

    candidates = await fetch_all_sources(settings)
    if not candidates:
        print("No candidates fetched.", file=sys.stderr)
        return 1
    print(f"Validating first {args.limit} of {len(candidates)} candidates...")
    working = 0
    for candidate in candidates[: args.limit]:
        result = await validator.validate(candidate.url, iterations=args.iterations)
        status = "OK" if result.successes >= settings.proxy_validation_required else "no"
        print(
            f"[{status}] {candidate.url} {result.successes}/{result.iterations} "
            f"p95={max(result.latencies) if result.latencies else 0}ms"
        )
        working += int(status == "OK")
    print(f"Working: {working}/{min(args.limit, len(candidates))}")
    return 0 if working else 1


async def cmd_scrape(args: argparse.Namespace) -> int:
    client = await _client()
    try:
        region = await client.region(args.city)
        event_code = args.movie.upper()
        if not event_code.startswith("ET"):
            results = await client.search_movies(args.movie, region)
            if not results:
                print("No movie matched.", file=sys.stderr)
                return 1
            event_code = results[0].event_code
            print(f"Movie: {results[0].title} ({event_code})")
        date_code = args.date or datetime.now().strftime("%Y%m%d")
        snapshot = await client.showtimes(event_code, date_code, region)
        print(f"{snapshot.title or event_code} · {region.name} · {snapshot.actual_date}")
        for theatre in snapshot.theatres:
            print(f"\n{theatre.venue_name} [{theatre.venue_code}]")
            for show in theatre.shows:
                print(
                    f"  {show.time:<10} {(show.format or '-')[:34]:<34} "
                    f"{show.availability_text:<12} {show.session_id}"
                )
        return 0
    except NoProxyAvailable:
        print("No active proxy. Refusing to contact BookMyShow directly.", file=sys.stderr)
        return 2
    except BmsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await close_db()


async def cmd_reconcile(_args: argparse.Namespace) -> int:
    """Repair active watches that have no shared monitors."""
    client = await _client()
    settings = get_settings()
    created = 0
    try:
        async with session_factory() as session:
            watches = await WatchRepository(session).list_all_active()
            for watch in watches:
                region = await session.get(Region, watch.region_code)
                if region is None:
                    print(f"watch {watch.id}: region {watch.region_code} missing")
                    continue
                existing = await session.execute(
                    select(Monitor.id).where(
                        Monitor.region_code == watch.region_code,
                        Monitor.event_code == watch.event_code,
                        Monitor.is_active.is_(True),
                    )
                )
                if existing.first() is not None:
                    continue
                try:
                    monitors = await sync_watch_monitors(
                        session, client, watch, region, settings
                    )
                    await session.commit()
                    created += len(monitors)
                    print(f"watch {watch.id}: created {len(monitors)} monitors")
                except (NoProxyAvailable, BmsError) as exc:
                    await session.rollback()
                    print(f"watch {watch.id}: skipped ({exc})")
                except Exception as exc:
                    await session.rollback()
                    print(f"watch {watch.id}: error {type(exc).__name__}: {exc}")
        print(f"created {created} monitors")
        return 0
    except NoProxyAvailable:
        print("No active proxy. Enable a proxy or set use_proxy=false.", file=sys.stderr)
        return 2
    finally:
        await close_db()


async def cmd_status(_args: argparse.Namespace) -> int:
    settings = get_settings()
    async with session_factory() as session:
        users = await UserRepository(session).count()
        watches = await WatchRepository(session).count_active()
        monitors = await MonitorRepository(session).count_active()
        due = await MonitorRepository(session).count_due()
        pending = await AlertRepository(session).count_pending()
        proxies = await ProxyRepository(session).counts()
        premium = await ProxyRepository(session).has_enabled_premium()
        use_proxy = bool(await SettingsRepository(session).get("use_proxy", True))
    print(f"db={settings.database_url}")
    print(
        f"users={users} watches={watches} monitors={monitors} due={due} "
        f"alerts_pending={pending}"
    )
    print(f"use_proxy={use_proxy} premium_only={premium} proxies={proxies}")
    return 0


async def cmd_doctor(_args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from sqlalchemy import text

    from bmsbot.config import PROJECT_ROOT

    settings = get_settings()
    print(f"project_root : {PROJECT_ROOT}")
    print(f"cwd          : {Path.cwd()}")
    print(f"data_dir     : {settings.data_dir}")
    print(f"database_url : {settings.database_url}")
    print(f"env          : {settings.env}")
    print(f"direct_mode  : allowed={settings.direct_allowed} allow_direct={settings.allow_direct}")

    db_path: str | None = None
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        print(f"db_file      : {db_path}")
        if not os.path.exists(db_path):
            print("  MISSING")
            return 1
        print(f"  size       : {os.path.getsize(db_path)} bytes")

    try:
        async with session_factory() as session:
            revision = (await session.execute(text("select version_num from alembic_version"))).scalar_one_or_none()
            tables = (await session.execute(text("select count(*) from sqlite_master where type='table'"))).scalar_one()
            admins = (await session.execute(text("select username from admin_users order by id"))).scalars().all()
            users = await UserRepository(session).count()
            watches = await WatchRepository(session).count_active()
            monitors = await MonitorRepository(session).count_active()
            pending = await AlertRepository(session).count_pending()
            failed = await AlertRepository(session).count_failed_since(
                utcnow() - timedelta(days=1)
            )
            last_sent = await AlertRepository(session).last_sent_at()
            proxies = await ProxyRepository(session).counts()
            premium = await ProxyRepository(session).has_enabled_premium()
            use_proxy = bool(await SettingsRepository(session).get("use_proxy", True))
            heartbeats = (await session.execute(text("select name, status, last_seen_at, detail from worker_heartbeats"))).all()
    except Exception as exc:
        print(f"database check FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"telegram     : {'configured' if settings.telegram_bot_token else 'MISSING TOKEN'}")
    print(f"alembic      : {revision}")
    print(f"tables       : {tables}")
    print(f"admins       : {list(admins)}")
    print(f"users        : {users}")
    print(f"watches      : {watches}")
    print(f"monitors     : {monitors}")
    print(f"alerts_pending: {pending}")
    print(f"alerts_failed_24h: {failed}")
    print(f"alerts_last_sent : {last_sent}")
    print(f"use_proxy    : {use_proxy}")
    print(f"premium_only : {premium}")
    print(f"proxies      : {proxies}")
    print("heartbeats   :")
    for row in heartbeats:
        detail = row[3] or {}
        if isinstance(detail, str):
            import json

            try:
                detail = json.loads(detail)
            except ValueError:
                detail = {}
        if not isinstance(detail, dict):
            detail = {}
        extra = ""
        if row[0] == "alert-sender":
            extra = (
                f" sent={detail.get('sent')} failed={detail.get('failed')}"
                f" pending={detail.get('pending')}"
                f" configured={detail.get('configured')}"
            )
            if detail.get("last_error"):
                extra += f" last_error={detail['last_error']}"
        print(f"  {row[0]:<16} {row[1]:<8} {row[2]}{extra}")
    if not settings.telegram_bot_token:
        print("WARNING: telegram token missing; alerts cannot be delivered")
        return 1
    if failed:
        print(f"WARNING: {failed} alert(s) failed to send in the last 24h")
    return 0


async def cmd_reset_admin_password(args: argparse.Namespace) -> int:
    await init_db()
    username = args.username or input("Admin username [first admin]: ").strip() or None
    password = getpass.getpass("New password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 2
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 2
    async with session_scope() as session:
        if username:
            result = await session.execute(select(AdminUser).where(AdminUser.username == username))
            admin = result.scalar_one_or_none()
        else:
            result = await session.execute(select(AdminUser).order_by(AdminUser.id))
            admin = result.scalars().first()
        if admin is None:
            admin = AdminUser(
                username=username or "admin",
                password_hash=hash_password(password),
            )
            session.add(admin)
            print(f"Created admin {admin.username!r}.")
        else:
            admin.password_hash = hash_password(password)
            admin.is_active = True
            print(f"Updated password for {admin.username!r}.")
    return 0


def _runner(module: str, function: str = "main") -> int:
    import importlib

    mod = importlib.import_module(module)
    asyncio.run(getattr(mod, function)())
    return 0


def cmd_run_bot(_args: argparse.Namespace) -> int:
    from bmsbot.bot.main import run_bot

    asyncio.run(run_bot())
    return 0


def cmd_run_monitor(_args: argparse.Namespace) -> int:
    from bmsbot.workers.monitor_worker import main

    asyncio.run(main())
    return 0


def cmd_run_proxy(_args: argparse.Namespace) -> int:
    from bmsbot.workers.proxy_worker import main

    asyncio.run(main())
    return 0


def cmd_run_alerts(_args: argparse.Namespace) -> int:
    from bmsbot.workers.alert_worker import main

    asyncio.run(main())
    return 0


def cmd_run_admin(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "bmsbot.admin.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bmsbot", description="BookMyShow watch bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create tables for development").set_defaults(func=cmd_init_db)

    admin = sub.add_parser("create-admin", help="Create the single super admin")
    admin.add_argument("--username", default=None)
    admin.set_defaults(func=cmd_create_admin)

    reset = sub.add_parser("reset-admin-password", help="Reset the admin password")
    reset.add_argument("--username", default=None)
    reset.set_defaults(func=cmd_reset_admin_password)

    sub.add_parser("doctor", help="Print resolved DB path, migrations and health").set_defaults(
        func=cmd_doctor
    )

    sub.add_parser("seed-regions", help="Download BMS regions").set_defaults(func=cmd_seed_regions)

    proxy = sub.add_parser("proxy-check", help="Validate proxies against BMS")
    proxy.add_argument("--url", default=None, help="Validate one proxy URL")
    proxy.add_argument("--limit", type=int, default=10, help="Candidates to test from open lists")
    proxy.add_argument("--iterations", type=int, default=None)
    proxy.set_defaults(func=cmd_proxy_check)

    scrap = sub.add_parser("scrape", help="Print showtimes (uses the proxy pool)")
    scrap.add_argument("--city", default="hyderabad")
    scrap.add_argument("--movie", required=True, help="Movie name or ET event code")
    scrap.add_argument("--date", default=None, help="YYYYMMDD (default today)")
    scrap.set_defaults(func=cmd_scrape)

    sub.add_parser("reconcile", help="Create missing monitors for active watches").set_defaults(
        func=cmd_reconcile
    )
    sub.add_parser("status", help="Print queue and pool status").set_defaults(func=cmd_status)
    sub.add_parser("run-bot", help="Run the Telegram bot").set_defaults(func=cmd_run_bot)
    sub.add_parser("run-monitor", help="Run the monitor worker").set_defaults(func=cmd_run_monitor)
    sub.add_parser("run-proxy", help="Run the proxy hunter").set_defaults(func=cmd_run_proxy)
    sub.add_parser("run-alerts", help="Run the alert sender").set_defaults(func=cmd_run_alerts)

    admin_run = sub.add_parser("run-admin", help="Run the admin dashboard")
    admin_run.add_argument("--host", default="127.0.0.1")
    admin_run.add_argument("--port", type=int, default=8080)
    admin_run.set_defaults(func=cmd_run_admin)
    return parser


def main() -> None:
    import inspect

    configure_logging(logging.INFO)
    args = build_parser().parse_args()
    result = args.func(args)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    raise SystemExit(result if isinstance(result, int) else 0)


if __name__ == "__main__":
    main()
