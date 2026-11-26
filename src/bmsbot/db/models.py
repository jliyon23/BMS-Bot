from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bmsbot.db.base import Base, TimestampMixin, utcnow
from bmsbot.domain.enums import (
    AlertStatus,
    MonitorKind,
    ProxyStatus,
    ProxyTier,
)


class Region(Base, TimestampMixin):
    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str] = mapped_column(String(128), index=True)
    lat: Mapped[str] = mapped_column(String(32))
    lng: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PinCode(Base):
    __tablename__ = "pincodes"

    pin: Mapped[str] = mapped_column(String(6), primary_key=True)
    district: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(128))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    region_code: Mapped[str | None] = mapped_column(ForeignKey("regions.code"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("regions.code"))
    pin_code: Mapped[str | None] = mapped_column(String(6))
    language: Mapped[str] = mapped_column(String(8), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5))
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    watches: Mapped[list[Watch]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Movie(Base, TimestampMixin):
    __tablename__ = "movies"

    event_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str | None] = mapped_column(String(255))
    poster_url: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class RegionMovie(Base):
    __tablename__ = "region_movies"
    __table_args__ = (UniqueConstraint("region_code", "event_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(ForeignKey("regions.code", ondelete="CASCADE"))
    event_code: Mapped[str] = mapped_column(ForeignKey("movies.event_code", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Watch(Base, TimestampMixin):
    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("user_id", "region_code", "event_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    region_code: Mapped[str] = mapped_column(ForeignKey("regions.code"))
    event_code: Mapped[str] = mapped_column(ForeignKey("movies.event_code"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    user: Mapped[User] = relationship(back_populates="watches")
    prefs: Mapped[list[WatchAlertPref]] = relationship(
        back_populates="watch", cascade="all, delete-orphan"
    )


class WatchAlertPref(Base):
    __tablename__ = "watch_alert_prefs"
    __table_args__ = (UniqueConstraint("watch_id", "alert_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    alert_type: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    watch: Mapped[Watch] = relationship(back_populates="prefs")


class Monitor(Base, TimestampMixin):
    __tablename__ = "monitors"
    __table_args__ = (
        UniqueConstraint("region_code", "event_code", "date_code", "kind", "venue_code"),
        Index("ix_monitors_due", "is_active", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(ForeignKey("regions.code"))
    event_code: Mapped[str] = mapped_column(ForeignKey("movies.event_code"), index=True)
    date_code: Mapped[str] = mapped_column(String(8), index=True)
    kind: Mapped[str] = mapped_column(String(16), default=MonitorKind.SHOWTIMES.value)
    venue_code: Mapped[str] = mapped_column(String(16), default="")
    interval_sec: Mapped[int] = mapped_column(Integer, default=10)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    has_shows: Mapped[bool] = mapped_column(Boolean, default=False)
    primed: Mapped[bool] = mapped_column(Boolean, default=False)
    first_open_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime)
    lag_ms: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)


class Theatre(Base, TimestampMixin):
    __tablename__ = "theatres"

    venue_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[str | None] = mapped_column(String(32))
    lng: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class Show(Base, TimestampMixin):
    __tablename__ = "shows"
    __table_args__ = (
        UniqueConstraint("venue_code", "session_id", "event_code", "date_code"),
        Index("ix_shows_lookup", "event_code", "date_code", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_code: Mapped[str] = mapped_column(ForeignKey("theatres.venue_code"))
    event_code: Mapped[str] = mapped_column(ForeignKey("movies.event_code"))
    date_code: Mapped[str] = mapped_column(String(8))
    session_id: Mapped[str] = mapped_column(String(32))
    show_datetime: Mapped[str | None] = mapped_column(String(14))
    show_time: Mapped[str] = mapped_column(String(32))
    format: Mapped[str] = mapped_column(String(128), default="")
    availability_code: Mapped[str | None] = mapped_column(String(4))
    availability_text: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    seat_total: Mapped[int | None] = mapped_column(Integer)
    seat_available: Mapped[int | None] = mapped_column(Integer)
    seat_sold: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seat_alert_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sold_out_alert_at: Mapped[datetime | None] = mapped_column(DateTime)


class SeatSnapshot(Base):
    __tablename__ = "seat_snapshots"
    __table_args__ = (Index("ix_seat_snapshots_show_time", "show_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(default=utcnow)
    total: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[int] = mapped_column(Integer, default=0)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    categories_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_pending", "status", "created_at"),
        Index("ix_alert_events_user", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    monitor_id: Mapped[int | None] = mapped_column(ForeignKey("monitors.id", ondelete="SET NULL"))
    alert_type: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=AlertStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class Proxy(Base, TimestampMixin):
    __tablename__ = "proxies"
    __table_args__ = (Index("ix_proxies_priority", "status", "tier", "p95_ms"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(255), unique=True)
    protocol: Mapped[str] = mapped_column(String(16), default="http")
    host: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(2))
    anonymity: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32), default="manual")
    tier: Mapped[str] = mapped_column(String(16), default=ProxyTier.FREE.value, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_rotating: Mapped[bool] = mapped_column(Boolean, default=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    exit_ip: Mapped[str | None] = mapped_column(String(45))
    p50_ms: Mapped[int] = mapped_column(Integer, default=0)
    p95_ms: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_success: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    validation_rounds: Mapped[int] = mapped_column(Integer, default=0)
    validation_successes: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=ProxyStatus.CANDIDATE.value)
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProxyCheck(Base):
    __tablename__ = "proxy_checks"
    __table_args__ = (Index("ix_proxy_checks_proxy_time", "proxy_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proxy_id: Mapped[int] = mapped_column(ForeignKey("proxies.id", ondelete="CASCADE"))
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class FSMState(Base):
    __tablename__ = "fsm_states"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
