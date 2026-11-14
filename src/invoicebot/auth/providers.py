"""Microsoft accounts' token providers and the scopes each one signs in with (R-7.9, D85).

The app and the ``auth-microsoft`` command build an account's provider the same way: the
client id comes from config, the encrypted MSAL cache lives under ``data_dir/tokens``, and
the age identity decrypts it.
"""

from __future__ import annotations

from collections.abc import Sequence

from invoicebot.auth.msal_cache import TokenProvider
from invoicebot.config.models import Config, MailerSink
from invoicebot.secrets import load_identity
from invoicebot.settings import Settings

#: The scopes every account signs in with: token refresh and OneDrive writes (R-7.9).
BASE_SCOPES = ("offline_access", "Files.ReadWrite")
#: The scope the invoice email needs; only a Graph mailer from the account adds it (R-7.9).
MAIL_SEND_SCOPE = "Mail.Send"
#: The mail mode that sends through Graph (R-7.6).
GRAPH_MODE = "graph"
#: Where each account's encrypted MSAL cache lives under ``data_dir`` (D85).
TOKENS_DIRECTORY = "tokens"


def build_token_provider(
    settings: Settings,
    cfg: Config,
    account: str,
    scopes: Sequence[str],
) -> TokenProvider:
    """Build the real provider: client id from config, cache under ``data_dir/tokens`` (D85).

    Raises:
        ValueError: ``AGE_IDENTITY_PATH`` is unset, so no cache can be decrypted.
    """
    if settings.age_identity_path is None:
        raise ValueError("AGE_IDENTITY_PATH must be set to sign in (R-7.9)")
    return TokenProvider(
        client_id=cfg.microsoft.client_id,
        account=account,
        scopes=scopes,
        cache_path=settings.data_dir / TOKENS_DIRECTORY / f"{account}.msal.age",
        identity=load_identity(settings.age_identity_path),
    )


def scopes_for(cfg: Config, account: str) -> tuple[str, ...]:
    """Return the scopes the account needs; add ``Mail.Send`` only for a Graph mailer (R-7.9)."""
    scopes = list(BASE_SCOPES)
    if any(
        isinstance(sink, MailerSink) and sink.account == account and GRAPH_MODE in sink.modes
        for flow in cfg.flows.values()
        for sink in flow.sinks
    ):
        scopes.append(MAIL_SEND_SCOPE)
    return tuple(scopes)
