"""The OneDrive folder sink: one invoice folder with its PDF, XML and manifest (R-7.1, R-7.2, D88).

One job: pick the folder name and number for an invoice, create the folder and
upload its files. The drive is a port; nothing here talks to Graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from invoicebot.config.models import OneDriveFolderSink
from invoicebot.sinks.base import DeliveryContext, Drive
from invoicebot.sinks.expr import render
from invoicebot.sinks.folders import max_seq, seq_pattern

#: The manifest version written into every folder (R-7.2).
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class FolderResult:
    """The folder one invoice was filed in (R-7.1): its name, absolute path and sequence number."""

    folder: str
    path: str
    seq: int


async def file_folder(sink: OneDriveFolderSink, drive: Drive, ctx: DeliveryContext) -> FolderResult:
    """Create the invoice's folder and upload its PDF, XML and manifest (R-7.1, R-7.2).

    A folder saved by an earlier attempt is reused as-is, so a retried delivery
    uploads again into the same folder and never claims a second number (D88). When
    ``folder_status`` renamed that folder, the saved ``folder_path`` is the path the
    upload goes into, so a BR-D11 resend keeps the one renamed folder instead of
    creating a second one (R-7.11, D109). The sequence number is read from the folders
    already under the sink root and its year folder, where a paid folder has moved
    (R-7.11).
    """
    if "folder" in ctx.saved:
        folder = str(ctx.saved["folder"])
        seq = int(ctx.saved["seq"])
        saved_path = ctx.saved.get("folder_path")
        path = saved_path if isinstance(saved_path, str) and saved_path else f"{sink.root}/{folder}"
    else:
        folder, seq = await _claim_folder(sink, drive, ctx)
        path = f"{sink.root}/{folder}"
    parent, name = path.rsplit("/", 1)
    await drive.create_folder(parent, name)
    await drive.upload(f"{path}/{render(sink.files.pdf, ctx.values)}", ctx.pdf)
    if ctx.xml is not None:
        await drive.upload(f"{path}/{render(sink.files.xml, ctx.values)}", ctx.xml)
    await drive.upload(f"{path}/{sink.manifest}", _manifest(ctx.invoice_id))
    return FolderResult(folder=folder, path=path, seq=seq)


async def _claim_folder(sink: OneDriveFolderSink, drive: Drive, ctx: DeliveryContext) -> tuple[str, int]:
    """Return the invoice's folder name with its newly claimed sequence number (R-7.1)."""
    fecha = ctx.values.get("fecha")
    if not isinstance(fecha, date):
        raise TypeError("delivery values need a 'fecha' date")
    pattern = seq_pattern(sink.folder, fecha.year, fecha.month)
    names = await drive.list_names(sink.root)
    names += await drive.list_names(f"{sink.root}/{fecha.year}")
    seq = await ctx.claim_seq(max_seq(names, pattern))
    return render(sink.folder, {**ctx.values, "seq": seq}), seq


def _manifest(invoice_id: int) -> bytes:
    """Return the folder's manifest bytes, carrying the invoice id (R-7.2)."""
    return json.dumps({"invoice_id": invoice_id, "version": MANIFEST_VERSION}).encode()
