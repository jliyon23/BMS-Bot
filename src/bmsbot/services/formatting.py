from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from bmsbot.domain.enums import ALERT_ICONS, ALERT_TITLES, AlertType

ALERT_EXPLANATION = """<b>What each alert means</b>

🏛️ <b>New theatre added</b> — a cinema started showing this movie.
🕒 <b>New showtime</b> — a new time was added at a theatre.
📅 <b>Bookings opened</b> — a new date is now open for booking.
🔴 <b>All seats sold out</b> — no seats remain for a show.
🟠 <b>Seat status changed</b> — Available → Filling Fast → Almost Full → Sold Out.
🔢 <b>Seat count changed</b> — the exact number of available seats moved (when seat-map data is available).
❌ <b>Show removed</b> — a show was removed or cancelled.

Tap the buttons below to choose which alerts you want.
You can change this later with /watches."""


def _date_label(date_code: str) -> str:
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b %Y")
    except ValueError:
        return date_code


def _show_line(item: dict[str, Any]) -> str:
    time = escape(str(item.get("time") or "?"))
    fmt = escape(str(item.get("format") or "").strip())
    suffix = f" · {fmt}" if fmt else ""
    return f"• <b>{time}</b>{suffix}"


def format_batch(
    *,
    movie_title: str,
    region_name: str,
    date_code: str,
    changes: list[tuple[AlertType, dict[str, Any]]],
) -> str:
    """Build one clean Telegram HTML message for a user/movie/date batch."""
    lines: list[str] = [
        f"🎬 <b>{escape(movie_title)}</b> — {escape(region_name)}",
        f"📅 <b>{_date_label(date_code)}</b>",
        "",
    ]

    grouped: dict[AlertType, list[dict[str, Any]]] = {}
    for alert_type, payload in changes:
        grouped.setdefault(alert_type, []).append(payload)

    for alert_type in AlertType:
        payloads = grouped.get(alert_type)
        if not payloads:
            continue
        lines.append(f"{ALERT_ICONS[alert_type]} <b>{ALERT_TITLES[alert_type]}</b>")
        if alert_type is AlertType.NEW_THEATRE:
            for payload in payloads:
                venue = escape(str(payload.get("venue_name") or payload.get("venue_code")))
                lines.append(f"• <b>{venue}</b>")
                for show in payload.get("shows", [])[:12]:
                    lines.append(f"  {_show_line(show)}")
        elif alert_type is AlertType.NEW_SHOW:
            for payload in payloads:
                venue = escape(str(payload.get("venue_name")))
                lines.append(f"• {venue} — {_show_line(payload).removeprefix('• ')}")
        elif alert_type in (AlertType.SOLD_OUT, AlertType.STATUS_CHANGE):
            for payload in payloads:
                venue = escape(str(payload.get("venue_name")))
                previous = escape(str(payload.get("previous_status") or "?"))
                current = escape(str(payload.get("availability") or "?"))
                lines.append(
                    f"• {venue} — <b>{escape(str(payload.get('time') or '?'))}</b>  "
                    f"{previous} → <b>{current}</b>"
                )
        elif alert_type is AlertType.SEAT_COUNT:
            for payload in payloads:
                venue = escape(str(payload.get("venue_name")))
                delta = int(payload.get("delta") or 0)
                sign = "+" if delta > 0 else ""
                lines.append(
                    f"• {venue} — <b>{escape(str(payload.get('time') or '?'))}</b>  "
                    f"{payload.get('old_available')} → <b>{payload.get('new_available')}</b> "
                    f"({sign}{delta})"
                )
        elif alert_type is AlertType.SHOW_REMOVED:
            for payload in payloads:
                venue = escape(str(payload.get("venue_name")))
                lines.append(
                    f"• {venue} — <b>{escape(str(payload.get('time') or '?'))}</b>"
                )
        elif alert_type is AlertType.BOOKING_OPEN:
            lines.append("• Booking is now open for this date.")
        lines.append("")

    return "\n".join(lines).strip()


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Split long HTML messages on line boundaries without breaking tags."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
