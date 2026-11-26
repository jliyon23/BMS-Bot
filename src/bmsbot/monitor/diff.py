from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bmsbot.domain.enums import AlertType, Availability


@dataclass(frozen=True)
class ShowState:
    venue_code: str
    venue_name: str
    session_id: str
    time: str
    show_datetime: str | None
    format: str
    availability_code: str | None
    availability_text: str
    seat_total: int | None = None
    seat_available: int | None = None


@dataclass(frozen=True)
class Change:
    alert_type: AlertType
    dedupe_fragment: str
    payload: dict[str, Any] = field(default_factory=dict)


def _show_payload(show: ShowState) -> dict[str, Any]:
    return {
        "venue_code": show.venue_code,
        "venue_name": show.venue_name,
        "session_id": show.session_id,
        "time": show.time,
        "show_datetime": show.show_datetime,
        "format": show.format,
        "availability": show.availability_text,
    }


def diff_showtimes(
    previous: list[ShowState],
    current: list[ShowState],
    *,
    seat_threshold: int = 5,
) -> list[Change]:
    """Pure diff between two showtime snapshots.

    A new venue produces one NEW_THEATRE change (with its shows); a new session
    at an existing venue produces NEW_SHOW. Sold-out takes precedence over a
    generic status change. Seat-count alerts only fire when both snapshots have
    an exact count and the change meets the threshold.
    """
    previous_by_key = {(show.venue_code, show.session_id): show for show in previous}
    current_by_key = {(show.venue_code, show.session_id): show for show in current}
    previous_venues = {show.venue_code for show in previous}
    changes: list[Change] = []

    new_by_venue: dict[str, list[ShowState]] = {}
    for show in current:
        if (show.venue_code, show.session_id) not in previous_by_key:
            new_by_venue.setdefault(show.venue_code, []).append(show)

    for venue_code, shows in new_by_venue.items():
        venue_name = shows[0].venue_name
        if venue_code not in previous_venues:
            changes.append(
                Change(
                    alert_type=AlertType.NEW_THEATRE,
                    dedupe_fragment=f"venue:{venue_code}",
                    payload={
                        "venue_code": venue_code,
                        "venue_name": venue_name,
                        "shows": [{"time": show.time, "format": show.format} for show in shows],
                    },
                )
            )
        else:
            for show in shows:
                changes.append(
                    Change(
                        alert_type=AlertType.NEW_SHOW,
                        dedupe_fragment=f"show:{venue_code}:{show.session_id}",
                        payload=_show_payload(show),
                    )
                )

    for key, old_show in previous_by_key.items():
        if key in current_by_key:
            continue
        changes.append(
            Change(
                alert_type=AlertType.SHOW_REMOVED,
                dedupe_fragment=f"removed:{old_show.venue_code}:{old_show.session_id}",
                payload=_show_payload(old_show),
            )
        )

    for key, new_show in current_by_key.items():
        old_show = previous_by_key.get(key)
        if old_show is None:
            continue

        old_status = old_show.availability_text
        new_status = new_show.availability_text
        if old_status == new_status:
            pass
        elif new_status == Availability.SOLD_OUT.value:
            changes.append(
                Change(
                    alert_type=AlertType.SOLD_OUT,
                    dedupe_fragment=f"soldout:{key[0]}:{key[1]}:{old_status}",
                    payload={**_show_payload(new_show), "previous_status": old_status},
                )
            )
        else:
            changes.append(
                Change(
                    alert_type=AlertType.STATUS_CHANGE,
                    dedupe_fragment=f"status:{key[0]}:{key[1]}:{old_status}:{new_status}",
                    payload={
                        **_show_payload(new_show),
                        "previous_status": old_status,
                        "current_status": new_status,
                    },
                )
            )

        if (
            old_show.seat_available is not None
            and new_show.seat_available is not None
            and old_show.seat_available != new_show.seat_available
        ):
            delta = new_show.seat_available - old_show.seat_available
            if abs(delta) >= seat_threshold:
                changes.append(
                    Change(
                        alert_type=AlertType.SEAT_COUNT,
                        dedupe_fragment=(
                            f"seats:{key[0]}:{key[1]}:{old_show.seat_available}:"
                            f"{new_show.seat_available}"
                        ),
                        payload={
                            **_show_payload(new_show),
                            "old_available": old_show.seat_available,
                            "new_available": new_show.seat_available,
                            "delta": delta,
                            "total": new_show.seat_total,
                        },
                    )
                )

    return changes


def booking_opened(had_shows: bool, has_shows: bool, date_code: str) -> Change | None:
    if had_shows or not has_shows:
        return None
    return Change(
        alert_type=AlertType.BOOKING_OPEN,
        dedupe_fragment=f"open:{date_code}",
        payload={"date_code": date_code},
    )
