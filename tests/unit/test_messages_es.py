"""``when_desc`` renders look times for sender messages (D190)."""

from datetime import UTC, date, datetime

import pytest

from invoicebot.rules.messages_es import when_desc


@pytest.mark.parametrize(
    ("at", "today", "expected"),
    [
        (datetime(2026, 11, 24, 5, 15, tzinfo=UTC), date(2026, 11, 24), "hoy a las 05:15"),
        (datetime(2026, 11, 25, 9, 0, tzinfo=UTC), date(2026, 11, 24), "mañana a las 09:00"),
        (datetime(2026, 11, 26, 9, 0, tzinfo=UTC), date(2026, 11, 24), "el 26/11 a las 09:00"),
        (datetime(2027, 1, 1, 0, 5, tzinfo=UTC), date(2026, 12, 31), "mañana a las 00:05"),
    ],
)
def test_when_desc(at: datetime, today: date, expected: str) -> None:
    """D190: a look time reads as hoy, mañana or a day and month."""
    assert when_desc(at, today) == expected
