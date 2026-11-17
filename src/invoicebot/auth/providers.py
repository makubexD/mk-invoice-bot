"""The accounts each provider signs in and the scopes they need (R-6.5, R-7.9, D85, D129).

Microsoft: the app and the ``auth-microsoft`` command build an account's provider the same
way: the client id comes from config, the encrypted MSAL cache lives under
``data_dir/tokens``, and the age identity decrypts it.

Google: ``google_accounts`` lists the accounts a Google sink or a ``gmail`` inbox names,
and ``google_scopes_for`` derives each one's scopes from config alone (D129), so signing
in asks for no scope the account's flows do not use.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from invoicebot.auth.msal_cache import TokenProvider
from invoicebot.config.models import (
    Config,
    GoogleDriveSink,
    GoogleSheetsSink,
    MailerSink,
    OneDriveFolderSink,
)
from invoicebot.secrets import load_identity
from invoicebot.settings import Settings

#: The scopes every account signs in with: token refresh only (R-7.9).
BASE_SCOPES = ("offline_access",)
#: Reading and writing the invoice files in the account's OneDrive (R-7.9).
FILES_SCOPE = "Files.ReadWrite"
#: Reading the invoice mailbox a flow's ``email_inbox`` fetcher searches (R-6.5, D98).
MAIL_READ_SCOPE = "Mail.Read"
#: The scope the invoice email needs; only a Graph mailer from the account adds it (R-7.9).
MAIL_SEND_SCOPE = "Mail.Send"
#: The mailer mode and inbox provider that talk to Graph (R-7.6, R-6.5).
GRAPH_MODE = "graph"
#: The mailer mode that sends through the bot's mailbox for the owner's rule (R-7.7, D108).
INBOX_RULE_MODE = "inbox_rule"
#: OneDrive storage targets name their account with this prefix (R-7.1, D98).
ONEDRIVE_TARGET_PREFIX = "onedrive_"
#: Where each account's encrypted MSAL cache lives under ``data_dir`` (D85).
TOKENS_DIRECTORY = "tokens"
#: The scope that returns an id_token naming the signed-in mailbox (R-6.5, D138).
OPENID_SCOPE = "openid"
#: The scope that adds the mailbox and its verified flag to the id_token (R-6.5, D138).
EMAIL_SCOPE = "email"
#: The scopes that only name the signed-in user. Google grants ``email`` back as
#: ``userinfo.email``, so the granted-scope check skips these; the id_token proves them
#: (R-6.5, D138).
IDENTITY_SCOPES = (OPENID_SCOPE, EMAIL_SCOPE)
#: The Drive scope a Google sink's account signs in with (R-6.5, D129).
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
#: The Sheets scope an account needs only for the owner's existing sheet (R-6.5, D145).
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
#: The Gmail scope a ``gmail`` inbox's account signs in with (R-6.5, D129).
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
#: The inbox provider that talks to Gmail instead of Graph (R-6.5, D135).
GMAIL_PROVIDER = "gmail"


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


def google_token_path(data_dir: Path, account: str) -> Path:
    """Return the encrypted refresh token file of one Google account (D128)."""
    return data_dir / TOKENS_DIRECTORY / f"{account}.google.age"


def _files_accounts(cfg: Config) -> set[str]:
    """Return the accounts an OneDrive target names across every flow (R-7.1, D98)."""
    return {
        target.removeprefix(ONEDRIVE_TARGET_PREFIX)
        for flow in cfg.flows.values()
        for sink in flow.sinks
        if isinstance(sink, OneDriveFolderSink)
        for target in sink.targets
    }


def _mailer_accounts(cfg: Config) -> set[str]:
    """Return the accounts a Graph mailer sends from across every flow (R-7.6, R-7.7).

    The ``inbox_rule`` mode sends through the bot's own mailbox, so that account
    needs ``Mail.Send`` too (D108).
    """
    accounts: set[str] = set()
    for flow in cfg.flows.values():
        for sink in flow.sinks:
            if not isinstance(sink, MailerSink):
                continue
            if GRAPH_MODE in sink.modes:
                accounts.add(sink.account)
            if INBOX_RULE_MODE in sink.modes:
                accounts.add(sink.inbox_rule_account)
    return accounts


def _inbox_accounts(cfg: Config) -> set[str]:
    """Return the accounts a Graph ``email_inbox`` fetcher searches (R-6.5, D98)."""
    return {
        flow.email_inbox.account
        for flow in cfg.flows.values()
        if flow.email_inbox is not None and flow.email_inbox.provider == GRAPH_MODE
    }


def scopes_for(cfg: Config, account: str) -> tuple[str, ...]:
    """Return the scopes ``account`` needs for the configured flows (R-6.5, R-7.9, D98)."""
    scopes = list(BASE_SCOPES)
    if account in _files_accounts(cfg):
        scopes.append(FILES_SCOPE)
    if account in _mailer_accounts(cfg):
        scopes.append(MAIL_SEND_SCOPE)
    if account in _inbox_accounts(cfg):
        scopes.append(MAIL_READ_SCOPE)
    return tuple(scopes)


def microsoft_accounts(cfg: Config) -> frozenset[str]:
    """Return every account a flow uses, each with a scope beyond ``offline_access`` (R-6.5, R-7.9, D98)."""
    candidates = _files_accounts(cfg) | _mailer_accounts(cfg) | _inbox_accounts(cfg)
    return frozenset(account for account in candidates if len(scopes_for(cfg, account)) > len(BASE_SCOPES))


def delivery_accounts(cfg: Config) -> frozenset[str]:
    """Return the used accounts whose sign-in delivers files or mail (R-6.5, R-7.9, D98)."""
    delivery = {FILES_SCOPE, MAIL_SEND_SCOPE}
    return frozenset(
        account for account in microsoft_accounts(cfg) if delivery.intersection(scopes_for(cfg, account))
    )


def _google_sink_accounts(cfg: Config) -> set[str]:
    """Return the accounts a Google sink names across every flow (R-6.5, D129)."""
    return {
        sink.account
        for flow in cfg.flows.values()
        for sink in flow.sinks
        if isinstance(sink, (GoogleDriveSink, GoogleSheetsSink))
    }


def _existing_sheet_accounts(cfg: Config) -> set[str]:
    """Return the accounts whose sheets sink is the owner's existing sheet (R-6.5, D145)."""
    return {
        sink.account
        for flow in cfg.flows.values()
        for sink in flow.sinks
        if isinstance(sink, GoogleSheetsSink) and sink.existing
    }


def _gmail_inbox_accounts(cfg: Config) -> set[str]:
    """Return the accounts a ``gmail`` inbox fetcher searches (R-6.5, D129)."""
    return {
        flow.email_inbox.account
        for flow in cfg.flows.values()
        if flow.email_inbox is not None and flow.email_inbox.provider == GMAIL_PROVIDER
    }


def google_accounts(cfg: Config) -> set[str]:
    """Return every account a Google sink or a ``gmail`` inbox names (R-6.5, D129)."""
    return _google_sink_accounts(cfg) | _gmail_inbox_accounts(cfg)


def google_scopes_for(cfg: Config, account: str) -> list[str]:
    """Return the Google scopes ``account`` needs for the configured flows (R-6.5, D129).

    Raises:
        ValueError: no Google sink or ``gmail`` inbox names ``account``.
    """
    scopes: list[str] = []
    if account in _google_sink_accounts(cfg):
        scopes.append(DRIVE_FILE_SCOPE)
    if account in _existing_sheet_accounts(cfg):
        scopes.append(SHEETS_SCOPE)
    if account in _gmail_inbox_accounts(cfg):
        scopes.append(GMAIL_READ_SCOPE)
    if not scopes:
        raise ValueError(f"no Google sink or gmail inbox names account {account!r}")
    return [*IDENTITY_SCOPES, *scopes]
