#!/usr/bin/env python3
"""Standalone proxy-only showtimes viewer.

This is a thin wrapper around the reusable BMS client. It refuses to contact
BookMyShow without an explicit proxy.

    BMS_PROXY=http://host:8080 python scripts/bms_showtimes.py \
        --city hyderabad --movie "Bhogi"

    python scripts/bms_showtimes.py --proxy http://host:8080 \
        --movie https://in.bookmyshow.com/hyderabad/movies/bhogi/ET00489093
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime

from bmsbot.bms.client import BmsClient, BmsError, NoProxyAvailable
from bmsbot.bms.http import AsyncRateLimiter
from bmsbot.config import get_settings
from bmsbot.proxies.pool import StaticProxyPool


def parse_date(value: str) -> str:
    text = value.strip().lower()
    if text == "today":
        return datetime.now().strftime("%Y%m%d")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise argparse.ArgumentTypeError("Use today, YYYY-MM-DD or YYYYMMDD")


async def run(args: argparse.Namespace) -> int:
    proxy = args.proxy or os.getenv("BMS_PROXY")
    if not proxy:
        print(
            "error: a proxy is required. Pass --proxy or set BMS_PROXY.\n"
            "The scraper never contacts BookMyShow directly.",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    client = BmsClient(
        settings,
        StaticProxyPool(proxy),
        limiter=AsyncRateLimiter(settings.max_requests_per_sec),
    )
    region = await client.region(args.city)

    match = re.search(r"\b(ET\d{6,})\b", args.movie, re.I)
    if match:
        event_code = match.group(1).upper()
        title = None
    else:
        results = await client.search_movies(args.movie, region)
        if not results:
            print("No movie matched.", file=sys.stderr)
            return 1
        event_code, title = results[0].event_code, results[0].title

    date_code = args.date or datetime.now().strftime("%Y%m%d")
    snapshot = await client.showtimes(event_code, date_code, region)
    title = snapshot.title or title or event_code

    if args.json:
        print(
            json.dumps(
                {
                    "city": {"code": region.code, "name": region.name},
                    "movie": {"event_code": event_code, "title": title},
                    "date": snapshot.actual_date or date_code,
                    "available_dates": snapshot.available_dates,
                    "theatres": [
                        {
                            "venue_code": theatre.venue_code,
                            "venue_name": theatre.venue_name,
                            "shows": [
                                {
                                    "session_id": show.session_id,
                                    "time": show.time,
                                    "format": show.format,
                                    "availability": show.availability_text,
                                }
                                for show in theatre.shows
                            ],
                        }
                        for theatre in snapshot.theatres
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"Movie : {title} ({event_code})")
    print(f"City  : {region.name} ({region.code})")
    print(f"Date  : {snapshot.actual_date or date_code}")
    print(f"Found : {len(snapshot.theatres)} theatres")
    for theatre in snapshot.theatres:
        print(f"\n{theatre.venue_name} [{theatre.venue_code}]")
        for show in theatre.shows:
            print(
                f"  {show.time:<10} {(show.format or '-')[:34]:<34} "
                f"{show.availability_text:<12} session {show.session_id}"
            )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Proxy-only BMS showtimes viewer")
    parser.add_argument("--city", default="hyderabad")
    parser.add_argument("--movie", required=True, help="Movie name, URL or ET code")
    parser.add_argument("--date", type=parse_date, default=None)
    parser.add_argument("--proxy", default=None, help="http://host:port or socks5://host:port")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        raise SystemExit(asyncio.run(run(args)))
    except NoProxyAvailable:
        print("error: proxy is not reachable/active. Refusing direct access.", file=sys.stderr)
        raise SystemExit(2)
    except BmsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
