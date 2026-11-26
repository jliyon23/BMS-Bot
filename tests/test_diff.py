from __future__ import annotations

from bmsbot.domain.enums import AlertType
from bmsbot.monitor.diff import ShowState, booking_opened, diff_showtimes


def show(
    venue_code: str = "V1",
    venue_name: str = "Theatre One",
    session_id: str = "S1",
    time: str = "10:00 AM",
    status: str = "AVAILABLE",
    seats: int | None = None,
) -> ShowState:
    return ShowState(
        venue_code=venue_code,
        venue_name=venue_name,
        session_id=session_id,
        time=time,
        show_datetime="202611271000",
        format="IMAX",
        availability_code="3",
        availability_text=status,
        seat_total=200 if seats is not None else None,
        seat_available=seats,
    )


def test_new_venue_creates_single_new_theatre_change() -> None:
    previous = [show()]
    current = [show(), show(venue_code="V2", venue_name="Theatre Two", session_id="S2")]
    changes = diff_showtimes(previous, current)
    assert [change.alert_type for change in changes] == [AlertType.NEW_THEATRE]
    assert changes[0].payload["venue_code"] == "V2"


def test_new_show_at_existing_venue() -> None:
    previous = [show()]
    current = [show(), show(session_id="S2", time="1:00 PM")]
    changes = diff_showtimes(previous, current)
    assert [change.alert_type for change in changes] == [AlertType.NEW_SHOW]


def test_removed_and_sold_out_and_status_change() -> None:
    previous = [
        show(session_id="S1", status="AVAILABLE"),
        show(session_id="S2", status="AVAILABLE"),
        show(session_id="S3", status="AVAILABLE"),
    ]
    current = [
        show(session_id="S1", status="SOLD OUT"),
        show(session_id="S2", status="FILLING FAST"),
    ]
    changes = diff_showtimes(previous, current)
    types = {change.alert_type for change in changes}
    assert AlertType.SOLD_OUT in types
    assert AlertType.STATUS_CHANGE in types
    assert AlertType.SHOW_REMOVED in types


def test_seat_count_threshold() -> None:
    previous = [show(seats=100)]
    below = diff_showtimes(previous, [show(seats=96)], seat_threshold=5)
    assert AlertType.SEAT_COUNT not in {change.alert_type for change in below}
    above = diff_showtimes(previous, [show(seats=95)], seat_threshold=5)
    assert [change.alert_type for change in above] == [AlertType.SEAT_COUNT]
    assert above[0].payload["delta"] == -5


def test_booking_opened_only_on_transition() -> None:
    assert booking_opened(False, True, "20261127") is not None
    assert booking_opened(True, True, "20261127") is None
    assert booking_opened(False, False, "20261127") is None


def test_no_changes_returns_nothing() -> None:
    assert diff_showtimes([show()], [show()]) == []
