"""The ``sunat_sol`` fetcher: run the SOL browser helper once per fetch (S10.1, R-5.2).

The fetcher builds the SOL request and sends it on the helper's stdin, so the credentials
travel in neither the helper's argv nor its environment and nothing here logs the request
(R-9.10, R-9.16). The helper signs in to SUNAT and downloads the files; this module maps its
output as the portal fetcher maps one and reports ``sunat_sol`` as the portal key (R-5.7).
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr, ValidationError

from invoicebot.config.models import MediaMaxBytes, SunatSolSite
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.protocol import HelperOutput, SolCredentialsIn, SolRequest
from invoicebot.fetchers.portal import (
    MAX_HELPER_OUTPUT_BYTES,
    PAGE_TIMEOUT_MS,
    helper_env,
    result_from,
)
from invoicebot.helper import run_helper
from invoicebot.secrets import Secrets

#: The fetcher's key in a flow and in the fetch statistics (R-5.7).
SUNAT_SOL_FETCHER = "sunat_sol"
#: The secrets the SOL sign-in needs; the view must declare all three (R-9.10).
SECRETS = ("sol_ruc", "sol_user", "sol_password")
#: The SOL helper module; ``-P`` keeps the working directory off its import path (D82).
SOL_HELPER_COMMAND = (sys.executable, "-P", "-m", "invoicebot.fetchers.browser.sol")


class SunatSolFetcher:
    """The ``sunat_sol`` fetcher: one SOL helper run per fetch (S10.1, R-5.2)."""

    def __init__(
        self,
        site: SunatSolSite,
        secrets: Secrets,
        max_bytes: MediaMaxBytes,
        timeout_s: float,
        helper_command: Sequence[str] = SOL_HELPER_COMMAND,
    ) -> None:
        missing = sorted(set(SECRETS) & secrets.missing)
        if missing:
            raise ValueError(f"sunat_sol needs the missing secret(s) {', '.join(missing)} (R-9.10)")
        self._site = site
        self._secrets = secrets
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s
        self._helper_command = helper_command

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice`` from SUNAT SOL and report why (R-5.2, R-9.10)."""
        request = SolRequest(
            site=self._site,
            invoice=invoice,
            out_dir=out_dir,
            max_bytes=self._max_bytes,
            page_timeout_ms=PAGE_TIMEOUT_MS,
            credentials=_credentials(self._secrets),
        )
        output = await self._run(_payload(request))
        if isinstance(output, FetchResult):
            return output
        return result_from(output, out_dir, invoice, self._max_bytes, SUNAT_SOL_FETCHER)

    async def _run(self, payload: bytes) -> HelperOutput | FetchResult:
        """Run the helper in its own temp folder and validate its one JSON line (R-9.2, R-9.10)."""
        try:
            with tempfile.TemporaryDirectory(prefix="invoicebot-browser-") as scratch:
                stdout = await run_helper(
                    self._helper_command,
                    payload,
                    helper_env(Path(scratch)),
                    self._timeout_s,
                    MAX_HELPER_OUTPUT_BYTES,
                )
        except TimeoutError:
            return FetchResult(FetchReason.TIMEOUT, (), SUNAT_SOL_FETCHER)
        except RuntimeError:
            return FetchResult(FetchReason.TECHNICAL, (), SUNAT_SOL_FETCHER)
        try:
            return HelperOutput.model_validate_json(stdout)
        except ValidationError:
            return FetchResult(FetchReason.TECHNICAL, (), SUNAT_SOL_FETCHER)


def _credentials(secrets: Secrets) -> SolCredentialsIn:
    """Return the SOL credentials from the view, held as ``SecretStr`` (R-9.10, R-9.16)."""
    return SolCredentialsIn(
        ruc=SecretStr(secrets.get("sol_ruc")),
        user=SecretStr(secrets.get("sol_user")),
        password=SecretStr(secrets.get("sol_password")),
    )


def _payload(request: SolRequest) -> bytes:
    """Serialize ``request`` for stdin with the credential values revealed (R-9.10).

    ``model_dump_json`` would send the ``SecretStr`` mask instead of the values, so the
    credentials are replaced after a plain model dump. These bytes are never logged.
    """
    document = request.model_dump(mode="json")
    credentials = request.credentials
    document["credentials"] = {
        "ruc": credentials.ruc.get_secret_value(),
        "user": credentials.user.get_secret_value(),
        "password": credentials.password.get_secret_value(),
    }
    return json.dumps(document).encode()
