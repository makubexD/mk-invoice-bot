"""The JSON the bot and the browser helper exchange (D82).

This is the one home for the helper's wire format. It must never import Playwright, so the
bot can import it without loading a browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchInvoice, FetchReason


class HelperRequest(BaseModel):
    """The fetch the helper performs (D82)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    portal: Portal
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)


class HelperFile(BaseModel):
    """One file the helper downloaded, as it appears on the wire."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pdf", "xml"]
    path: Path
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HelperOutput(BaseModel):
    """The helper's one stdout line: the reason and its files (S10.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: FetchReason
    files: tuple[HelperFile, ...] = ()
