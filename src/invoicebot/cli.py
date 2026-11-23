"""The ``invoicebot`` command line: ``portal-test`` (S10.5), ``auth-microsoft`` (R-7.9),
``auth-google`` (R-6.4, R-6.5), ``google-setup`` (R-6.2, D129) and ``telegram`` (D186).

``portal-test`` reads one sample invoice from a YAML file and runs the named portal through
the browser helper into a temporary folder. It prints the reason, each file's kind and
size, and whether the XML matches; it never prints an invoice field value or a file path
(R-9.16). ``auth-microsoft`` signs one account in with the device-code flow and then resumes
the invoices that were parked in ``PENDING_AUTH`` (BR-D01). ``auth-google`` runs the
installed-app sign-in with a loopback redirect and PKCE, stores the account's encrypted
refresh token and resumes the same waiting invoices (R-6.4, R-6.5, D128). ``google-setup``
takes a flow name: it creates the flow's Drive folder (only when the flow has a Drive
sink) and either creates its spreadsheet with the header or checks an existing one, then
prints the ids for the private config (R-6.2, D129, D145). ``telegram`` lists the private
senders who pressed Start and registers, inspects or deletes the bot's webhook, reading its
token and secret from the age store (D186, R-9.9).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import secrets
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml
from pydantic import TypeAdapter, ValidationError

from invoicebot import rehearse
from invoicebot.auth.google_consent import (
    STATE_PARAM,
    ConsentError,
    consent_url,
    parse_redirect,
    pkce_pair,
)
from invoicebot.auth.google_oauth import (
    GOOGLE_CLIENT_SECRET,
    TOKEN_URL,
    GoogleTokens,
    google_secrets,
    revoke,
    save_refresh_token,
)
from invoicebot.auth.msal_cache import AuthRequired, TokenProvider
from invoicebot.auth.providers import (
    DRIVE_FILE_SCOPE,
    IDENTITY_SCOPES,
    build_token_provider,
    delivery_accounts,
    google_accounts,
    google_scopes_for,
    google_token_path,
    microsoft_accounts,
    scopes_for,
)
from invoicebot.channels.telegram.client import TelegramClient, TelegramError, telegram_secrets
from invoicebot.channels.telegram.setup import (
    WebhookActive,
    delete_webhook,
    sender_ids,
    set_webhook,
    webhook_info,
)
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import (
    SHEETS_KEY_COLUMN,
    Config,
    Flow,
    Google,
    GoogleDriveSink,
    GoogleSheetsSink,
    StoreDirectory,
    enabled_chat_channels,
)
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.portal import DEFAULT_HELPER_COMMAND, PortalFetcher
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.pipeline.pending_auth import GOOGLE, MICROSOFT
from invoicebot.queue.jobs import enqueue
from invoicebot.secrets import Secrets, SecretStore, load_identity
from invoicebot.settings import Settings
from invoicebot.sinks.google_api import GoogleError
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.store.db import create_pool
from invoicebot.store.invoices import resume_pending_auth
from invoicebot.text import normalize_email

_SAMPLE = TypeAdapter(FetchInvoice)
#: How long the Google sign-in waits for the browser's redirect (D128).
SIGN_IN_TIMEOUT_S = 300
#: The request-line cap of the one-request loopback listener (R-6.5).
_REQUEST_LINE_LIMIT = 8192
#: The page the browser shows once the redirect arrives (R-6.5).
_CALLBACK_BODY = b"Sign-in received; you can close this tab.\n"
_CALLBACK_HEADERS = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\nContent-Length: "
)
_CALLBACK_RESPONSE = (
    _CALLBACK_HEADERS + str(len(_CALLBACK_BODY)).encode("ascii") + b"\r\n\r\n" + _CALLBACK_BODY
)
#: The page a request without this sign-in's ``state`` gets (R-9.9).
_BAD_REQUEST_BODY = b"Bad request.\n"
_BAD_REQUEST_RESPONSE = (
    b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain; charset=utf-8\r\n"
    b"Connection: close\r\nContent-Length: "
    + str(len(_BAD_REQUEST_BODY)).encode("ascii")
    + b"\r\n\r\n"
    + _BAD_REQUEST_BODY
)
#: The random bytes behind the Google sign-in's ``state`` (R-6.5).
_STATE_BYTES = 32
#: An id_token is ``header.payload.signature`` (R-6.5, D138).
_ID_TOKEN_PARTS = 3
#: The age-store secret the Google sign-in reads (D128).

#: The root folder ``google-setup`` creates for flow A's invoices (D129).
GOOGLE_SETUP_FOLDER_NAME = "Comprobantes"
#: The spreadsheet title ``google-setup`` gives the invoices sheet (D129).
GOOGLE_SETUP_SHEET_TITLE = "Comprobantes"


class _SignInError(Exception):
    """The token endpoint did not hand back a usable refresh token (R-6.4)."""


#: The verdict line for each reason that says something about the XML itself.
_XML_VERDICT = {FetchReason.OK: "yes", FetchReason.XML_MISMATCH: "no"}

#: Builds the account's provider from settings, config, the account and its scopes (R-7.9).
type TokenProviderFactory = Callable[[Settings, Config, str, Sequence[str]], TokenProvider]


def _show(message: str) -> None:
    """Write the sign-in URL or instruction the command built for the person (R-7.9)."""
    sys.stdout.write(message + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    helper_command: Sequence[str] = DEFAULT_HELPER_COMMAND,
    token_provider_factory: TokenProviderFactory = build_token_provider,
    settings: Settings | None = None,
    show: Callable[[str], None] = _show,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Parse ``argv`` and run the chosen command, returning its exit status (S10.5)."""
    parser = _parser()
    args = parser.parse_args(argv)
    resolved = settings if settings is not None else Settings()
    cfg = load_config(resolved.config_path)
    if args.command == "auth-microsoft":
        return _auth_microsoft(parser, resolved, cfg, args.account, token_provider_factory)
    if args.command == "auth-google":
        return _auth_google(parser, resolved, cfg, args.account, show, http_transport)
    if args.command == "google-setup":
        return _google_setup(parser, resolved, cfg, args, http_transport)
    if args.command == "telegram":
        return _telegram(parser, resolved, cfg, args, show, http_transport)
    if args.command == "rehearse":
        return _rehearse(parser, resolved, cfg, args)
    directory = load_directory(resolved.config_path.parent)
    return _portal_test(parser, directory, cfg, args.key, args.sample, helper_command)


def run() -> None:
    """Console-script entry point: exit with the command's status (S10.5)."""
    sys.exit(main())


def _parser() -> argparse.ArgumentParser:
    """Return the parser for ``invoicebot`` and its subcommands (S10.5)."""
    parser = argparse.ArgumentParser(prog="invoicebot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    portal_test = subparsers.add_parser("portal-test", help="run one portal fetch on a sample invoice")
    portal_test.add_argument("key", help="portal key, its file name under portals/")
    portal_test.add_argument("--sample", required=True, type=Path, help="YAML file with the invoice fields")
    auth = subparsers.add_parser(
        "auth-microsoft",
        help="sign a Microsoft account in and resume the invoices waiting for it",
    )
    auth.add_argument("--account", required=True, help="account name, e.g. personal")
    google = subparsers.add_parser(
        "auth-google",
        help="sign a Google account in and resume the invoices waiting for it",
    )
    google.add_argument("--account", required=True, help="account name, e.g. owner")
    setup = subparsers.add_parser(
        "google-setup",
        help="create or check the flow's Drive folder and spreadsheet",
    )
    setup.add_argument("--flow", required=True, help="flow name, e.g. personal_google")
    setup.add_argument(
        "--folder-name",
        default=GOOGLE_SETUP_FOLDER_NAME,
        help="Drive folder to create under the account's root",
    )
    setup.add_argument("--sheet-title", default=GOOGLE_SETUP_SHEET_TITLE, help="spreadsheet title")
    telegram = subparsers.add_parser(
        "telegram",
        help="collect the bot's sender ids and register its webhook (D186)",
    )
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    telegram_sub.add_parser("ids", help="list the private senders who pressed Start")
    webhook = telegram_sub.add_parser("webhook", help="set, inspect or delete the bot's webhook")
    webhook_action = webhook.add_mutually_exclusive_group(required=True)
    webhook_action.add_argument("--url", help="the public https URL to register")
    webhook_action.add_argument("--info", action="store_true", help="print the webhook's current state")
    webhook_action.add_argument("--delete", action="store_true", help="delete the webhook")
    rehearse = subparsers.add_parser(
        "rehearse",
        help="run the live pipeline through the dev-only console channel (D165)",
    )
    rehearse_sub = rehearse.add_subparsers(dest="rehearse_command", required=True)
    send = rehearse_sub.add_parser("send", help="feed one message, file or button press through intake")
    send.add_argument("--name", help="file name; its bytes are read from stdin (extension picks kind)")
    send.add_argument("--text", help="text message, e.g. a command")
    send.add_argument("--button", help="button id to press")
    send.add_argument(
        "--reply-to",
        type=int,
        help="console reply id to quote (the #<id> rehearse replies printed)",
    )
    replies = rehearse_sub.add_parser("replies", help="print the console replies the outbox kept")
    replies.add_argument(
        "--after",
        type=int,
        default=0,
        help="print only rows with an id above this (default 0)",
    )
    return parser


def _auth_microsoft(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    account: str,
    provider_factory: TokenProviderFactory,
) -> int:
    """Sign the account in, then resume the invoices that waited for it (R-7.9, BR-D01).

    An account no flow uses exits 2 before signing in. Only a sign-in that delivers files
    or mail resumes the waiting invoices; a failed sign-in exits 1 and resumes nothing, so
    no invoice leaves ``PENDING_AUTH`` without a working token.
    """
    accounts = microsoft_accounts(cfg)
    if account not in accounts:
        configured = ", ".join(sorted(accounts)) or "none"
        parser.error(f"account {account!r} is not configured; configured accounts: {configured}")
    if settings.database_url is None:
        parser.error("DATABASE_URL must be set to resume waiting invoices")
    provider = provider_factory(settings, cfg, account, scopes_for(cfg, account))
    try:
        provider.login_device_code(_show)
    except AuthRequired as error:
        sys.stderr.write(f"sign-in failed: {error}\n")
        return 1
    if account not in delivery_accounts(cfg):
        sys.stdout.write("resumed 0 invoice(s)\n")
        return 0
    count = asyncio.run(_resume_pending_auth(settings.database_url, MICROSOFT, account))
    sys.stdout.write(f"resumed {count} invoice(s)\n")
    return 0


def _auth_google(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    account: str,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Sign the account in over the loopback flow, then resume its waiting invoices (R-6.4, BR-D01).

    An account no Google sink or ``gmail`` inbox names exits 2 before signing in. A failed
    sign-in exits 1, writes no token and resumes nothing, so no invoice leaves
    ``PENDING_AUTH`` without a working token.
    """
    accounts = google_accounts(cfg)
    if account not in accounts:
        configured = ", ".join(sorted(accounts)) or "none"
        parser.error(f"account {account!r} is not configured; configured accounts: {configured}")
    if settings.database_url is None:
        parser.error("DATABASE_URL must be set to resume waiting invoices")
    if settings.secrets_path is None or settings.age_identity_path is None:
        parser.error("SECRETS_PATH and AGE_IDENTITY_PATH must be set to sign in")
    client_secret = google_secrets(SecretStore.load(settings.secrets_path, settings.age_identity_path)).get(
        GOOGLE_CLIENT_SECRET
    )
    try:
        asyncio.run(_google_sign_in(settings, cfg, account, client_secret, show, transport))
    except (ConsentError, TimeoutError, _SignInError, httpx.HTTPError) as error:
        sys.stderr.write(f"sign-in failed: {error}\n")
        return 1
    if DRIVE_FILE_SCOPE not in google_scopes_for(cfg, account):
        sys.stdout.write("resumed 0 invoice(s)\n")
        return 0
    count = asyncio.run(_resume_pending_auth(settings.database_url, GOOGLE, account))
    sys.stdout.write(f"resumed {count} invoice(s)\n")
    return 0


@dataclass(frozen=True)
class _SetupTarget:
    """The flow's sheets sink to create or check, and its Drive sink when it has one (D145)."""

    sink: GoogleSheetsSink
    drive: GoogleDriveSink | None


def _setup_target(flow: Flow) -> _SetupTarget | None:
    """Return what ``google-setup`` creates or checks for ``flow``, or None (D145)."""
    sink = next((other for other in flow.sinks if isinstance(other, GoogleSheetsSink)), None)
    if sink is None:
        return None
    drive = next((other for other in flow.sinks if isinstance(other, GoogleDriveSink)), None)
    return _SetupTarget(sink, drive)


def _google_setup(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Create or check the flow's Drive folder and sheet and print their ids (R-6.2, D129, D145).

    The flow named by ``--flow`` supplies its sheets sink, whose account signs the
    request and whose ``existing`` flag chooses creating a sheet or checking one.
    An unknown flow, or a flow with no Google sheets sink, exits 2 before any
    request. The command writes no config file, so the ids it prints go into the
    private config by hand.
    """
    flow = cfg.flows.get(args.flow)
    if flow is None:
        configured = ", ".join(sorted(cfg.flows)) or "none"
        parser.error(f"flow {args.flow!r} is not configured; configured flows: {configured}")
    target = _setup_target(flow)
    if target is None:
        parser.error(f"flow {args.flow!r} has no Google sheets sink")
    if cfg.google is None:
        parser.error("google settings must be configured to run google-setup (D128)")
    if settings.secrets_path is None or settings.age_identity_path is None:
        parser.error("SECRETS_PATH and AGE_IDENTITY_PATH must be set to create the sheet")
    client_secret = google_secrets(SecretStore.load(settings.secrets_path, settings.age_identity_path)).get(
        GOOGLE_CLIENT_SECRET
    )
    try:
        lines = asyncio.run(_create_google_setup(settings, cfg, args, target, client_secret, transport))
    except AuthRequired as required:
        sys.stderr.write(f"sign-in needed: run invoicebot auth-google --account {required.account}\n")
        return 1
    except (GoogleError, httpx.HTTPError) as error:
        sys.stderr.write(f"google-setup failed: {error}\n")
        return 1
    for line in lines:
        sys.stdout.write(line + "\n")
    return 0


def _telegram(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Collect sender ids or manage the bot's webhook (D186, R-9.16).

    The token and the webhook secret come from the age store, so neither is typed
    or printed. A failure names the missing secret or Telegram's status, never a
    value or the request URL.
    """
    if "telegram" not in enabled_chat_channels(cfg.channels):
        parser.error("the telegram channel is not enabled in the config")
    secrets_path, identity_path = settings.secrets_path, settings.age_identity_path
    if secrets_path is None or identity_path is None:
        parser.error("SECRETS_PATH and AGE_IDENTITY_PATH must be set to run telegram")
    secrets = telegram_secrets(SecretStore.load(secrets_path, identity_path))
    needed = {"telegram_bot_token"}
    if args.telegram_command == "webhook" and args.url is not None:
        needed.add("telegram_webhook_secret")
    missing = secrets.missing & needed
    if missing:
        parser.error(f"the secret store lacks {', '.join(sorted(missing))}")
    try:
        return asyncio.run(_run_telegram(args, secrets, show, transport))
    except ValueError as error:
        parser.error(str(error))
    except TelegramError as error:
        sys.stderr.write(f"{error}\n")
        return 1


async def _run_telegram(
    args: argparse.Namespace,
    secrets: Secrets,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Run the chosen Telegram command over one Bot API client (D186)."""
    async with httpx.AsyncClient(transport=transport) as http:
        client = TelegramClient(http, token=secrets.get("telegram_bot_token"))
        if args.telegram_command == "ids":
            return await _telegram_ids(client, show)
        await _telegram_webhook(client, secrets, args, show)
    return 0


async def _telegram_ids(client: TelegramClient, show: Callable[[str], None]) -> int:
    """Print one line per private sender, or the delete hint when a webhook is set (D186)."""
    try:
        senders = await sender_ids(client)
    except WebhookActive:
        sys.stderr.write("a webhook is set; run `invoicebot telegram webhook --delete` first\n")
        return 1
    if not senders:
        show("no messages yet: press Start in the bot, then run again")
    for user_id, username in senders:
        show(f"{user_id} @{username}" if username else f"{user_id} -")
    return 0


async def _telegram_webhook(
    client: TelegramClient,
    secrets: Secrets,
    args: argparse.Namespace,
    show: Callable[[str], None],
) -> None:
    """Set, print or delete the bot's webhook (D186)."""
    if args.url is not None:
        await set_webhook(client, args.url, secrets.get("telegram_webhook_secret"))
        show("webhook set")
    elif args.info:
        info = await webhook_info(client)
        show(f"url: {info.url}")
        show(f"pending: {info.pending}")
        show(f"last_error_date: {_shown(info.last_error_date)}")
        show(f"last_error_message: {_shown(info.last_error_message)}")
    else:
        await delete_webhook(client)
        show("webhook deleted")


def _shown(value: object) -> str:
    """Render a webhook info field, with ``-`` when Telegram reported none (D186)."""
    return "-" if value is None else str(value)


def _rehearse(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
) -> int:
    """Dispatch the ``rehearse`` subcommands (D165)."""
    if args.rehearse_command == "replies":
        return _rehearse_replies(parser, settings, cfg, args)
    return _rehearse_send(parser, settings, cfg, args)


def _rehearse_send(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
) -> int:
    """Feed one console message through the live intake and return its exit status (D165)."""
    if args.name is None and args.text is None and args.button is None:
        parser.error("rehearse send needs one of --name, --text or --button")
    if args.reply_to is not None and args.reply_to < 1:
        parser.error("--reply-to must be a positive reply id")
    database_url = settings.database_url
    if database_url is None:
        parser.error("DATABASE_URL must be set to rehearse a console message")
    try:
        request = rehearse.RehearseSend(
            name=args.name,
            text=args.text,
            button=args.button,
            reply_to=args.reply_to,
        )
        result = asyncio.run(
            rehearse.rehearse_send(
                settings.data_dir,
                database_url,
                cfg,
                intake_settings(cfg),
                request,
                sys.stdin.buffer,
            )
        )
    except rehearse.RehearseError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    _show(f"intake: {result.status}")
    if result.invoice_id is not None:
        _show(f"invoice: {result.invoice_id}")
    return 0


def _rehearse_replies(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
) -> int:
    """Print the console replies above ``--after`` and the next cursor (D165)."""
    if args.after < 0:
        parser.error("--after must be >= 0")
    database_url = settings.database_url
    if database_url is None:
        parser.error("DATABASE_URL must be set to rehearse a console message")
    try:
        rehearse.require_console(cfg)
    except rehearse.RehearseError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    replies = asyncio.run(rehearse.rehearse_replies(database_url, args.after))
    for line in rehearse.format_replies(replies, args.after):
        _show(line)
    return 0


async def _create_google_setup(
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
    target: _SetupTarget,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None,
) -> list[str]:
    """Create or check the flow's folder and sheet; return the lines to print (R-6.2, D129, D145).

    A flow with a Drive sink gets a root folder in that sink's account. A sheet the sink does not mark
    ``existing`` is created with the header; an existing sheet has its tab added
    or its header checked, and nothing else is written.

    Raises:
        AuthRequired: the account has no usable refresh token.
        GoogleError: Google refuses a request, answers without a usable id, or
            the existing tab's first row differs (``SheetHeaderMismatch``).
        httpx.HTTPError: a request never reached Google.
    """
    identity_path = settings.age_identity_path
    if identity_path is None:
        raise _SignInError("AGE_IDENTITY_PATH must be set to create the sheet")
    google = _google_settings(cfg)
    sink = target.sink
    header = [SHEETS_KEY_COLUMN, *sink.columns]
    lines: list[str] = []
    async with httpx.AsyncClient(transport=transport, timeout=google.timeout_s) as http:
        identity = load_identity(identity_path)

        def tokens_for(account: str) -> GoogleTokens:
            token_path = google_token_path(settings.data_dir, account)
            return GoogleTokens(
                http, google.client_id, client_secret, account, token_path, identity, google.timeout_s
            )

        if target.drive is not None:
            account = target.drive.account
            drive = GoogleDrive(http, tokens_for(account), google.timeout_s, account=account)
            folder_id = await drive.ensure_folder("root", args.folder_name)
            lines.append(f"root_folder_id: {folder_id}")
        sheets = GoogleSheets(
            http,
            tokens_for(sink.account),
            google.timeout_s,
            account=sink.account,
            max_rows=google.max_rows,
        )
        if sink.existing:
            await sheets.ensure_tab(sink.spreadsheet_id, sink.tab, header)
            lines.append(f"spreadsheet_id: {sink.spreadsheet_id} (checked)")
        else:
            spreadsheet_id = await sheets.create(args.sheet_title, sink.tab, header)
            lines.append(f"spreadsheet_id: {spreadsheet_id}")
    return lines


def _google_settings(cfg: Config) -> Google:
    """Return the Google OAuth client settings, or fail when config has none (D128).

    Raises:
        _SignInError: no ``google`` block is configured, so no client id or timeout exists.
    """
    if cfg.google is None:
        raise _SignInError("google settings must be configured to sign in (D128)")
    return cfg.google


async def _google_sign_in(
    settings: Settings,
    cfg: Config,
    account: str,
    client_secret: str,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Run one loopback sign-in and save the account's refresh token (R-6.4, R-6.5, D128).

    Raises:
        ConsentError: the redirect's state does not match or carries no code.
        TimeoutError: no redirect arrives within ``SIGN_IN_TIMEOUT_S``.
        _SignInError: the token endpoint refuses the code, the id_token does not name the
            account's pinned mailbox, the endpoint returns no refresh token, or it grants
            fewer scopes than the account needs.
    """
    identity_path = settings.age_identity_path
    if identity_path is None:
        raise _SignInError("AGE_IDENTITY_PATH must be set to sign in")
    google = _google_settings(cfg)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_path: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    scopes = google_scopes_for(cfg, account)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _answer_redirect(reader, writer, redirect_path, state)

    server = await asyncio.start_server(answer, "127.0.0.1", 0, limit=_REQUEST_LINE_LIMIT)
    async with server:
        sockets = server.sockets
        if not sockets:
            raise _SignInError("the sign-in listener did not bind")
        redirect_uri = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        show(consent_url(google.client_id, redirect_uri, scopes, state, challenge))
        async with asyncio.timeout(SIGN_IN_TIMEOUT_S):
            path = await redirect_path
    code = parse_redirect(path, state)
    async with httpx.AsyncClient(transport=transport, timeout=google.timeout_s) as client:
        response = await _exchange_code(client, google.client_id, client_secret, code, verifier, redirect_uri)
        await _require_pinned_mailbox(client, response, cfg, account)
    data_scopes = [scope for scope in scopes if scope not in IDENTITY_SCOPES]
    refresh_token = _granted_refresh_token(response, data_scopes)
    save_refresh_token(
        google_token_path(settings.data_dir, account),
        load_identity(identity_path),
        refresh_token,
    )


async def _exchange_code(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> httpx.Response:
    """Swap the authorization code at Google's token endpoint and return its answer (R-6.4).

    Raises:
        _SignInError: the endpoint answers non-200.
        httpx.HTTPError: the request never reached Google.
    """
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if response.status_code != httpx.codes.OK:
        raise _SignInError(f"the token endpoint answered {response.status_code} (R-6.4)")
    return response


def _id_token_claims(response: httpx.Response) -> dict[str, object] | None:
    """Return the id_token's payload, or None when it is missing or malformed (R-6.5, D138).

    The signature is not checked: the token arrives from Google's TLS token endpoint in
    the same answer to the code exchange (D138). Only the middle base64url part is read.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    id_token = body.get("id_token")
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) != _ID_TOKEN_PARTS:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


async def _require_pinned_mailbox(
    client: httpx.AsyncClient, response: httpx.Response, cfg: Config, account: str
) -> None:
    """Refuse a sign-in that is not the mailbox ``account`` is pinned to (R-6.5, D138).

    Before refusing, revoke the grant the wrong sign-in obtained, so it cannot be used
    afterwards. The refusal stands even when the revocation fails.

    Raises:
        _SignInError: the id_token is missing or malformed, its ``email_verified`` is not
            true, or its ``email`` differs from ``google.accounts[account]``. The message
            names the expected and the signed-in address.
    """
    expected = _google_settings(cfg).accounts[account]
    claims = _id_token_claims(response)
    address = claims.get("email") if claims is not None else None
    signed_in = address if isinstance(address, str) else "an unreadable id_token"
    if (
        isinstance(address, str)
        and claims is not None
        and claims.get("email_verified") is True
        and normalize_email(address) == normalize_email(expected)
    ):
        return
    token = _token_to_revoke(response)
    if token is not None and not _pinned_elsewhere(address, cfg):
        await revoke(client, token)
    raise _SignInError(f"signed in as {signed_in}, but account {account!r} is pinned to {expected}")


def _pinned_elsewhere(address: object, cfg: Config) -> bool:
    """True when ``address`` is another configured account's mailbox (D138).

    Google's revoke drops the whole grant for that user and client, so revoking a sign-in
    into another account's mailbox would sign that account out too; it is left alone.
    """
    if not isinstance(address, str):
        return False
    pinned = {normalize_email(mailbox) for mailbox in _google_settings(cfg).accounts.values()}
    return normalize_email(address) in pinned


def _token_to_revoke(response: httpx.Response) -> str | None:
    """Return a token from a refused sign-in's answer: its refresh token, else its access token (D138)."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("refresh_token", "access_token"):
        token = payload.get(key)
        if isinstance(token, str) and token:
            return token
    return None


def _granted_refresh_token(response: httpx.Response, scopes: Sequence[str]) -> str:
    """Return the refresh token in a token response, once every scope is granted (R-6.4, R-6.5).

    Raises:
        _SignInError: the body is not a token response, carries no refresh token, or its
            ``scope`` lacks one of ``scopes``. The message never echoes a token or the
            body (R-9.9).
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _SignInError(
            "the token response has no refresh_token; remove this app's access at "
            "https://myaccount.google.com/permissions and sign in again"
        )
    granted = payload.get("scope")
    granted_scopes = granted.split() if isinstance(granted, str) else []
    missing = [scope for scope in scopes if scope not in granted_scopes]
    if missing:
        raise _SignInError(
            f"Google did not grant: {' '.join(missing)}; sign in again and allow every permission"
        )
    return refresh_token


async def _answer_redirect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    redirect_path: asyncio.Future[str],
    expected_state: str,
) -> None:
    """Read one request line and answer it; only this sign-in's redirect ends the wait (R-6.5, R-9.9).

    A request whose ``state`` is missing or differs gets a plain 400 page and is
    ignored, so a browser preconnect, a probe or another sign-in's redirect cannot end
    this sign-in before the real redirect arrives.
    """
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError, asyncio.IncompleteReadError):
        line = b""
    path = _request_path(line, expected_state)
    try:
        writer.write(_CALLBACK_RESPONSE if path else _BAD_REQUEST_RESPONSE)
        await writer.drain()
    finally:
        writer.close()
        if path and not redirect_path.done():
            redirect_path.set_result(path)
        await writer.wait_closed()


def _request_path(line: bytes, expected_state: str) -> str:
    """Return the redirect's path once its ``state`` matches, or "" for any other request (R-6.5, R-9.9).

    Only a request whose query carries this sign-in's ``state`` can be the redirect; a
    ``/favicon.ico`` read, a browser preconnect or a redirect for another sign-in must
    not end this one.
    """
    parts = line.decode("ascii", "replace").split()
    if len(parts) < 2 or parts[0] != "GET":
        return ""
    path = parts[1]
    state = parse_qs(urlsplit(path).query, keep_blank_values=True).get(STATE_PARAM, [""])[0]
    if not hmac.compare_digest(state.encode(), expected_state.encode()):
        return ""
    return path


async def _resume_pending_auth(database_url: str, provider: str, account: str) -> int:
    """Move the account's waiting invoices to ``DELIVERING`` with a deliver job (BR-D01).

    The move and each invoice's deliver job share one transaction, so a crash never leaves an
    invoice ``DELIVERING`` without its job.
    """
    pool = create_pool(database_url)
    await pool.open(wait=True)
    invoice_ids: list[int] = []
    try:
        async with pool.connection() as conn, conn.transaction():
            invoice_ids = await resume_pending_auth(conn, provider, account)
            for invoice_id in invoice_ids:
                await enqueue(conn, invoice_id, DELIVER_STEP)
    finally:
        await pool.close()
    return len(invoice_ids)


def _portal_test(
    parser: argparse.ArgumentParser,
    directory: StoreDirectory,
    cfg: Config,
    key: str,
    sample_path: Path,
    helper_command: Sequence[str],
) -> int:
    """Run the named portal on one sample invoice and print the outcome (S10.5)."""
    if key not in directory.portals:
        parser.error(f"unknown portal key {key!r}")
    invoice = _load_sample(parser, sample_path)
    store = directory.stores.get(invoice.ruc_emisor)
    if store is None:
        parser.error("the sample's ruc_emisor has no store file")
    test_directory = directory.model_copy(
        update={
            "portals": {
                **directory.portals,
                key: directory.portals[key].model_copy(update={"status": "active"}),
            },
            "stores": {
                **directory.stores,
                invoice.ruc_emisor: store.model_copy(update={"portal": key}),
            },
        }
    )
    fetcher = PortalFetcher(
        test_directory,
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
        helper_command=helper_command,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = asyncio.run(fetcher.fetch(invoice, Path(tmp_dir)))
    _print_result(result)
    return 0 if result.ok else 1


def _load_sample(parser: argparse.ArgumentParser, path: Path) -> FetchInvoice:
    """Read the sample YAML into ``FetchInvoice``; an error names fields, never values."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        parser.error("the sample file is not readable YAML")
    if not isinstance(document, dict):
        parser.error("the sample must be a YAML mapping of invoice fields")
    try:
        return _SAMPLE.validate_python(document)
    except ValidationError as error:
        parser.error(f"invalid sample: {_error_fields(error)}")


def _error_fields(error: ValidationError) -> str:
    """Name the fields pydantic rejected, never their values (S10.5)."""
    names = sorted({str(part) for item in error.errors() for part in item["loc"]})
    return ", ".join(names) or "unknown fields"


def _print_result(result: FetchResult) -> None:
    """Write the reason, file sizes and XML verdict, never a value or path (R-9.16)."""
    lines = [f"reason: {result.reason.value}"]
    lines.extend(f"{file.kind} {file.size} bytes" for file in result.files)
    lines.append(f"xml matches: {_XML_VERDICT.get(result.reason, 'n/a')}")
    sys.stdout.write("\n".join(lines) + "\n")
