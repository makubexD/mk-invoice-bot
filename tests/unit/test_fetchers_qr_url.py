"""Tests for the QR document URL allowlist (R-5.2, R-5.10)."""

from __future__ import annotations

import pytest

from invoicebot.config.models import Portal, StoreDirectory
from invoicebot.fetchers.qr_url import allowed_hosts, is_allowed


def _portal(key: str, url: str | None) -> Portal:
    return Portal.model_validate({"key": key, "adapter": "generic_form", "url": url})


def _directory(*portals: Portal) -> StoreDirectory:
    return StoreDirectory(stores={}, portals={portal.key: portal for portal in portals})


@pytest.mark.parametrize(
    ("url", "hosts", "expected"),
    [
        ("https://e-factura.sunat.gob.pe/x", frozenset(), True),
        ("https://sunat.gob.pe.evil.test/", frozenset(), False),
        ("https://evilsunat.gob.pe/", frozenset(), False),
        ("https://portal.example/consulta", frozenset({"portal.example"}), True),
        ("http://portal.example/consulta", frozenset({"portal.example"}), False),
        ("https://user@e-factura.sunat.gob.pe/x", frozenset(), False),
        ("https://[::1", frozenset(), False),
    ],
)
def test_is_allowed_url_decides_by_scheme_and_host(url: str, hosts: frozenset[str], expected: bool) -> None:
    """R-5.2, R-5.10: only https URLs on SUNAT or an allowed host pass."""
    assert is_allowed(url, hosts) is expected


def test_allowed_hosts_lowercases_and_skips_portals_without_url() -> None:
    """R-5.2, R-5.10: every portal URL contributes its lowercased host, if any."""
    directory = _directory(
        _portal("upper", "HTTPS://Portal.Example/consulta"),
        _portal("missing", None),
    )

    assert allowed_hosts(directory) == frozenset({"portal.example"})
