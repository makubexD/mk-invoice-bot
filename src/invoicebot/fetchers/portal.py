"""The ``portal_registry`` fetcher: run the browser helper once per fetch (S10.2, D82).

The fetcher finds the issuer's portal in the store directory and runs the browser helper
in its own process and its own temporary folder for one fetch (R-9.2). The helper downloads
the files; this module checks that every file it reports lies inside the output directory,
under its kind's byte limit and matches the bot's own SHA-256, then reads the XML and
matches it against the invoice (R-5.6, BR-F06, BR-F09). A failed fetch removes the
downloaded files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from invoicebot.config.checks import MANUAL_PORTAL
from invoicebot.config.models import MediaMaxBytes, Portal, PortalCaptcha, StoreDirectory
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.extract.ubl import XmlRejected, matches_invoice, parse_invoice_xml
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.probe_protocol import ProbeReport
from invoicebot.fetchers.browser.protocol import (
    CaptchaChallenge,
    CaptchaForm,
    CaptchaResume,
    HelperFile,
    HelperOutput,
    HelperRequest,
)
from invoicebot.helper import run_helper
from invoicebot.sandbox import check_no_parent_dir

logger = logging.getLogger(__name__)

#: The browser helper module; ``-P`` keeps the working directory off its import path.
DEFAULT_HELPER_COMMAND = (sys.executable, "-P", "-m", "invoicebot.fetchers.browser")
# Per-action Playwright timeout; the whole run is bounded by helper_timeout_s.
PAGE_TIMEOUT_MS = 15_000
#: The helper prints one small JSON object; anything larger is a bug the queue retries (R-9.2).
MAX_HELPER_OUTPUT_BYTES = 64 * 1024
#: The chunk size used to hash a downloaded file without reading it whole (R-5.6).
HASH_CHUNK_BYTES = 64 * 1024


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
        return await self._fetch(invoice, out_dir, None)

    async def resume(
        self, invoice: FetchInvoice, form: CaptchaForm, answer: str, out_dir: Path
    ) -> FetchResult:
        """Send the sender's captcha ``answer`` for ``form`` and report why the fetch ended (D209)."""
        return await self._fetch(invoice, out_dir, CaptchaResume(form=form, answer=answer))

    async def _fetch(self, invoice: FetchInvoice, out_dir: Path, resume: CaptchaResume | None) -> FetchResult:
        """Run the helper for ``invoice``, check its output; ``resume`` continues a stopped fetch (D209)."""
        portal = self._portal_for(invoice.ruc_emisor)
        if isinstance(portal, FetchResult):
            return portal
        if resume is not None and not _https_on_host_of(resume.form.action_url, portal):
            return FetchResult(FetchReason.TECHNICAL, (), portal.key)
        output = await self._run(portal, invoice, out_dir, resume)
        if isinstance(output, FetchResult):
            return output
        if output.captcha is not None:
            return _asked_result(output.captcha, out_dir, self._max_bytes, portal)
        return result_from(output, out_dir, invoice, self._max_bytes, portal.key)

    def _portal_for(self, ruc: str) -> Portal | FetchResult:
        """Return the store's usable portal, or why the fetch cannot run (R-5.8, S10.10, D99)."""
        store = self._directory.stores.get(ruc)
        if store is None or store.portal is None:
            return FetchResult(FetchReason.NO_PORTAL, (), None)
        if store.portal == MANUAL_PORTAL:
            return FetchResult(FetchReason.NO_WEBSITE, (), None)
        portal = self._directory.portals.get(store.portal)
        if portal is None:
            return FetchResult(FetchReason.NO_PORTAL, (), None)
        if portal.captcha != "none" and not isinstance(portal.captcha, PortalCaptcha):
            # shortcut: only a captcha the sender types is handled (D209); others stay unsolvable (D99).
            return FetchResult(FetchReason.CAPTCHA_UNSOLVABLE, (), portal.key)
        if portal.status != "active":
            return FetchResult(FetchReason.PORTAL_INACTIVE, (), portal.key)
        return portal

    async def _run(
        self,
        portal: Portal,
        invoice: FetchInvoice,
        out_dir: Path,
        resume: CaptchaResume | None,
    ) -> HelperOutput | FetchResult:
        """Run the browser helper in its own temp folder and validate its one JSON line (S10.2, R-9.2)."""
        request = HelperRequest(
            portal=portal,
            invoice=invoice,
            out_dir=out_dir,
            max_bytes=self._max_bytes,
            page_timeout_ms=PAGE_TIMEOUT_MS,
            resume=resume,
        )
        job = BrowserJob(
            command=self._helper_command,
            payload=request.model_dump_json().encode(),
            timeout_s=self._helper_timeout_s,
            name=portal.key,
            out_dir=out_dir,
            model=HelperOutput,
        )
        return await run_browser_job(job)


def _https_on_host_of(url: str, portal: Portal) -> bool:
    """Return whether ``url`` is https on the host of the portal's own URL (D209)."""
    if portal.url is None:
        return False
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.hostname == urlsplit(portal.url).hostname
    )


def _asked_result(
    challenge: CaptchaChallenge, out_dir: Path, max_bytes: MediaMaxBytes, portal: Portal
) -> FetchResult:
    """Return ``CAPTCHA_ASKED`` with the challenge when the bot confirms it, else ``TECHNICAL`` (D209).

    The PNG is checked as any downloaded file is (inside ``out_dir``, own size and SHA-256),
    and both form URLs must be https on the portal's host. A failed check removes the PNG.
    """
    # shortcut: the PNG is checked as a "pdf" file, the one kind whose limit is an upper bound here.
    root = out_dir.resolve()
    image = HelperFile(kind="pdf", path=challenge.path, size=challenge.size, sha256=challenge.sha256)
    valid = check_files((image,), root, max_bytes, {}) is FetchReason.OK
    valid = valid and challenge.path.stat().st_size == challenge.size
    valid = valid and all(
        _https_on_host_of(url, portal) for url in (challenge.form.action_url, challenge.form.page_url)
    )
    if not valid:
        delete_inside((image,), root)
        return FetchResult(FetchReason.TECHNICAL, (), portal.key)
    return FetchResult(FetchReason.CAPTCHA_ASKED, (), portal.key, challenge)


@dataclass(frozen=True)
class BrowserJob[T: (HelperOutput, ProbeReport)]:
    """One browser-helper run: what to start, what to send and the model its line must fit."""

    command: Sequence[str]
    payload: bytes
    timeout_s: float
    #: Becomes the ``portal_key`` of a failure result and the ``fetcher`` of a log line.
    name: str
    out_dir: Path
    model: type[T]


async def run_browser_job[T: (HelperOutput, ProbeReport)](job: BrowserJob[T]) -> T | FetchResult:
    """Run one browser-helper job in its own temp folder and validate its one JSON line.

    The helper runs with a minimal environment whose temp files stay in a scratch folder
    removed after the run, and its output is capped at ``MAX_HELPER_OUTPUT_BYTES`` (R-9.2).
    The fetch folder reaches the sandbox wrapper as ``HELPER_OUT_DIR`` (D150).
    ``job.name`` becomes the ``portal_key`` of a failure result, so each fetcher reports
    itself (R-5.7). A helper that reports an ``error_class``, and one that exits non-zero,
    log a warning naming only the error class and our own message, never page text (D169).

    Returns:
        The helper's parsed ``job.model`` output, or the ``FetchResult`` saying why the job failed:
        ``TIMEOUT`` when it outlives ``timeout_s``, ``TECHNICAL`` when the helper fails or
        prints something that is not a valid ``job.model``.

    Raises:
        ValueError: when ``out_dir`` has a ``..`` component, which would let it leave the
            data root the sandbox wrapper masks (D150).
        asyncio.CancelledError: when the caller is cancelled; the helper's process group is
            killed and reaped first (R-9.2).
    """
    command, out_dir, fetcher_name = job.command, job.out_dir, job.name
    check_no_parent_dir(out_dir, "out_dir")
    try:
        with tempfile.TemporaryDirectory(prefix="invoicebot-browser-") as scratch:
            stdout = await run_helper(
                command,
                job.payload,
                helper_env(Path(scratch)) | {"HELPER_OUT_DIR": str(out_dir)},
                job.timeout_s,
                MAX_HELPER_OUTPUT_BYTES,
            )
    except TimeoutError:
        logger.warning("portal helper timed out", extra={"fetcher": fetcher_name})
        return FetchResult(FetchReason.TIMEOUT, (), fetcher_name)
    except RuntimeError as err:
        logger.warning(
            "portal helper failed",
            # Our own message, no page text (D169).
            extra={"fetcher": fetcher_name, "error": type(err).__name__, "detail": str(err)},
        )
        return FetchResult(FetchReason.TECHNICAL, (), fetcher_name)
    try:
        output = job.model.model_validate_json(stdout)
    except ValidationError as err:
        logger.warning(
            "portal helper output invalid",
            extra={"fetcher": fetcher_name, "error": type(err).__name__},
        )
        return FetchResult(FetchReason.TECHNICAL, (), fetcher_name)
    if output.error_class is not None:
        logger.warning(
            "portal helper failed inside",
            extra={"fetcher": fetcher_name, "error": output.error_class},
        )
    return output


def helper_env(tmp_dir: Path) -> dict[str, str]:
    """Return the helper's minimal environment, with its temp files confined to ``tmp_dir``.

    ``tmp_dir`` is the caller's scratch folder, removed after the run, so a killed helper
    leaves no download artifacts or browser profile behind (R-9.2).
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_dir)}
    for name in ("HOME", "PLAYWRIGHT_BROWSERS_PATH"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def result_from(
    output: HelperOutput,
    out_dir: Path,
    invoice: FetchInvoice,
    max_bytes: MediaMaxBytes,
    key: str,
) -> FetchResult:
    """Check the helper's files and turn them into the fetch result (R-5.6)."""
    root = out_dir.resolve()
    digests: dict[Path, str] = {}
    reason = output.reason
    if reason is FetchReason.OK:
        reason = _validate_files(output.files, root, invoice, max_bytes, digests)
    if reason is not FetchReason.OK:
        delete_inside(output.files, root)
        return FetchResult(reason, (), key)
    files = tuple(
        FetchedFile(file.kind, file.path, file.path.stat().st_size, digests[file.path])
        for file in output.files
    )
    return FetchResult(FetchReason.OK, files, key)


def _validate_files(
    files: Sequence[HelperFile],
    root: Path,
    invoice: FetchInvoice,
    max_bytes: MediaMaxBytes,
    digests: dict[Path, str],
) -> FetchReason:
    """Return why the helper's files are unusable, or ``OK`` when they carry a matching XML (R-5.6)."""
    reason = check_files(files, root, max_bytes, digests)
    if reason is not FetchReason.OK:
        return reason
    xml = next((file for file in files if file.kind == "xml"), None)
    if xml is None:
        return FetchReason.MISSING_DOCS
    return xml_reason(xml.path, invoice) or FetchReason.OK


def check_files(
    files: Sequence[HelperFile],
    root: Path,
    max_bytes: MediaMaxBytes,
    digests: dict[Path, str],
) -> FetchReason:
    """Return ``OK`` when every file lies inside ``root``, fits its limit and matches its hash.

    ``digests`` receives each file's bot-computed hash, so the caller builds the result
    without reading the files a second time (R-5.6).
    """
    if not all(_inside(file.path, root) for file in files):
        return FetchReason.TECHNICAL
    for file in files:
        reason = _too_large(file, max_bytes)
        if reason is not None:
            return reason
    for file in files:
        try:
            digest = sha256_hex(file.path)
        except OSError:
            return FetchReason.TECHNICAL
        if digest != file.sha256:
            return FetchReason.TECHNICAL
        digests[file.path] = digest
    return FetchReason.OK


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


def sha256_hex(path: Path) -> str:
    """Return the SHA-256 hex digest of the file at ``path``, read in 64 KiB chunks (R-5.6).

    Raises:
        OSError: when the file cannot be opened or read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def xml_reason(xml: Path, invoice: FetchInvoice) -> FetchReason | None:
    """Return the XML's failure reason, or ``None`` when it matches the invoice (R-5.6, BR-F09)."""
    try:
        data = xml.read_bytes()
    except OSError:
        return FetchReason.TECHNICAL
    try:
        reading = parse_invoice_xml(data)
    except XmlRejected:
        return FetchReason.XML_INVALID
    if not matches_invoice(reading, invoice_reading(invoice)):
        return FetchReason.XML_MISMATCH
    return None


def invoice_reading(invoice: FetchInvoice) -> Reading:
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


def delete_inside(files: Sequence[HelperFile], root: Path) -> None:
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
