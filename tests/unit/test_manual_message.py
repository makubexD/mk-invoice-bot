"""Tests for BR-F07's manual-required message (BR-F07, S7.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal

import pytest

from invoicebot.config.models import Brand, Flow, FlowInbox, Portal, Store, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.chain import Attempt
from invoicebot.pipeline.manual_message import LookTimes, StoreFacts, docs_needed_text, manual_message
from invoicebot.rules.messages_es import (
    FETCH_AUTH_LINE,
    FETCH_NO_PORTAL_LINE,
    FETCH_NOT_FOUND_LINE,
    FETCH_PORTAL_TODO_LINE,
    FETCH_TAUGHT_PORTAL_LINE,
    FETCH_TECHNICAL_LINE,
    MESSAGES,
    fetcher_desc,
)

STORE_RUC = "20000000001"
PORTAL_KEY = "synthetic_portal"
PORTAL_HOST = "portal.example"
PORTAL_URL = f"https://{PORTAL_HOST}/consulta"
COMERCIO = "Comercio Sintético"
PORTAL_FETCHER = "portal_registry"
ALL_FETCHERS = (PORTAL_FETCHER, "sunat_sol")


def _flow(fetchers: Sequence[str] = ALL_FETCHERS, *, inbox: FlowInbox | None = None) -> Flow:
    return Flow(
        label="Trabajo",
        accepted_tipos=["01"],
        fetchers=list(fetchers),
        required_docs=["pdf", "xml"],
        deliver_without_docs=False,
        email_inbox=inbox,
        sinks=[],
    )


def _inbox(provider: Literal["gmail", "graph"] = "gmail") -> FlowInbox:
    return FlowInbox(provider=provider, account="bot", retry_schedule=["5m", "1h", "6h", "24h"])


def _store(*, portal: str | None = PORTAL_KEY, comercio: str = COMERCIO) -> Store:
    return Store(
        ruc=STORE_RUC,
        razon_social="COMERCIO DE PRUEBA S.A.C.",
        tipo="01",
        brands=[Brand(comercio=comercio)],
        portal=portal,
    )


def _portal(url: str | None = PORTAL_URL) -> Portal:
    return Portal(key=PORTAL_KEY, adapter="generic_form", url=url)


def _directory(store: Store | None = None, portal: Portal | None = None) -> StoreDirectory:
    """Return a directory holding the given store and portal, or empty ones."""
    return StoreDirectory(
        stores={} if store is None else {store.ruc: store},
        portals={} if portal is None else {portal.key: portal},
    )


def _with_store() -> StoreDirectory:
    return _directory(_store(), _portal())


def _reading(ruc_emisor: str = STORE_RUC) -> Reading:
    return Reading(
        fields={
            "ruc_emisor": Field(value=ruc_emisor, source="qr", confidence=1.0),
            "serie": Field(value="F001", source="qr", confidence=1.0),
            "numero": Field(value="4321", source="qr", confidence=1.0),
        },
        document_url=None,
    )


def _bullets(
    reason: FetchReason,
    *,
    flow: Flow | None = None,
    directory: StoreDirectory | None = None,
    taught_portal: bool = False,
) -> list[str]:
    """Return the bullets of the message for one fetcher's final ``reason``."""
    text = manual_message(
        _reading(),
        flow or _flow(),
        [Attempt(PORTAL_FETCHER, reason)],
        StoreFacts(directory or _with_store(), taught_portal),
    )
    return [line for line in text.splitlines() if line.startswith("•")]


@pytest.mark.parametrize(
    ("reason", "bullet"),
    [
        (FetchReason.NOT_FOUND_YET, FETCH_NOT_FOUND_LINE),
        (FetchReason.NOT_FOUND, FETCH_NOT_FOUND_LINE),
        (FetchReason.MISSING_DOCS, FETCH_NOT_FOUND_LINE),
        (FetchReason.NO_URL, None),
        (FetchReason.NO_PORTAL, FETCH_NO_PORTAL_LINE),
        (FetchReason.NO_WEBSITE, MESSAGES["BR-F11"]),
        (FetchReason.PORTAL_INACTIVE, FETCH_PORTAL_TODO_LINE),
        (FetchReason.CAPTCHA_UNSOLVABLE, MESSAGES["BR-F04"]),
        (FetchReason.TECHNICAL, FETCH_TECHNICAL_LINE),
        (FetchReason.TIMEOUT, FETCH_TECHNICAL_LINE),
        (FetchReason.GUARD_BLOCKED, MESSAGES["BR-F08"]),
        (FetchReason.XML_INVALID, MESSAGES["BR-F09"]),
        (FetchReason.XML_MISMATCH, MESSAGES["BR-F06"]),
        (FetchReason.AUTH_REQUIRED, FETCH_AUTH_LINE),
    ],
)
def test_each_reason_renders_its_table_bullet(reason: FetchReason, bullet: str | None) -> None:
    """BR-F07 (S7.4): each final reason renders the bullet the step's table gives it."""
    expected = [] if bullet is None else [bullet.format(**_placeholders())]

    assert _bullets(reason) == expected


def _placeholders() -> dict[str, str]:
    """Return the synthetic values the BR-F templates fill in."""
    return {"fetcher": fetcher_desc(PORTAL_FETCHER), "portal": f"de {COMERCIO}", "comercio": COMERCIO}


def test_br_f01_is_added_when_the_flow_lacks_sunat_sol() -> None:
    """BR-F07 (S7.4): a flow without sunat_sol adds BR-F01's bullet after the fetcher bullets."""
    bullets = _bullets(FetchReason.NOT_FOUND, flow=_flow((PORTAL_FETCHER,)))

    assert bullets[-1] == MESSAGES["BR-F01"]


def test_br_f01_is_absent_when_the_flow_has_sunat_sol() -> None:
    """BR-F07 (S7.4): a flow with sunat_sol does not add BR-F01's bullet."""
    bullets = _bullets(FetchReason.NOT_FOUND)

    assert MESSAGES["BR-F01"] not in bullets


def test_two_attempts_of_one_fetcher_give_one_bullet_from_the_last_reason() -> None:
    """BR-F07 (S7.4): two attempts of one fetcher give one bullet, from its last reason."""
    attempts = [
        Attempt(PORTAL_FETCHER, FetchReason.NOT_FOUND_YET),
        Attempt(PORTAL_FETCHER, FetchReason.XML_INVALID),
    ]

    text = manual_message(_reading(), _flow(), attempts, StoreFacts(_with_store(), taught_portal=False))

    bullets = [line for line in text.splitlines() if line.startswith("•")]
    assert bullets == [MESSAGES["BR-F09"].format(fetcher=fetcher_desc(PORTAL_FETCHER))]


def test_bullet_order_follows_each_fetcher_first_attempt() -> None:
    """BR-F07 (S7.4): bullets follow the order in which each fetcher was first tried."""
    attempts = [
        Attempt("email_inbox", FetchReason.AUTH_REQUIRED),
        Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL),
        Attempt("email_inbox", FetchReason.AUTH_REQUIRED),
    ]

    text = manual_message(_reading(), _flow(), attempts, StoreFacts(_with_store(), taught_portal=False))

    bullets = [line for line in text.splitlines() if line.startswith("•")]
    assert bullets == [FETCH_AUTH_LINE, FETCH_NO_PORTAL_LINE]


@pytest.mark.parametrize(
    ("directory", "portal"),
    [
        (_directory(_store(), _portal()), f"de {COMERCIO}"),
        (_directory(_store(), _portal(url=None)), f"de {COMERCIO}"),
        (_directory(_store(portal=None), None), f"de {COMERCIO}"),
        (_directory(None, None), "del comercio"),
    ],
)
def test_portal_placeholder_names_the_comercio_never_the_host(directory: StoreDirectory, portal: str) -> None:
    """D208, BR-F07: {portal} is "de" and the comercio, else "del comercio"; never the portal's host."""
    bullets = _bullets(FetchReason.PORTAL_INACTIVE, directory=directory)

    assert bullets == [FETCH_PORTAL_TODO_LINE.format(portal=portal)]
    assert PORTAL_HOST not in bullets[0]


def test_docs_needed_text_names_the_docs_the_fetcher_bullets_and_the_mailbox() -> None:
    """D190, BR-F13: the text names the missing docs, the bullets tried, the mailbox and the offer."""
    attempts = [
        Attempt("qr_url", FetchReason.NO_URL),
        Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL),
    ]

    text = docs_needed_text(
        7,
        _reading(),
        _flow(inbox=_inbox()),
        attempts,
        StoreFacts(_with_store(), taught_portal=False),
        LookTimes(datetime(2026, 1, 2, 14, 5, tzinfo=UTC), date(2026, 1, 1)),
    )

    assert text == (
        "⏳ Comprobante #7: no pude descargar el PDF y el XML.\n"
        "Ya busqué:\n"
        "• Portal del comercio: no lo conozco\n"
        "Mientras tanto, reviso tu Gmail hasta mañana a las 14:05 por si el comercio te lo envió; "
        "si llega, lo archivo solo y te aviso.\n"
        "¿Cómo seguimos?"
    )


def test_docs_needed_text_drops_the_search_line_without_a_bullet() -> None:
    """D190, BR-F13: only a qr_url with no link leaves no "Ya busqué:" line and no blank line."""
    text = docs_needed_text(
        7,
        _reading(),
        _flow(inbox=_inbox()),
        [Attempt("qr_url", FetchReason.NO_URL)],
        StoreFacts(_with_store(), taught_portal=False),
        LookTimes(datetime(2026, 1, 2, 14, 5, tzinfo=UTC), date(2026, 1, 1)),
    )

    assert "Ya busqué:" not in text
    assert "\n\n" not in text
    assert text == (
        "⏳ Comprobante #7: no pude descargar el PDF y el XML.\n"
        "Mientras tanto, reviso tu Gmail hasta mañana a las 14:05 por si el comercio te lo envió; "
        "si llega, lo archivo solo y te aviso.\n"
        "¿Cómo seguimos?"
    )


@pytest.mark.parametrize(
    ("last_at", "today", "last_desc"),
    [
        (datetime(2026, 11, 24, 14, 5, tzinfo=UTC), date(2026, 11, 24), "hoy a las 14:05"),
        (datetime(2026, 11, 25, 12, 20, tzinfo=UTC), date(2026, 11, 24), "mañana a las 12:20"),
        (datetime(2026, 11, 26, 9, 0, tzinfo=UTC), date(2026, 11, 24), "el 26/11 a las 09:00"),
    ],
)
def test_docs_needed_text_words_the_last_look_against_the_owner_date(
    last_at: datetime, today: date, last_desc: str
) -> None:
    """D190, BR-F13: the last look reads hoy, mañana or a date, and the offer closes the text."""
    text = docs_needed_text(
        7,
        _reading(),
        _flow(inbox=_inbox()),
        [Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL)],
        StoreFacts(_with_store(), taught_portal=False),
        LookTimes(last_at, today),
    )

    assert text.startswith("⏳ Comprobante #7: no pude descargar el PDF y el XML.")
    assert "Próxima revisión" not in text
    assert f"Mientras tanto, reviso tu Gmail hasta {last_desc} por si el comercio te lo envió" in text
    assert text.splitlines()[-1] == "¿Cómo seguimos?"


def test_docs_needed_text_names_outlook_for_a_graph_inbox() -> None:
    """D190, BR-F13: a graph inbox reads "tu Outlook"."""
    text = docs_needed_text(
        7,
        _reading(),
        _flow(inbox=_inbox("graph")),
        [Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL)],
        StoreFacts(_with_store(), taught_portal=False),
        LookTimes(
            datetime(2026, 11, 25, 12, 20, tzinfo=UTC),
            date(2026, 11, 24),
        ),
    )

    assert "Mientras tanto, reviso tu Outlook hasta mañana a las 12:20" in text


def test_docs_needed_text_needs_a_flow_with_an_inbox() -> None:
    """D190, BR-F13: a flow without email_inbox raises ValueError."""
    with pytest.raises(ValueError, match="BR-F13 needs a flow with an email_inbox"):
        docs_needed_text(
            7,
            _reading(),
            _flow(),
            [Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL)],
            StoreFacts(_with_store(), taught_portal=False),
            LookTimes(
                datetime(2026, 11, 25, 12, 20, tzinfo=UTC),
                date(2026, 11, 24),
            ),
        )


def test_br_f11_uses_a_generic_comercio_without_a_store() -> None:
    """BR-F07 (S7.4): BR-F11 names the store's first brand, or a generic comercio without a store."""
    assert _bullets(FetchReason.NO_WEBSITE, directory=_directory(None, None)) == [
        MESSAGES["BR-F11"].format(comercio="el comercio")
    ]


def test_a_taught_portal_changes_the_no_portal_bullet_in_br_f07() -> None:
    """D208, BR-F07: NO_PORTAL with an approved taught link says the portal was taught."""
    assert _bullets(FetchReason.NO_PORTAL, taught_portal=True) == [FETCH_TAUGHT_PORTAL_LINE]


def test_a_taught_portal_changes_the_no_portal_bullet_in_br_f13() -> None:
    """D208, BR-F13: NO_PORTAL with an approved taught link says the portal was taught."""
    text = docs_needed_text(
        7,
        _reading(),
        _flow(inbox=_inbox()),
        [Attempt(PORTAL_FETCHER, FetchReason.NO_PORTAL)],
        StoreFacts(_with_store(), taught_portal=True),
        LookTimes(datetime(2026, 1, 2, 14, 5, tzinfo=UTC), date(2026, 1, 1)),
    )

    assert FETCH_TAUGHT_PORTAL_LINE in text.splitlines()
    assert FETCH_NO_PORTAL_LINE not in text


def test_no_taught_portal_keeps_the_fixed_no_portal_bullet() -> None:
    """D208, BR-F07: NO_PORTAL without a taught link still says the portal is unknown."""
    assert _bullets(FetchReason.NO_PORTAL, taught_portal=False) == [FETCH_NO_PORTAL_LINE]
