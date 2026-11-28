"""``when_desc`` renders look times for sender messages (D190)."""

import string
from datetime import UTC, date, datetime

import pytest

from invoicebot.config.checks import BUTTON_TITLE_MAX
from invoicebot.rules.catalog import RULES
from invoicebot.rules.messages_es import (
    BUTTON_DOCS,
    DOCS_GONE,
    FETCH_PROGRESS,
    MESSAGES,
    buyer_desc,
    when_desc,
)


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


def test_br_d10_formats_with_exactly_its_fields() -> None:
    """BR-D10, D195: the done text takes serie, numero, folder and to, and nothing else."""
    fields = {name for _, name, _, _ in string.Formatter().parse(MESSAGES["BR-D10"]) if name}
    assert fields == {"serie", "numero", "folder", "to"}


def test_d206_progress_and_gone_notes_name_the_invoice() -> None:
    """D206: BR-F17's note and BR-D25's text both carry the invoice number."""
    assert "#47" in FETCH_PROGRESS.format(id=47)
    assert "#47" in DOCS_GONE.format(id=47)


def test_d206_docs_button_fits_the_title_limit() -> None:
    """D206: the [📎 Ver documentos] button title fits BUTTON_TITLE_MAX."""
    assert len(BUTTON_DOCS) <= BUTTON_TITLE_MAX


@pytest.mark.parametrize("rule_id", ["BR-F18", "BR-F19", "BR-F20"])
def test_d209_captcha_rules_are_in_the_catalog(rule_id: str) -> None:
    """D209: the captcha messages BR-F18, BR-F19 and BR-F20 are registered rules."""
    assert rule_id in RULES


@pytest.mark.parametrize("message_id", ["BR-F18", "BR-F19", "BR-F20", "BR-F20_shape"])
def test_d209_captcha_messages_format_with_their_placeholders(message_id: str) -> None:
    """D209: each captcha message formats with only its placeholders (BR-F18: portal, minutes)."""
    text = MESSAGES[message_id].format(portal="PEBSA", minutes=3)
    assert text
    if message_id == "BR-F18":
        assert "PEBSA" in text
        assert "3 min" in text
    else:
        assert text == MESSAGES[message_id]


@pytest.mark.parametrize(
    ("tipo", "tipo_doc", "num_doc", "expected"),
    [
        ("01", "", "", "de un cliente que no pude leer"),
        ("03", "", "", "de ningún cliente (boleta sin datos)"),
        ("01", "6", "20000000001", "del RUC 20000000001"),
    ],
)
def test_buyer_desc_names_an_unread_factura_buyer(
    tipo: str, tipo_doc: str, num_doc: str, expected: str
) -> None:
    """D213: a factura with no buyer says the buyer was not read; a boleta keeps "sin datos"."""
    assert buyer_desc(tipo, tipo_doc, num_doc) == expected
