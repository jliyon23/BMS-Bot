from __future__ import annotations

from bmsbot.domain.enums import AlertType
from bmsbot.services.formatting import format_batch, split_message


def test_batch_contains_icons_bold_and_escaped_html() -> None:
    message = format_batch(
        movie_title="Movie <script>",
        region_name="Hyderabad",
        date_code="20261127",
        changes=[
            (
                AlertType.NEW_THEATRE,
                {
                    "venue_name": "PVR <b>Mall</b>",
                    "shows": [{"time": "10:00 AM", "format": "IMAX"}],
                },
            ),
            (
                AlertType.SOLD_OUT,
                {
                    "venue_name": "INOX",
                    "time": "1:00 PM",
                    "previous_status": "AVAILABLE",
                    "availability": "SOLD OUT",
                },
            ),
        ],
    )
    assert "🏛️" in message
    assert "<b>" in message
    assert "&lt;script&gt;" in message
    assert "&lt;b&gt;Mall&lt;/b&gt;" in message
    assert "AVAILABLE → <b>SOLD OUT</b>" in message


def test_split_message_keeps_chunks_under_limit() -> None:
    text = "\n".join(f"line {index}" for index in range(200))
    chunks = split_message(text, limit=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
