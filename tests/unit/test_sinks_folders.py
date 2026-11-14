"""Tests for comercio selection and folder sequence numbers (R-7.1, R-7.11)."""

from __future__ import annotations

import pytest

from invoicebot.config.models import Brand, Store
from invoicebot.sinks.expr import ExprError
from invoicebot.sinks.folders import comercio_for, max_seq, seq_pattern

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
