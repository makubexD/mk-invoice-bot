"""The weekly padrón refresh: download SUNAT's ZIP and import it (S12.4, D159).

The download is capped by ``padron.max_mb`` and the ZIP is deleted whatever the
outcome, so a failed refresh leaves neither a partial file nor a stale one. A
non-200 answer and an oversized body raise :class:`PadronRefreshError` whose
``reason`` says which; the daily loop logs it and the owner retries by hand.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import httpx

from invoicebot.config.models import Padron
from invoicebot.padron import ImportResult, import_padron
from invoicebot.store.db import Pool

logger = logging.getLogger(__name__)

#: The folder under ``data_dir`` the padrón ZIP is downloaded to (D159).
PADRON_DIRECTORY = "padron"

#: The name SUNAT's padrón ZIP is saved under (D159).
ZIP_NAME = "padron_reducido_ruc.zip"

#: The stage that failed; the daily loop logs it as the warning's reason.
PadronReason = Literal["status", "size"]


class PadronRefreshError(Exception):
    """The padrón download failed; ``reason`` says whether the status or the size did."""

    def __init__(self, reason: PadronReason) -> None:
        super().__init__(f"padron refresh failed: {reason}")
        self.reason = reason


async def refresh_padron(
    pool: Pool, http: httpx.AsyncClient, padron_cfg: Padron, data_dir: Path
) -> ImportResult:
    """Download ``padron_cfg.url`` and import it, deleting the ZIP whatever happens (S12.4, D159).

    The ZIP streams to ``<data_dir>/padron/padron_reducido_ruc.zip`` and is
    removed in ``finally``. The import runs on an autocommit connection, as the
    script does, because ``import_padron`` commits its COPY batches itself. Logs
    one INFO line with the imported and skipped row counts.
    """
    zip_path = data_dir / PADRON_DIRECTORY / ZIP_NAME
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _download(http, str(padron_cfg.url), zip_path, padron_cfg.max_mb)
        result = await _import(pool, zip_path)
    finally:
        zip_path.unlink(missing_ok=True)
    logger.info("padron refreshed", extra={"rows": result.rows, "skipped": result.skipped})
    return result


async def _download(http: httpx.AsyncClient, url: str, zip_path: Path, max_mb: int) -> None:
    """Stream ``url`` into ``zip_path``, refusing a non-200 answer or more than ``max_mb`` (D159)."""
    max_bytes = max_mb * 1024 * 1024
    async with http.stream("GET", url) as response:
        if response.status_code != httpx.codes.OK:
            raise PadronRefreshError("status")
        written = 0
        with zip_path.open("wb") as zip_file:
            async for chunk in response.aiter_bytes():
                written += len(chunk)
                if written > max_bytes:
                    raise PadronRefreshError("size")
                zip_file.write(chunk)


async def _import(pool: Pool, zip_path: Path) -> ImportResult:
    """Run ``import_padron`` in autocommit, as ``scripts/import_padron.py`` does (S12.4)."""
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            return await import_padron(conn, zip_path)
        finally:
            await conn.set_autocommit(False)
