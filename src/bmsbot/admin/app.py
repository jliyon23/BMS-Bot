from __future__ import annotations

import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from bmsbot.admin.auth import (
    create_admin_session,
    delete_admin_session,
    load_admin_session,
    valid_csrf,
    verify_password,
)
from bmsbot.config import Settings, get_settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import (
    AdminUser,
    AlertEvent,
    AuditLog,
    Monitor,
    Movie,
    Region,
    WorkerHeartbeat,
)
from bmsbot.db.repositories.alerts import AlertRepository
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.proxies import ProxyRepository
from bmsbot.db.repositories.settings import SettingsRepository
from bmsbot.db.repositories.users import UserRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.db.session import session_factory
from bmsbot.domain.enums import ProxyTier
from bmsbot.proxies.crypto import CredentialCipher, parse_proxy_url

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _login_allowed(ip: str) -> bool:
    now = time.monotonic()
    bucket = _login_attempts[ip]
    _login_attempts[ip] = [stamp for stamp in bucket if now - stamp < 300]
    return len(_login_attempts[ip]) < 5


async def _db() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


def _ctx(request: Request, **kwargs) -> dict:
    return {
        "request": request,
        "csrf": request.session.get("csrf", ""),
        "msg": request.query_params.get("msg"),
        **kwargs,
    }


async def _current_admin(request: Request, session: AsyncSession):
    token = request.session.get("sid")
    if not token:
        return None
    loaded = await load_admin_session(session, token)
    if loaded is None:
        request.session.clear()
        return None
    return loaded


async def _audit(
    session: AsyncSession,
    request: Request,
    admin: AdminUser,
    action: str,
    entity: str,
    entity_id: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor=admin.username,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail or {},
            ip=request.client.host if request.client else None,
        )
    )
    await session.flush()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.env == "prod" and (
        len(settings.admin_secret_key) < 32
        or settings.admin_secret_key in {"change-this-in-production", "change-this-to-a-long-random-value"}
    ):
        raise RuntimeError("Set a strong BMS_ADMIN_SECRET_KEY before running in production")
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Fail fast with an actionable message instead of a 500 on login.
        async with session_factory() as session:
            try:
                await session.execute(text("select 1 from admin_users limit 1"))
            except Exception as exc:
                raise RuntimeError(
                    "Database is not migrated. Run `alembic upgrade head` from the "
                    f"project root. Resolved DB: {settings.database_url}"
                ) from exc
        yield

    app = FastAPI(
        title=f"{settings.app_name} Admin",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.admin_secret_key,
        max_age=settings.admin_session_ttl_sec,
        same_site="lax",
        https_only=settings.env == "prod",
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; base-uri 'self'"
        )
        return response

    # ------------------------------------------------------------------ auth

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, session: AsyncSession = Depends(_db)):
        if await _current_admin(request, session):
            return RedirectResponse("/", status_code=303)
        if not request.session.get("csrf"):
            request.session["csrf"] = secrets.token_urlsafe(32)
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error=None)
        )

    @app.post("/login")
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf: str = Form(...),
        session: AsyncSession = Depends(_db),
    ):
        ip = request.client.host if request.client else "unknown"
        if not _login_allowed(ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                _ctx(request, error="Too many attempts. Try again in a few minutes."),
                status_code=429,
            )
        if not valid_csrf(request.session.get("csrf"), csrf):
            return templates.TemplateResponse(
                request, "login.html", _ctx(request, error="Invalid CSRF token"), status_code=400
            )
        result = await session.execute(select(AdminUser).where(AdminUser.username == username))
        admin = result.scalar_one_or_none()
        if admin is None or not admin.is_active or not verify_password(admin.password_hash, password):
            _login_attempts[ip].append(time.monotonic())
            await _audit(session, request, AdminUser(username=username), "login_failed", "admin")
            await session.commit()
            return templates.TemplateResponse(
                request, "login.html", _ctx(request, error="Invalid username or password"), status_code=401
            )

        token, csrf_token = await create_admin_session(
            session,
            admin,
            ttl_sec=settings.admin_session_ttl_sec,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
        await _audit(session, request, admin, "login", "admin", str(admin.id))
        await session.commit()
        request.session["sid"] = token
        request.session["csrf"] = csrf_token
        _login_attempts.pop(ip, None)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, session: AsyncSession = Depends(_db)):
        form = await request.form()
        token = request.session.get("sid")
        if token and valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            await delete_admin_session(session, token)
            await session.commit()
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    async def _require(request: Request, session: AsyncSession):
        loaded = await _current_admin(request, session)
        if loaded is None:
            return None
        return loaded

    # ----------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        users = await UserRepository(session).count()
        watches = await WatchRepository(session).count_active()
        monitors = await MonitorRepository(session).count_active()
        due = await MonitorRepository(session).count_due()
        alerts = AlertRepository(session)
        pending = await alerts.count_pending()
        sent = await alerts.count_sent_since(utcnow() - timedelta(hours=24))
        failed_24h = await alerts.count_failed_since(utcnow() - timedelta(hours=24))
        last_sent = await alerts.last_sent_at()
        settings_obj = get_settings()
        proxy_counts = await ProxyRepository(session).counts()
        premium = await ProxyRepository(session).has_enabled_premium()
        heartbeats = list(
            (await session.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.name))).scalars()
        )
        recent_alerts = await alerts.list_recent(limit=8)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(
                request,
                admin=admin,
                users=users,
                watches=watches,
                monitors=monitors,
                due=due,
                pending=pending,
                sent=sent,
                failed_24h=failed_24h,
                last_sent=last_sent,
                telegram_configured=bool(settings_obj.telegram_bot_token),
                proxy_counts=proxy_counts,
                premium=premium,
                heartbeats=heartbeats,
                recent_alerts=recent_alerts,
                now=utcnow(),
            ),
        )

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(
        request: Request, q: str | None = None, page: int = 1, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        page = max(1, page)
        limit = 50
        repo = UserRepository(session)
        users = await repo.list_page(limit=limit, offset=(page - 1) * limit, query=q)
        total = await repo.count()
        return templates.TemplateResponse(
            request,
            "users.html",
            _ctx(
                request,
                admin=admin,
                users=users,
                q=q or "",
                page=page,
                total=total,
                pages=max(1, (total + limit - 1) // limit),
            ),
        )

    @app.post("/users/{user_id}/ban")
    async def ban_user(
        user_id: int, request: Request, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        user = await UserRepository(session).get(user_id)
        if user is not None:
            await UserRepository(session).set_banned(user, not user.is_banned)
            await _audit(session, request, admin, "toggle_ban", "user", str(user.id))
            await session.commit()
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/{user_id}/delete")
    async def delete_user(
        user_id: int, request: Request, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        user = await UserRepository(session).get(user_id)
        if user is not None:
            await UserRepository(session).delete(user)
            await MonitorRepository(session).deactivate_orphans()
            await _audit(session, request, admin, "delete", "user", str(user_id))
            await session.commit()
        return RedirectResponse("/users", status_code=303)

    @app.get("/proxies", response_class=HTMLResponse)
    async def proxies_page(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        repo = ProxyRepository(session)
        proxies = await repo.list_all(limit=300)
        counts = await repo.counts()
        premium = await repo.has_enabled_premium()
        use_proxy = bool(await SettingsRepository(session).get("use_proxy", True))
        return templates.TemplateResponse(
            request,
            "proxies.html",
            _ctx(
                request,
                admin=admin,
                proxies=proxies,
                counts=counts,
                premium=premium,
                use_proxy=use_proxy,
                direct_allowed=settings.direct_allowed,
                cipher=CredentialCipher(settings.proxy_encryption_key).enabled,
            ),
        )

    @app.post("/proxies/add")
    async def add_proxy(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        raw_url = str(form.get("url", "")).strip()
        tier = str(form.get("tier", "premium"))
        if tier not in (ProxyTier.PREMIUM.value, ProxyTier.FREE.value):
            tier = ProxyTier.PREMIUM.value
        label = str(form.get("label", "admin")).strip() or "admin"
        is_rotating = str(form.get("is_rotating", "")).lower() in {"on", "1", "true", "yes"}
        try:
            parsed = parse_proxy_url(raw_url)
        except ValueError as exc:
            return RedirectResponse(f"/proxies?msg={exc}", status_code=303)
        import ipaddress

        try:
            address = ipaddress.ip_address(parsed.host)
            if address.is_private or address.is_loopback or address.is_reserved:
                return RedirectResponse(
                    "/proxies?msg=Private+and+loopback+proxy+addresses+are+not+allowed",
                    status_code=303,
                )
        except ValueError:
            pass

        credentials = parsed.credentials
        encrypted = None
        if credentials:
            cipher = CredentialCipher(settings.proxy_encryption_key)
            if not cipher.enabled:
                return RedirectResponse(
                    "/proxies?msg=Set+BMS_PROXY_ENCRYPTION_KEY+before+adding+authenticated+proxies",
                    status_code=303,
                )
            encrypted = cipher.encrypt(credentials)

        repo = ProxyRepository(session)
        existing = await repo.by_url(parsed.base_url)
        if existing is None:
            proxy = await repo.upsert_candidate(
                url=parsed.base_url,
                protocol=parsed.scheme,
                host=parsed.host,
                port=parsed.port,
                country=None,
                anonymity="premium" if tier == ProxyTier.PREMIUM.value else "manual",
                source=label,
                tier=tier,
                is_rotating=is_rotating,
            )
        else:
            proxy = existing
            proxy.tier = tier
            proxy.is_rotating = is_rotating
            proxy.is_enabled = True
        proxy.credentials_encrypted = encrypted
        proxy.status = "candidate"
        proxy.is_enabled = True
        proxy.updated_at = utcnow()
        await _audit(session, request, admin, "proxy_add", "proxy", str(proxy.id), {"tier": tier})
        await session.commit()
        return RedirectResponse("/proxies?msg=Proxy+added+and+queued+for+validation", status_code=303)

    @app.post("/proxies/toggle-proxy")
    async def toggle_use_proxy(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = SettingsRepository(session)
        current = bool(await repo.get("use_proxy", True))
        new_value = not current
        if not new_value and not settings.direct_allowed:
            return RedirectResponse(
                "/proxies?msg=Direct+mode+is+disabled+in+production", status_code=303
            )
        await repo.set("use_proxy", new_value)
        await _audit(
            session, request, admin, "use_proxy", "setting", "use_proxy", {"value": new_value}
        )
        await session.commit()
        state = "enabled" if new_value else "disabled"
        return RedirectResponse(f"/proxies?msg=Proxy+{state}", status_code=303)

    @app.post("/proxies/{proxy_id}/recheck")
    async def recheck_proxy(proxy_id: int, request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = ProxyRepository(session)
        proxy = await repo.get(proxy_id)
        if proxy is not None:
            proxy.status = "candidate"
            proxy.is_enabled = True
            proxy.cooldown_until = None
            proxy.last_checked_at = None
            proxy.last_error = None
            await _audit(session, request, admin, "proxy_recheck", "proxy", str(proxy_id))
            await session.commit()
        return RedirectResponse("/proxies?msg=Proxy+queued+for+recheck", status_code=303)

    @app.post("/proxies/{proxy_id}/toggle")
    async def toggle_proxy(proxy_id: int, request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = ProxyRepository(session)
        proxy = await repo.get(proxy_id)
        if proxy is not None:
            await repo.set_enabled(proxy, not proxy.is_enabled)
            await _audit(session, request, admin, "proxy_toggle", "proxy", str(proxy_id))
            await session.commit()
        return RedirectResponse("/proxies", status_code=303)

    @app.post("/proxies/{proxy_id}/delete")
    async def delete_proxy(proxy_id: int, request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = ProxyRepository(session)
        proxy = await repo.get(proxy_id)
        if proxy is not None:
            await repo.delete(proxy)
            await _audit(session, request, admin, "proxy_delete", "proxy", str(proxy_id))
            await session.commit()
        return RedirectResponse("/proxies", status_code=303)

    @app.get("/monitors", response_class=HTMLResponse)
    async def monitors_page(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        monitors = await MonitorRepository(session).list_page(limit=100)
        rows = []
        for monitor in monitors:
            movie = await session.get(Movie, monitor.event_code)
            region = await session.get(Region, monitor.region_code)
            rows.append((monitor, movie, region))
        return templates.TemplateResponse(
            request, "monitors.html", _ctx(request, admin=admin, rows=rows, now=utcnow())
        )

    @app.post("/monitors/{monitor_id}/toggle")
    async def toggle_monitor(
        monitor_id: int, request: Request, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = MonitorRepository(session)
        monitor = await repo.get(monitor_id)
        if monitor is not None:
            await repo.set_active(monitor, not monitor.is_active)
            await _audit(session, request, admin, "monitor_toggle", "monitor", str(monitor_id))
            await session.commit()
        return RedirectResponse("/monitors", status_code=303)

    @app.post("/monitors/{monitor_id}/run")
    async def run_monitor(
        monitor_id: int, request: Request, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        await session.execute(
            update(Monitor)
            .where(Monitor.id == monitor_id)
            .values(next_run_at=utcnow(), lease_owner=None, lease_until=None, is_active=True)
        )
        await _audit(session, request, admin, "monitor_run", "monitor", str(monitor_id))
        await session.commit()
        return RedirectResponse("/monitors", status_code=303)

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(
        request: Request, status: str | None = None, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        alerts = await AlertRepository(session).list_recent(limit=200, status=status)
        return templates.TemplateResponse(
            request,
            "alerts.html",
            _ctx(request, admin=admin, alerts=alerts, status=status or ""),
        )

    @app.post("/alerts/{alert_id}/resend")
    async def resend_alert(
        alert_id: int, request: Request, session: AsyncSession = Depends(_db)
    ):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        await session.execute(
            update(AlertEvent)
            .where(AlertEvent.id == alert_id)
            .values(status="pending", attempts=0, last_error=None, next_attempt_at=None)
        )
        await _audit(session, request, admin, "alert_resend", "alert", str(alert_id))
        await session.commit()
        return RedirectResponse("/alerts", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        rows = list(
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)
                )
            ).scalars()
        )
        return templates.TemplateResponse(
            request, "audit.html", _ctx(request, admin=admin, rows=rows)
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        repo = SettingsRepository(session)
        overrides = await repo.all()
        use_proxy = bool(await repo.get("use_proxy", True))
        return templates.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                admin=admin,
                settings=settings,
                overrides=overrides,
                use_proxy=use_proxy,
                direct_allowed=settings.direct_allowed,
            ),
        )

    @app.post("/settings")
    async def save_settings(request: Request, session: AsyncSession = Depends(_db)):
        loaded = await _require(request, session)
        if loaded is None:
            return RedirectResponse("/login", status_code=303)
        _, admin = loaded
        form = await request.form()
        if not valid_csrf(request.session.get("csrf"), str(form.get("csrf", ""))):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        repo = SettingsRepository(session)
        if form.get("use_proxy_present"):
            desired = str(form.get("use_proxy", "")).lower() in {"on", "1", "true", "yes"}
            if not desired and not settings.direct_allowed:
                return RedirectResponse(
                    "/settings?msg=Direct+mode+is+disabled+in+production", status_code=303
                )
            await repo.set("use_proxy", desired)
            await _audit(
                session,
                request,
                admin,
                "use_proxy",
                "setting",
                "use_proxy",
                {"value": desired},
            )
        for key, minimum, maximum in (
            ("seat_alert_threshold", 1, 200),
            ("max_seat_checks_per_scan", 0, 100),
        ):
            raw = form.get(key)
            if raw is None:
                continue
            try:
                value = max(minimum, min(maximum, int(str(raw))))
            except ValueError:
                continue
            await repo.set(key, value)
            await _audit(session, request, admin, "setting_update", "setting", key, {"value": value})
        await session.commit()
        return RedirectResponse("/settings?msg=Settings+saved", status_code=303)

    return app


def run() -> None:
    uvicorn.run(
        "bmsbot.admin.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )


if __name__ == "__main__":
    run()
