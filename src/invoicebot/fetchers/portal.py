"""The ``portal_registry`` fetcher: run the browser helper once per fetch (S10.2, D82).

The fetcher finds the issuer's portal in the store directory and runs the browser helper
in its own process for one fetch. The helper downloads the files; this module checks that
every file it reports lies inside the output directory and under its kind's byte limit, then
reads the XML and matches it against the invoice (R-5.6, BR-F06, BR-F09). A failed fetch
removes the downloaded files.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from invoicebot.config.checks import MANUAL_PORTAL
from invoicebot.config.models import MediaMaxBytes, Portal, StoreDirectory
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.extract.ubl import XmlRejected, matches_invoice, parse_invoice_xml
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.protocol import HelperFile, HelperOutput, HelperRequest
from invoicebot.helper import run_helper

#: The browser helper module; ``-P`` keeps the working directory off its import path.
DEFAULT_HELPER_COMMAND = (sys.executable, "-P", "-m", "invoicebot.fetchers.browser")
# Per-action Playwright timeout; the whole run is bounded by helper_timeout_s.
PAGE_TIMEOUT_MS = 15_000
#: The helper prints one small JSON object; anything larger is a bug the queue retries (R-9.2).
MAX_HELPER_OUTPUT_BYTES = 64 * 1024


class PortalFetcher:
    """The ``portal_registry`` fetcher: one browser-helper run per fetch (S10.2, D82)."""

    def __init__(
        self,
        directory: StoreDirectory,
        max_bytes: MediaMaxBytes,
        helper_timeout_s: float,
        *,
        helper_command: Sequence[str] = DEFAULT_HELPER_COMMAND,
    ) -> None:
        self._directory = directory
        self._max_bytes = max_bytes
        self._helper_timeout_s = helper_timeout_s
        self._helper_command = helper_command

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice`` from its store's portal and report why (R-5.6, S10.2)."""
        portal = self._portal_for(invoice.ruc_emisor)
        if isinstance(portal, FetchResult):
            return portal
        output = await self._run(portal, invoice, out_dir)
        if isinstance(output, FetchResult):
            return output
        return _result_from(output, out_dir, invoice, self._max_bytes, portal.key)

    def _portal_for(self, ruc: str) -> Portal | FetchResult:
        """Return the store's active portal, or the reason there is none (R-5.8)."""
        store = self._directory.stores.get(ruc)
        if store is None or store.portal in (None, MANUAL_PORTAL):
            return FetchResult(FetchReason.NO_PORTAL, (), None)
        portal = self._directory.portals.get(store.portal)
        if portal is None:
            return FetchResult(FetchReason.NO_PORTAL, (), None)
        if portal.status != "active":
            return FetchResult(FetchReason.PORTAL_INACTIVE, (), portal.key)
        return portal

    async def _run(self, portal: Portal, invoice: FetchInvoice, out_dir: Path) -> HelperOutput | FetchResult:
        """Run the browser helper and validate its one JSON line (S10.2, R-9.2)."""
        request = HelperRequest(
            portal=portal,
            invoice=invoice,
            out_dir=out_dir,
            max_bytes=self._max_bytes,
            page_timeout_ms=PAGE_TIMEOUT_MS,
        )
        try:
            stdout = await run_helper(
                self._helper_command,
                request.model_dump_json().encode(),
                _helper_env(),
                self._helper_timeout_s,
                MAX_HELPER_OUTPUT_BYTES,
            )
        except TimeoutError:
            return FetchResult(FetchReason.TIMEOUT, (), portal.key)
        except RuntimeError:
            return FetchResult(FetchReason.TECHNICAL, (), portal.key)
        try:
            return HelperOutput.model_validate_json(stdout)
        except ValidationError:
            return FetchResult(FetchReason.TECHNICAL, (), portal.key)


def _helper_env() -> dict[str, str]:
    """Return the helper's minimal environment: PATH and the variables Chromium needs."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for name in ("HOME", "PLAYWRIGHT_BROWSERS_PATH"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _result_from(
    output: HelperOutput,
    out_dir: Path,
    invoice: FetchInvoice,
    max_bytes: MediaMaxBytes,
    key: str,
) -> FetchResult:
    """Check the helper's files and turn them into the fetch result (R-5.6)."""
    root = out_dir.resolve()
    reason = output.reason
    if reason is FetchReason.OK:
        reason = _validate_files(output.files, root, invoice, max_bytes)
    if reason is not FetchReason.OK:
        _delete_inside(output.files, root)
        return FetchResult(reason, (), key)
    files = tuple(
        FetchedFile(file.kind, file.path, file.path.stat().st_size, file.sha256) for file in output.files
    )
    return FetchResult(FetchReason.OK, files, key)


def _validate_files(
    files: Sequence[HelperFile],
    root: Path,
    invoice: FetchInvoice,
    max_bytes: MediaMaxBytes,
) -> FetchReason:
    """Return why the helper's files are unusable, or ``OK`` when they carry a matching XML."""
    if not all(_inside(file.path, root) for file in files):
        return FetchReason.TECHNICAL
    for file in files:
        reason = _too_large(file, max_bytes)
        if reason is not None:
            return reason
    xml = next((file for file in files if file.kind == "xml"), None)
    if xml is None:
        return FetchReason.MISSING_DOCS
    reason = _xml_reason(xml, invoice)
    return FetchReason.OK if reason is None else reason


def _too_large(file: HelperFile, max_bytes: MediaMaxBytes) -> FetchReason | None:
    """Return the reason for a file over its kind's limit, or ``None`` (R-5.6).

    The size checked is the bot's own stat of the file on disk, never the helper's report.
    """
    try:
        size = file.path.stat().st_size
    except OSError:
        return FetchReason.TECHNICAL
    if size <= getattr(max_bytes, file.kind):
        return None
    return FetchReason.TECHNICAL if file.kind == "pdf" else FetchReason.XML_INVALID


def _xml_reason(xml: HelperFile, invoice: FetchInvoice) -> FetchReason | None:
    """Return the XML's failure reason, or ``None`` when it matches the invoice (R-5.6, BR-F09)."""
    try:
        data = xml.path.read_bytes()
    except OSError:
        return FetchReason.TECHNICAL
    try:
        reading = parse_invoice_xml(data)
    except XmlRejected:
        return FetchReason.XML_INVALID
    if not matches_invoice(reading, _invoice_reading(invoice)):
        return FetchReason.XML_MISMATCH
    return None


def _invoice_reading(invoice: FetchInvoice) -> Reading:
    """Return the stored invoice as a reading the match can compare (R-5.6).

    The stored values came from the photo reading, so they are marked ``ocr``; the source
    does not affect the match.
    """
    values = {
        "ruc_emisor": invoice.ruc_emisor,
        "serie": invoice.serie,
        "numero": invoice.numero,
        "total": str(invoice.total),
    }
    fields = {
        name: Field(value=value, source="ocr", confidence=FULL_CONFIDENCE) for name, value in values.items()
    }
    return Reading(fields=fields, document_url=None)


def _inside(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside ``root`` (R-5.6)."""
    resolved = _resolved(path)
    return resolved is not None and resolved.is_relative_to(root)


def _delete_inside(files: Sequence[HelperFile], root: Path) -> None:
    """Remove every listed file that resolves inside ``root`` (R-5.6)."""
    for file in files:
        resolved = _resolved(file.path)
        if resolved is not None and resolved.is_relative_to(root):
            resolved.unlink(missing_ok=True)


def _resolved(path: Path) -> Path | None:
    """Return ``path`` with its symlinks resolved, or ``None`` when that fails."""
    try:
        return path.resolve()
    except OSError:
        return None
