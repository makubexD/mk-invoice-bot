"""The JSON the bot and the browser helper exchange (D82).

This is the one home for the helper's wire format. It must never import Playwright, so the
bot can import it without loading a browser.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from invoicebot.config.models import MediaMaxBytes, Portal, SunatSolSite
from invoicebot.fetchers.base import FetchInvoice, FetchReason

#: The one argument a sandboxed helper accepts: run Chromium's own sandbox (D147).
CHROMIUM_SANDBOX_FLAG = "--chromium-sandbox"


class BadRequest(Exception):
    """The helper's stdin or its arguments are not one valid request."""


def parse_helper_args(argv: Sequence[str]) -> bool:
    """Return whether the helper runs Chromium with its sandbox (D147).

    Raises:
        BadRequest: the arguments are not empty and not exactly ``CHROMIUM_SANDBOX_FLAG``.
    """
    if not argv:
        return False
    if list(argv) == [CHROMIUM_SANDBOX_FLAG]:
        return True
    raise BadRequest


class HelperRequest(BaseModel):
    """The fetch the helper performs (D82)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    portal: Portal
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)


class SolCredentialsIn(BaseModel):
    """The SOL credentials as they arrive on stdin; their text form never shows them (R-9.16)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ruc: SecretStr
    user: SecretStr
    password: SecretStr


class SolRequest(BaseModel):
    """The SOL fetch the helper performs (D82, R-9.10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: SunatSolSite
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)
    credentials: SolCredentialsIn


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
