from __future__ import annotations

from enum import StrEnum


class AlertType(StrEnum):
    NEW_THEATRE = "new_theatre"
    NEW_SHOW = "new_show"
    BOOKING_OPEN = "booking_open"
    SOLD_OUT = "sold_out"
    STATUS_CHANGE = "status_change"
    SEAT_COUNT = "seat_count"
    SHOW_REMOVED = "show_removed"


DEFAULT_ALERTS: dict[AlertType, bool] = {
    AlertType.NEW_THEATRE: True,
    AlertType.NEW_SHOW: True,
    AlertType.BOOKING_OPEN: True,
    AlertType.SOLD_OUT: True,
    AlertType.STATUS_CHANGE: True,
    AlertType.SEAT_COUNT: False,
    AlertType.SHOW_REMOVED: True,
}

ALERT_ICONS: dict[AlertType, str] = {
    AlertType.NEW_THEATRE: "🏛️",
    AlertType.NEW_SHOW: "🕒",
    AlertType.BOOKING_OPEN: "📅",
    AlertType.SOLD_OUT: "🔴",
    AlertType.STATUS_CHANGE: "🟠",
    AlertType.SEAT_COUNT: "🔢",
    AlertType.SHOW_REMOVED: "❌",
}

ALERT_TITLES: dict[AlertType, str] = {
    AlertType.NEW_THEATRE: "New theatre added",
    AlertType.NEW_SHOW: "New showtime added",
    AlertType.BOOKING_OPEN: "Bookings opened",
    AlertType.SOLD_OUT: "All seats sold out",
    AlertType.STATUS_CHANGE: "Seat status changed",
    AlertType.SEAT_COUNT: "Seat count changed",
    AlertType.SHOW_REMOVED: "Show removed",
}


class ProxyTier(StrEnum):
    FREE = "free"
    PREMIUM = "premium"


class ProxyStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    DEAD = "dead"
    DISABLED = "disabled"


class MonitorKind(StrEnum):
    SHOWTIMES = "showtimes"
    SEAT = "seat"


class AlertStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Availability(StrEnum):
    SOLD_OUT = "SOLD OUT"
    ALMOST_FULL = "ALMOST FULL"
    FILLING_FAST = "FILLING FAST"
    AVAILABLE = "AVAILABLE"
    UNKNOWN = "UNKNOWN"


AVAILABILITY_BY_CODE: dict[str, Availability] = {
    "0": Availability.SOLD_OUT,
    "1": Availability.ALMOST_FULL,
    "2": Availability.FILLING_FAST,
    "3": Availability.AVAILABLE,
}
