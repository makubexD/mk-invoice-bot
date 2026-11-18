"""Tests for the config digest a gap stores (D157)."""

from pydantic import BaseModel

from invoicebot.config.digest import config_digest


class _Entry(BaseModel):
    """A small config entry with a dict field, so key order can be exercised."""

    name: str
    limits: dict[str, int]


def test_config_digest_is_stable_for_equal_models() -> None:
    """D157: two models with equal values produce the same digest."""
    first = _Entry(name="store", limits={"max_docs": 2, "max_bytes": 10})
    second = _Entry(name="store", limits={"max_docs": 2, "max_bytes": 10})

    assert config_digest(first) == config_digest(second)


def test_config_digest_differs_when_one_field_changes() -> None:
    """D157: changing one field changes the digest."""
    first = _Entry(name="store", limits={"max_docs": 2})
    second = _Entry(name="store", limits={"max_docs": 3})

    assert config_digest(first) != config_digest(second)


def test_config_digest_ignores_dict_key_order() -> None:
    """D157: the order of a dict field's keys does not change the digest."""
    first = _Entry(name="store", limits={"max_docs": 2, "max_bytes": 10})
    second = _Entry(name="store", limits={"max_bytes": 10, "max_docs": 2})

    assert config_digest(first) == config_digest(second)
