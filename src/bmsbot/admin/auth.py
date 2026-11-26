from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import AdminSession, AdminUser

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_admin_session(
    session: AsyncSession, admin: AdminUser, *, ttl_sec: int, ip: str | None, user_agent: str | None
) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    await session.execute(delete(AdminSession).where(AdminSession.expires_at < utcnow()))
    session.add(
        AdminSession(
            id=_token_hash(token),
            admin_id=admin.id,
            csrf_token=csrf,
            expires_at=utcnow() + timedelta(seconds=ttl_sec),
            ip=ip,
            user_agent=(user_agent or "")[:255],
        )
    )
    admin.last_login_at = utcnow()
    await session.flush()
    return token, csrf


async def load_admin_session(
    session: AsyncSession, token: str
) -> tuple[AdminSession, AdminUser] | None:
    row = await session.get(AdminSession, _token_hash(token))
    if row is None or row.expires_at < utcnow():
        return None
    admin = await session.get(AdminUser, row.admin_id)
    if admin is None or not admin.is_active:
        return None
    return row, admin


async def delete_admin_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(AdminSession).where(AdminSession.id == _token_hash(token)))
    await session.flush()


def valid_csrf(expected: str | None, received: str | None) -> bool:
    return bool(expected and received and hmac.compare_digest(expected, received))
