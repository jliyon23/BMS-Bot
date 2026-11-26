from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from bmsbot.domain.enums import AVAILABILITY_BY_CODE, Availability


@dataclass(frozen=True)
class RegionInfo:
    code: str
    name: str
    slug: str
    lat: str
    lng: str
    group: str = ""


@dataclass(frozen=True)
class SearchResult:
    event_code: str
    title: str


@dataclass
class ParsedShow:
    venue_code: str
    venue_name: str
    session_id: str
    time: str
    show_datetime: str | None
    format: str
    availability_code: str | None
    availability_text: str = Availability.UNKNOWN.value


@dataclass
class ParsedTheatre:
    venue_code: str
    venue_name: str
    shows: list[ParsedShow] = field(default_factory=list)


@dataclass
class ShowtimesSnapshot:
    title: str | None
    actual_date: str | None
    available_dates: list[str]
    theatres: list[ParsedTheatre]

    @property
    def has_shows(self) -> bool:
        return any(theatre.shows for theatre in self.theatres)


@dataclass(frozen=True)
class SeatCounts:
    total: int = 0
    available: int = 0
    sold: int = 0
    blocked: int = 0
    categories: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CategoryStatus:
    price_desc: str
    availability_code: str
    price: str


@dataclass(frozen=True)
class PrimarySeatInfo:
    session_id: str
    availability_code: str
    categories: list[CategoryStatus] = field(default_factory=list)


def parse_seat_primary(payload: dict[str, Any]) -> dict[str, PrimarySeatInfo]:
    """Parse /movies-data/seatlayout/v1/primary.

    The primary endpoint returns per-show category availability, not exact
    seat counts. It is still useful for sold-out and category-level status
    changes without opening a booking transaction.
    """
    data = payload.get("data", payload)
    shows = data.get("showTimes") or []
    result: dict[str, PrimarySeatInfo] = {}
    for show in shows:
        session_id = show.get("sessionId")
        if not session_id:
            continue
        categories = [
            CategoryStatus(
                price_desc=str(category.get("priceDesc") or ""),
                availability_code=str(category.get("availStatus") or ""),
                price=str(category.get("curPrice") or ""),
            )
            for category in (show.get("categories") or [])
        ]
        result[str(session_id)] = PrimarySeatInfo(
            session_id=str(session_id),
            availability_code=str(show.get("availStatus") or ""),
            categories=categories,
        )
    return result


def effective_availability(info: PrimarySeatInfo) -> Availability:
    if info.availability_code == "0":
        return Availability.SOLD_OUT
    if info.categories and all(category.availability_code == "0" for category in info.categories):
        return Availability.SOLD_OUT
    return AVAILABILITY_BY_CODE.get(info.availability_code, Availability.UNKNOWN)


def parse_regions(text: str) -> list[RegionInfo]:
    match = re.search(r"var\s+regionlst\s*=\s*(\{.*\})\s*;\s*var\s+regionalias\s*=", text, re.S)
    if not match:
        return []
    raw = json.loads(match.group(1))
    regions: list[RegionInfo] = []
    seen: set[str] = set()
    for group, items in raw.items():
        for item in items:
            code = item.get("code") or item.get("RegionCode")
            if not code or code in seen:
                continue
            seen.add(code)
            regions.append(
                RegionInfo(
                    code=str(code),
                    name=str(item.get("name") or item.get("RegionName") or code),
                    slug=str(item.get("slug") or item.get("RegionSlug") or str(code).lower()),
                    lat=str(item.get("lat") or item.get("Latitude") or ""),
                    lng=str(item.get("long") or item.get("Longitude") or ""),
                    group=str(group),
                )
            )
    return regions


def parse_search_results(payload: dict[str, Any]) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            identifier = node.get("identifier")
            title = node.get("result-title") or node.get("title")
            if isinstance(identifier, str) and re.fullmatch(r"ET\d{6,}", identifier, re.I):
                code = identifier.upper()
                if title and code not in seen:
                    seen.add(code)
                    results.append(SearchResult(code, str(title)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return results


def parse_movie_links(html: str) -> list[tuple[str, str, str]]:
    """Return (event_code, slug, title) for movie links on a BMS page."""
    found = re.findall(
        r'href="(?:https?://in\.bookmyshow\.com)?/(?:[a-z0-9\-]+/)?movies/([a-z0-9\-]+)/(ET\d+)"',
        html,
        re.I,
    )
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for slug, code in found:
        code = code.upper()
        if code in seen:
            continue
        seen.add(code)
        out.append((code, slug.lower(), slug.replace("-", " ").title()))
    return out


def _iter_venue_cards(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("type") == "venue-card":
            yield node
            return
        for value in node.values():
            yield from _iter_venue_cards(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_venue_cards(value)


def parse_showtimes(data: dict[str, Any]) -> ShowtimesSnapshot:
    title = ((data.get("header") or {}).get("title") or {}).get("text")
    actual_date = (data.get("additionalData") or {}).get("dateCode")
    dates: list[str] = []
    for widget in data.get("topStickyWidgets") or []:
        for item in widget.get("data") or []:
            cta = item.get("cta") or {}
            if cta.get("type") != "dateSelector":
                continue
            date_code = (cta.get("additionalData") or {}).get("dateCode")
            if date_code and date_code not in dates:
                dates.append(str(date_code))

    theatres: dict[str, ParsedTheatre] = {}
    for card in _iter_venue_cards(data.get("showtimeWidgets") or []):
        additional = card.get("additionalData") or {}
        venue_code = additional.get("venueCode")
        if not venue_code:
            continue
        theatre = theatres.setdefault(
            venue_code,
            ParsedTheatre(
                venue_code=str(venue_code),
                venue_name=str(additional.get("venueName") or venue_code),
            ),
        )
        known_sessions = {show.session_id for show in theatre.shows}
        for section in card.get("showtimesSections") or []:
            for showtime in section.get("showtimes") or []:
                details = showtime.get("additionalData") or {}
                session_id = details.get("sessionId")
                if not session_id or str(session_id) in known_sessions:
                    continue
                availability_code = details.get("availStatus")
                availability = AVAILABILITY_BY_CODE.get(
                    str(availability_code), Availability.UNKNOWN
                )
                theatre.shows.append(
                    ParsedShow(
                        venue_code=str(venue_code),
                        venue_name=theatre.venue_name,
                        session_id=str(session_id),
                        time=str(details.get("showTime") or showtime.get("title") or "?"),
                        show_datetime=(
                            str(details["showDateTime"]) if details.get("showDateTime") else None
                        ),
                        format=str(showtime.get("screenAttr") or ""),
                        availability_code=(
                            str(availability_code) if availability_code is not None else None
                        ),
                        availability_text=availability.value,
                    )
                )
                known_sessions.add(str(session_id))

    return ShowtimesSnapshot(
        title=title,
        actual_date=str(actual_date) if actual_date else None,
        available_dates=dates,
        theatres=[theatre for theatre in theatres.values() if theatre.shows],
    )


_SEAT_AVAILABLE = {"0", "a", "av", "available", "free", "vacant", "y"}
_SEAT_SOLD = {"1", "s", "so", "sold", "booked", "occupied", "n"}
_SEAT_BLOCKED = {"2", "b", "blocked", "held", "reserved"}


def parse_seat_layout(payload: dict[str, Any]) -> SeatCounts | None:
    """Best-effort parser for the seat-layout payload.

    BMS does not document this shape. We support the common aggregate fields
    and, when a seat list is present, count seat statuses. The monitor falls
    back to the showtimes availability bucket when this returns None.
    """
    data = payload.get("data", payload)

    explicit_total = _first_int(data, ("totalSeats", "total_seats", "total"))
    explicit_available = _first_int(data, ("availableSeats", "available_seats", "available"))
    explicit_sold = _first_int(data, ("soldSeats", "sold_seats", "sold", "booked"))
    if explicit_total and explicit_available is not None:
        return SeatCounts(
            total=explicit_total,
            available=explicit_available,
            sold=explicit_sold or max(0, explicit_total - explicit_available),
        )

    available = sold = blocked = 0
    categories: dict[str, Any] = {}

    def walk(node: Any, path: str = "") -> None:
        nonlocal available, sold, blocked
        if isinstance(node, dict):
            status = None
            for key in ("seatStatus", "status", "availStatus", "seat_status"):
                if key in node and not isinstance(node[key], (dict, list)):
                    status = str(node[key]).strip().lower()
                    break
            if status is not None and any(k in node for k in ("seatNo", "seatNumber", "seatId", "seat_id")):
                if status in _SEAT_AVAILABLE:
                    available += 1
                elif status in _SEAT_SOLD:
                    sold += 1
                elif status in _SEAT_BLOCKED:
                    blocked += 1
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(data)
    total = available + sold + blocked
    if total == 0:
        return None
    return SeatCounts(
        total=total,
        available=available,
        sold=sold,
        blocked=blocked,
        categories=categories,
    )


def _first_int(data: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None
