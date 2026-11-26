from __future__ import annotations

from bmsbot.bms.parsers import (
    effective_availability,
    parse_movie_links,
    parse_regions,
    parse_search_results,
    parse_seat_primary,
    parse_showtimes,
)
from bmsbot.domain.enums import Availability


def test_parse_regions() -> None:
    text = (
        'var regionlst={"HYD":[{"code":"HYD","name":"Hyderabad","slug":"hyderabad",'
        '"lat":"17.3","long":"78.4"}]};var regionalias=[]'
    )
    regions = parse_regions(text)
    assert len(regions) == 1
    assert regions[0].code == "HYD"
    assert regions[0].slug == "hyderabad"


def test_parse_search_results() -> None:
    payload = {
        "data": {
            "widgets": {
                "results": {
                    "cards": [
                        {"identifier": "ET00491771", "result-title": "Dune: Part Three"},
                        {"identifier": "not-an-event", "result-title": "Junk"},
                    ]
                }
            }
        }
    }
    results = parse_search_results(payload)
    assert [(row.event_code, row.title) for row in results] == [
        ("ET00491771", "Dune: Part Three")
    ]


def test_parse_movie_links() -> None:
    html = (
        '<a href="/movies/bhogi/ET00489093">Bhogi</a>'
        '<a href="https://in.bookmyshow.com/hyderabad/movies/dune-part-three/ET00491771">Dune</a>'
    )
    rows = parse_movie_links(html)
    assert ("ET00489093", "bhogi", "Bhogi") in rows
    assert ("ET00491771", "dune-part-three", "Dune Part Three") in rows


def test_parse_showtimes_and_availability() -> None:
    data = {
        "header": {"title": {"text": "Bhogi"}},
        "additionalData": {"dateCode": "20261127"},
        "topStickyWidgets": [
            {
                "data": [
                    {"cta": {"type": "dateSelector", "additionalData": {"dateCode": "20261127"}}},
                    {"cta": {"type": "dateSelector", "additionalData": {"dateCode": "20261128"}}},
                ]
            }
        ],
        "showtimeWidgets": [
            {
                "type": "groupList",
                "data": [
                    {
                        "type": "venue-card",
                        "additionalData": {"venueCode": "PRHN", "venueName": "Prasads"},
                        "showtimesSections": [
                            {
                                "showtimes": [
                                    {
                                        "title": "08:00 AM",
                                        "screenAttr": "IMAX",
                                        "additionalData": {
                                            "sessionId": "123",
                                            "availStatus": "0",
                                            "showTime": "08:00 AM",
                                            "showDateTime": "202611270800",
                                        },
                                    },
                                    {
                                        "title": "11:00 AM",
                                        "screenAttr": "4K",
                                        "additionalData": {
                                            "sessionId": "124",
                                            "availStatus": "3",
                                            "showTime": "11:00 AM",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ],
            }
        ],
    }
    snapshot = parse_showtimes(data)
    assert snapshot.title == "Bhogi"
    assert snapshot.actual_date == "20261127"
    assert snapshot.available_dates == ["20261127", "20261128"]
    assert len(snapshot.theatres) == 1
    statuses = [show.availability_text for show in snapshot.theatres[0].shows]
    assert statuses == ["SOLD OUT", "AVAILABLE"]


def test_parse_primary_seat_layout() -> None:
    payload = {
        "data": {
            "showTimes": [
                {
                    "sessionId": "1",
                    "availStatus": "3",
                    "categories": [{"priceDesc": "EXECUTIVE", "availStatus": "3"}],
                },
                {
                    "sessionId": "2",
                    "availStatus": "3",
                    "categories": [
                        {"priceDesc": "EXECUTIVE", "availStatus": "0"},
                        {"priceDesc": "VIP", "availStatus": "0"},
                    ],
                },
            ]
        }
    }
    info = parse_seat_primary(payload)
    assert effective_availability(info["1"]) == Availability.AVAILABLE
    assert effective_availability(info["2"]) == Availability.SOLD_OUT
