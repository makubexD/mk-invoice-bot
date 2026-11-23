"""Read the go-live import's claim folders and register them (R-7.16).

One job: turn a reimbursement folder's name and its single XML into the claim the import
registers, or into the reason it is skipped, then store each claim as a DONE flow B
invoice with a sent batch. A claim whose natural key already exists is skipped, so a
second run changes nothing. It reads only the folders it is given and writes nothing there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import ChannelName
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.config.models import Config, OneDriveFolderSink
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.extract.ubl import MAX_XML_BYTES, XmlRejected, parse_invoice_xml
from invoicebot.pipeline.route import natural_key as _route_natural_key
from invoicebot.states import DONE
from invoicebot.store.flow_b import BatchState, merge_flow_data, open_batch, set_batch_state
from invoicebot.store.invoices import (
    claim_natural_key,
    create_invoice,
    save_reading,
    set_flow,
    set_state,
)

#: ``{YYYY}-{M or MM}-Support_{seq}_{comercio}`` with the optional ``_Sent`` and
#: ``_PAID_{dd}_{mm}___{amount}`` suffixes the sink appends (R-7.11, R-7.16).
FOLDER_NAME_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{1,2})-Support_(?P<seq>\d+)_"
    r"(?P<comercio>[^/\\]+?)"
    r"(?P<sent>_Sent)?"
    r"(?P<paid>_PAID_\d{2}_\d{2}___\d+(?:[._]\d+)?)?$"
)
MIN_MONTH = 1
MAX_MONTH = 12
MIN_SEQ = 1

SKIP_NAME = "not a claim folder name"
SKIP_PAID = "already paid"
SKIP_UNSENT = "not sent"
SKIP_NO_XML = "no XML in the folder"
SKIP_SEVERAL_XML = "more than one XML in the folder"
SKIP_XML_TOO_LARGE = "the XML is over the size cap"
SKIP_XML_UNREADABLE = "the XML cannot be read"
SKIP_XML_UNPARSEABLE = "the XML is not a readable invoice"
SKIP_FIELDS_MISSING = "the XML lacks a required claim field"
#: Why a claim is skipped when another invoice already holds its natural key (R-7.16).
SKIP_EXISTS = "its natural key is already registered"

#: A folder named only by its four-digit year holds that year's claim folders (R-7.11).
YEAR_FOLDER_PATTERN = re.compile(r"^\d{4}$")
#: The suffix a sent claim folder carries and its base folder name does not (R-7.11).
SENT_FOLDER_SUFFIX = "_Sent"
#: The batch state an imported claim's batch is stored in: the mail already went out (D110).
SENT_BATCH_STATE = "sent"
#: The state ``open_batch`` stores before the batch is closed as sent (R-7.5).
PREPARED_BATCH_STATE: BatchState = "prepared"

#: What the import did with one claim (R-7.16).
type ImportStatus = Literal["registered", "would_register", "skipped"]


@dataclass(frozen=True)
class ClaimFolder:
    """The claim data a folder name carries: its month, seq, comercio and status (R-7.16)."""

    year: int
    month: int
    seq: int
    comercio: str
    sent: bool
    paid: bool


@dataclass(frozen=True)
class Claim:
    """One sent, unpaid claim read from a folder, with the folder it came from (R-7.16)."""

    ruc: str
    tipo: str
    serie: str
    numero: str
    fecha: date
    total: Decimal
    razon_social: str
    folder: ClaimFolder
    #: Where the claim was read from, not part of the claim's data, so not compared.
    folder_path: Path | None = field(default=None, compare=False)
    #: The claim folder's path under the copy root, as POSIX, not compared either.
    relative_path: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Skipped:
    """A folder the import does not register, and the reason the script prints (R-7.16)."""

    reason: str


@dataclass(frozen=True)
class ClaimReading:
    """One folder the walk visited: the claim it holds, or why it is skipped (R-7.16)."""

    path: Path
    result: Claim | Skipped


@dataclass(frozen=True)
class ImportEntry:
    """One claim the import looked at and what it did with it (R-7.16)."""

    claim: Claim
    status: ImportStatus
    invoice_id: int | None = None


@dataclass(frozen=True)
class ImportReport:
    """The outcome of one import run, one entry per claim (R-7.16)."""

    entries: tuple[ImportEntry, ...]


@dataclass(frozen=True)
class ImportTarget:
    """Where an imported claim is filed: its flow and that flow's sink root (R-7.16)."""

    flow_id: str
    drive_root: str


def parse_claim_folder(name: str) -> ClaimFolder | None:
    """Return the claim data in a folder name, or ``None`` when it is not one (R-7.16).

    The month may be written with or without its leading zero, and the comercio may not
    hold a path separator.
    """
    match = FOLDER_NAME_PATTERN.match(name)
    if match is None:
        return None
    month = int(match.group("month"))
    seq = int(match.group("seq"))
    if not MIN_MONTH <= month <= MAX_MONTH or seq < MIN_SEQ:
        return None
    return ClaimFolder(
        year=int(match.group("year")),
        month=month,
        seq=seq,
        comercio=match.group("comercio"),
        sent=match.group("sent") is not None,
        paid=match.group("paid") is not None,
    )


def read_claim(folder: Path) -> Claim | Skipped:
    """Return the claim ``folder`` holds, or the reason it is not one (R-7.16).

    Only a sent and unpaid folder with exactly one readable XML under the size cap counts
    (D110).
    """
    claim_folder = parse_claim_folder(folder.name)
    if claim_folder is None:
        return Skipped(SKIP_NAME)
    if claim_folder.paid:
        return Skipped(SKIP_PAID)
    if not claim_folder.sent:
        return Skipped(SKIP_UNSENT)
    xmls = sorted(folder.glob("*.xml"))
    if not xmls:
        return Skipped(SKIP_NO_XML)
    if len(xmls) > 1:
        return Skipped(SKIP_SEVERAL_XML)
    return _claim_from_xml(xmls[0], claim_folder)


def _claim_from_xml(xml: Path, folder: ClaimFolder) -> Claim | Skipped:
    """Return the claim in ``xml``, or the reason it cannot be read (R-7.16)."""
    try:
        if xml.stat().st_size > MAX_XML_BYTES:
            return Skipped(SKIP_XML_TOO_LARGE)
        data = xml.read_bytes()
    except OSError:
        return Skipped(SKIP_XML_UNREADABLE)
    try:
        reading = parse_invoice_xml(data)
    except XmlRejected:
        return Skipped(SKIP_XML_UNPARSEABLE)
    ruc = reading.value("ruc_emisor")
    tipo = reading.value("tipo")
    serie = reading.value("serie")
    numero = reading.value("numero")
    fecha = reading.fecha()
    total = reading.amount("total")
    razon_social = reading.value("razon_social")
    if (
        ruc is None
        or tipo is None
        or serie is None
        or numero is None
        or fecha is None
        or total is None
        or razon_social is None
    ):
        return Skipped(SKIP_FIELDS_MISSING)
    return Claim(
        ruc=ruc,
        tipo=tipo,
        serie=serie,
        numero=numero,
        fecha=fecha,
        total=total,
        razon_social=razon_social,
        folder=folder,
        folder_path=xml.parent,
    )


def claim_folders(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield each claim folder under the roots, descending one level into a year folder (R-7.16)."""
    for root in roots:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if YEAR_FOLDER_PATTERN.fullmatch(child.name):
                yield from (folder for folder in sorted(child.iterdir()) if folder.is_dir())
            else:
                yield child


def read_claims(roots: Iterable[Path]) -> list[ClaimReading]:
    """Read every claim folder under the roots, in walk order (R-7.16).

    A claim also knows its folder's POSIX path under the root it was walked from, which is
    the one the flow's OneDrive folder sink shares.
    """
    readings: list[ClaimReading] = []
    for root in roots:
        for path in claim_folders([root]):
            result = read_claim(path)
            if isinstance(result, Claim):
                result = replace(result, relative_path=path.relative_to(root).as_posix())
            readings.append(ClaimReading(path, result))
    return readings


def import_flow(cfg: Config, profile_id: str) -> str:
    """Return the profile's flow that files into an OneDrive folder (flow B, D110).

    Raises:
        ValueError: the profile is unknown, names an unknown flow, or has no single flow
            with an ``onedrive_folder`` sink.
    """
    profile = cfg.profiles.get(profile_id)
    if profile is None:
        raise ValueError(f"unknown profile {profile_id!r}")
    folder_flows: list[str] = []
    for name in profile.flows:
        flow = cfg.flows.get(name)
        if flow is None:
            raise ValueError(f"profile {profile_id!r} names unknown flow {name!r}")
        if any(isinstance(sink, OneDriveFolderSink) for sink in flow.sinks):
            folder_flows.append(name)
    if len(folder_flows) != 1:
        raise ValueError(
            f"profile {profile_id!r} needs exactly one flow with an OneDrive folder sink, "
            f"found {folder_flows}"
        )
    return folder_flows[0]


def import_origin_channel(cfg: Config) -> ChannelName:
    """Return the owner profile's preferred channel for an imported claim (R-1.9, D110).

    Raises:
        ValueError: the owner profile has no preferred channel.
    """
    return ProfileIndex.from_config(cfg).admin_channel(cfg.owner.profile)


def folder_sink_root(cfg: Config, flow_id: str) -> str:
    """Return the root of the flow's OneDrive folder sink (R-7.16).

    Raises:
        ValueError: the flow is unknown or has no ``onedrive_folder`` sink.
    """
    flow = cfg.flows.get(flow_id)
    if flow is None:
        raise ValueError(f"unknown flow {flow_id!r}")
    for sink in flow.sinks:
        if isinstance(sink, OneDriveFolderSink):
            return sink.root
    raise ValueError(f"flow {flow_id!r} has no OneDrive folder sink")


async def register_claims(
    conn: AsyncConnection[Any],
    profile_id: str,
    target: ImportTarget,
    claims: Iterable[Claim],
    *,
    origin_channel: ChannelName,
    dry_run: bool = False,
) -> ImportReport:
    """Register each claim as a DONE flow B invoice with a sent batch (R-7.16, D110).

    The invoice's origin channel is the owner profile's preferred channel (R-1.9). A
    claim whose natural key an invoice already holds is skipped, so a second run changes
    nothing. Each claim is registered in its own transaction; with ``dry_run`` nothing is
    written and the report says which claims would be registered. Never commits outside
    a claim's transaction.
    """
    entries: list[ImportEntry] = []
    for claim in claims:
        reading = _claim_reading(claim)
        key = _natural_key(profile_id, reading)
        async with conn.transaction():
            holder = await _natural_key_holder(conn, key)
            if holder is not None:
                entries.append(ImportEntry(claim, "skipped", holder))
                continue
            if dry_run:
                entries.append(ImportEntry(claim, "would_register"))
                continue
            invoice_id = await _start_invoice(conn, profile_id, key, origin_channel)
            await _insert_claim(conn, invoice_id, target, claim, reading)
        entries.append(ImportEntry(claim, "registered", invoice_id))
    return ImportReport(tuple(entries))


def _natural_key(profile_id: str, reading: Reading) -> str:
    """Return the claim's D93 natural key through the pipeline's one builder (R-7.16)."""
    key = _route_natural_key(profile_id, reading)
    if key is None:
        raise ValueError("a claim is missing a natural-key field")
    return key


async def _natural_key_holder(conn: AsyncConnection[Any], key: str) -> int | None:
    """Return the id of the invoice already holding ``key``, or None (R-7.16)."""
    cur = await conn.execute("SELECT id FROM invoices WHERE natural_key = %s", (key,))
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def _start_invoice(
    conn: AsyncConnection[Any],
    profile_id: str,
    key: str,
    origin_channel: ChannelName,
) -> int:
    """Create the claim's invoice on ``origin_channel`` and give it its natural key (R-7.16)."""
    invoice_id = await create_invoice(conn, profile_id, origin_channel)
    duplicate = await claim_natural_key(conn, invoice_id, key)
    if duplicate is not None:
        raise RuntimeError(f"invoice {duplicate.invoice_id} claimed the key first")
    return invoice_id


async def _insert_claim(
    conn: AsyncConnection[Any],
    invoice_id: int,
    target: ImportTarget,
    claim: Claim,
    reading: Reading,
) -> None:
    """Store one claim's fields, batch, flow and folder data on its invoice (R-7.16)."""
    await save_reading(conn, invoice_id, reading)
    await conn.execute(
        "UPDATE invoices SET razon_social = %s WHERE id = %s",
        (claim.razon_social, invoice_id),
    )
    batch_id = await open_batch(conn, invoice_id, target.flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await set_flow(conn, invoice_id, target.flow_id)
    await merge_flow_data(conn, invoice_id, _flow_data(claim, target.drive_root))
    await set_state(conn, invoice_id, DONE)


def _claim_reading(claim: Claim) -> Reading:
    """Return the claim's invoice fields as an XML reading for storage (R-7.16)."""
    values = {
        "ruc_emisor": claim.ruc,
        "tipo": claim.tipo,
        "serie": claim.serie,
        "numero": claim.numero,
        "fecha_emision": claim.fecha.isoformat(),
        "total": f"{claim.total:f}",
        "razon_social": claim.razon_social,
    }
    return Reading(
        fields={
            name: Field(value=value, source="xml", confidence=FULL_CONFIDENCE)
            for name, value in values.items()
        },
        document_url=None,
    )


def _flow_data(claim: Claim, drive_root: str) -> dict[str, Any]:
    """Return D110's flow data for an imported claim, filed under the sink's root (R-7.16)."""
    if claim.folder_path is None:
        raise ValueError("an imported claim needs the folder it was read from")
    if claim.relative_path is None:
        raise ValueError("an imported claim needs its path under the copy root")
    return {
        "folder": claim.folder_path.name.removesuffix(SENT_FOLDER_SUFFIX),
        "folder_path": f"{drive_root}/{claim.relative_path}",
        "seq": claim.folder.seq,
        "year": claim.folder.year,
        "month": claim.folder.month,
        "comercio": claim.folder.comercio,
        "imported": True,
    }
