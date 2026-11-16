"""Tests for comercio selection and folder sequence numbers (R-7.1, R-7.11)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from invoicebot.config.models import Brand, Store
from invoicebot.sinks.expr import ExprError
from invoicebot.sinks.folders import (
    amount_label,
    comercio_for,
    folder_parent,
    max_seq,
    seq_pattern,
    status_name,
)

_JULY_FOLDERS = [
    "2026-7-Support_3_KFC",
    "2026-07-Support_5_X_Sent",
    "2026-07-Support_2_Y_PAID_15_12___144_6",
    "2026-08-Support_9_Z",
    "notes.txt",
]
_FOLDER_TEMPLATE = "{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}"


def _store(*brands: Brand) -> Store:
    return Store(
        ruc="20000000001",
        razon_social="DELOSI S.A.",
        tipo="restaurante",
        brands=list(brands),
        portal=None,
    )


def test_comercio_for_serie_prefix_picks_its_brand() -> None:
    """R-7.1: a brand whose serie_prefix prefixes the serie is the comercio."""
    store = _store(
        Brand(comercio="KFC", serie_prefix=["F0"]),
        Brand(comercio="Pizza", serie_prefix=["F1"]),
    )

    assert comercio_for(store, "F001", "DELOSI S.A.") == "KFC"


def test_comercio_for_only_brand_without_prefix_returns_it() -> None:
    """R-7.1: a store with a single brand falls back to that brand."""
    store = _store(Brand(comercio="LaLena"))

    assert comercio_for(store, "X001", "DELOSI S.A.") == "LaLena"


def test_comercio_for_two_brands_without_match_returns_none() -> None:
    """R-7.1: two brands and no serie_prefix match leave the comercio unknown."""
    store = _store(
        Brand(comercio="KFC", serie_prefix=["F0"]),
        Brand(comercio="Pizza", serie_prefix=["F1"]),
    )

    assert comercio_for(store, "X001", "DELOSI S.A.") is None


def test_comercio_for_no_store_slugs_razon_social() -> None:
    """R-7.1: without a store the first two words become an ASCII CamelCase slug."""
    assert comercio_for(None, "F001", "Restaurante La Leña S.A.C.") == "RestauranteLa"


def test_comercio_for_no_store_without_words_returns_none() -> None:
    """R-7.1: a legal name with no ASCII words has no comercio."""
    assert comercio_for(None, "F001", "---") is None


def test_max_seq_reads_july_folders() -> None:
    """R-7.1, R-7.11: the largest July number wins; status suffixes and other files are ignored."""
    pattern = seq_pattern(_FOLDER_TEMPLATE, 2026, 7)

    assert max_seq(_JULY_FOLDERS, pattern) == 5


def test_max_seq_month_without_folders_returns_zero() -> None:
    """R-7.1: a month with no matching folder starts at zero."""
    pattern = seq_pattern(_FOLDER_TEMPLATE, 2026, 9)

    assert max_seq(_JULY_FOLDERS, pattern) == 0


def test_seq_pattern_does_not_match_a_longer_month() -> None:
    """R-7.1: 071 is not July, so its folder is ignored."""
    pattern = seq_pattern(_FOLDER_TEMPLATE, 2026, 7)

    assert max_seq(["2026-071-Support_9_X"], pattern) == 0


def test_seq_pattern_without_seq_raises() -> None:
    """R-7.1: a folder template that never names {seq} cannot be read back."""
    with pytest.raises(ExprError):
        seq_pattern("{fecha:%Y}-{fecha:%m}-Support_{comercio}", 2026, 7)


@pytest.mark.parametrize(
    ("amount", "label"),
    [
        (Decimal("144.60"), "144_6"),
        (Decimal("42.00"), "42"),
        (Decimal("143.90"), "143_9"),
        (Decimal("0.05"), "0_05"),
        (Decimal("100.00"), "100"),
    ],
)
def test_amount_label_drops_trailing_zeros(amount: Decimal, label: str) -> None:
    """R-7.11: the amount keeps its digits and uses `_` for the point."""
    assert amount_label(amount) == label


def test_status_name_adds_sent_then_paid_into_the_year_folder() -> None:
    """R-7.11: a sent invoice gains `_Sent`, a payment adds `_PAID_15_12___144_6` and the year folder."""
    base = _FOLDER_TEMPLATE.format(fecha=date(2026, 11, 15), seq=2, comercio="LaLena")
    sent = status_name(base, sent=True, paid_on=None, payment_total=None)

    assert base == "2026-11-Support_2_LaLena"
    assert sent == "2026-11-Support_2_LaLena_Sent"
    assert (
        status_name(base, sent=True, paid_on=date(2026, 12, 15), payment_total=Decimal("144.60"))
        == "2026-11-Support_2_LaLena_Sent_PAID_15_12___144_6"
    )
    assert folder_parent("/Reembolsos", 2026, paid=True) == "/Reembolsos/2026"
    assert folder_parent("/Reembolsos", 2026, paid=False) == "/Reembolsos"


def test_status_name_paid_without_sent_has_no_sent_suffix() -> None:
    """R-7.11: a payment recorded before the send gives the paid name without `_Sent`."""
    assert (
        status_name(
            "2026-11-Support_2_LaLena",
            sent=False,
            paid_on=date(2026, 12, 15),
            payment_total=Decimal("144.60"),
        )
        == "2026-11-Support_2_LaLena_PAID_15_12___144_6"
    )


def test_status_name_paid_without_amount_raises() -> None:
    """R-7.11: a paid status needs its payment total."""
    with pytest.raises(ValueError, match="payment_total"):
        status_name(
            "2026-11-Support_2_LaLena",
            sent=True,
            paid_on=date(2026, 12, 15),
            payment_total=None,
        )


@pytest.mark.parametrize(
    ("sent", "paid_on", "payment_total"),
    [
        (False, None, None),
        (True, None, None),
        (True, date(2026, 12, 15), Decimal("144.60")),
        (False, date(2026, 12, 15), Decimal("144.60")),
    ],
)
def test_seq_pattern_reads_each_status_name(
    sent: bool, paid_on: date | None, payment_total: Decimal | None
) -> None:
    """R-7.1: the base, `_Sent` and `_PAID_` names of the month all read back their seq."""
    pattern = seq_pattern(_FOLDER_TEMPLATE, 2026, 11)
    base = _FOLDER_TEMPLATE.format(fecha=date(2026, 11, 15), seq=2, comercio="LaLena")

    assert max_seq([status_name(base, sent, paid_on, payment_total)], pattern) == 2
