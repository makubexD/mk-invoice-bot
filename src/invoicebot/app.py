"""FastAPI application: the health endpoint and the runtime wiring at startup.

With no config or secrets file the app serves ``/health`` only. With both, the
lifespan loads them, mounts the WhatsApp webhook and runs the job worker and
the outbox sender as supervised background tasks until shutdown.
"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import cast

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.auth.google_oauth import GoogleTokens
from invoicebot.auth.providers import (
    GRAPH_MODE,
    build_token_provider,
    google_accounts,
    google_token_path,
    scopes_for,
)
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.client import REQUEST_TIMEOUT_SECONDS, WhatsAppClient
from invoicebot.channels.whatsapp.media import WhatsAppMediaSource
from invoicebot.channels.whatsapp.transport import make_transport
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import (
    DEFAULT_RETRY_SCHEDULE,
    EMAIL_INBOX_FETCHER,
    Config,
    Flow,
    GoogleDriveSink,
    GoogleSheetsSink,
    Mail,
    MailerSink,
    OneDriveFolderSink,
    StoreDirectory,
    WhatsAppChannel,
)
from invoicebot.extract.parsing_rules import load_rules
from invoicebot.fetchers.base import Fetcher, FetchInvoice
from invoicebot.fetchers.email_inbox import EmailInboxFetcher
from invoicebot.fetchers.graph_mail import GraphMailbox
from invoicebot.fetchers.portal import PortalFetcher
from invoicebot.fetchers.qr_url import QR_URL_FETCHER, QrUrlFetcher, allowed_hosts
from invoicebot.github.app_token import GitHubApp
from invoicebot.github.transport import GitHubHooks, make_github_transport
from invoicebot.logging import configure_logging
from invoicebot.net.guard import SystemResolver
from invoicebot.net.http import GuardedTransport
from invoicebot.ops.backup import BackupError, BackupJob, BackupReason, run_backup
from invoicebot.ops.daily import DailyTask, daily_forever
from invoicebot.ops.selfcheck import disk_used_pct, selfcheck_forever
from invoicebot.pipeline.commands import PaymentFlow
from invoicebot.pipeline.deliver import (
    DELIVER_STEP,
    DeliverContext,
    make_deliver_handler,
    make_resend_handler,
)
from invoicebot.pipeline.deliver_dispatch import make_deliver_dispatch
from invoicebot.pipeline.deliver_google import GoogleDeliverContext, make_google_deliver_handler
from invoicebot.pipeline.extract import ROUTE_STEP, make_extract_handler
from invoicebot.pipeline.fetch import FETCH_STEP, PORTAL_FETCHER, Schedule, make_fetch_handler
from invoicebot.pipeline.folder_status import make_folder_status_handler
from invoicebot.pipeline.gaps import GapContext, daily_digest, issue_opened, writes_today
from invoicebot.pipeline.handlers import EXTRACT_STEP, MEGABYTE, make_download_handler
from invoicebot.pipeline.intake import DOWNLOAD_STEP
from invoicebot.pipeline.laptop import check_overdue, poll_receipts
from invoicebot.pipeline.manual_message import portal_for
from invoicebot.pipeline.outgoing import queue_rate_limited_alert, queue_token_error_alert
from invoicebot.pipeline.park import PARK_SWEEP_S, park_stale_questions
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.steps import ATTACH_STEP, FOLDER_STATUS_STEP, SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import make_sunat_recheck_handler, skip_recheck
from invoicebot.pipeline.teach import TeachSetup
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.outbox import OutboxSender, Transport
from invoicebot.queue.worker import Handler, Worker
from invoicebot.routing.router import compile_rules
from invoicebot.rules.messages_es import BACKUP_REASON_DESC, MESSAGES
from invoicebot.secrets import Secrets, SecretStore, assert_no_plain_secrets, load_identity
from invoicebot.settings import Settings
from invoicebot.sinks.base import Drive
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.sinks.mailer import DryRun, GraphSend, LaptopOutlook, mail_modes
from invoicebot.sinks.mailer_graph import GraphMailer
from invoicebot.sinks.onedrive import GraphDrive
from invoicebot.store.db import Pool, create_pool, ping
from invoicebot.store.retention import prune
from invoicebot.sunat import token as sunat

logger = logging.getLogger(__name__)

DbPing = Callable[[], Awaitable[bool]]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
TaskFactory = Callable[[], Awaitable[None]]
#: Builds one flow's instance of a fetcher, or ``None`` when the flow cannot use it (S7.1).
_FetcherBuilder = Callable[[str, Flow], Fetcher | None]

#: Secrets the WhatsApp channel declares (R-9.10).
WHATSAPP_ADAPTER = "whatsapp"
WHATSAPP_SECRETS = frozenset(
    {"whatsapp_token", "whatsapp_app_secret", "whatsapp_verify_token", "sender_hash_key"}
)
#: The Microsoft account flow B files through (R-7.1); config pins it to ``personal``.
MICROSOFT_ACCOUNT = "personal"
#: The bot's own mailbox, which sends the ``inbox_rule`` mail (R-7.7, D108).
BOT_ACCOUNT = "bot"
#: The adapter and mail mode that hands the mail to the laptop agent (R-7.14, D108).
LAPTOP_ADAPTER = "laptop_outlook"
#: The age-store secret the laptop job's HMAC is keyed with (R-7.14).
LAPTOP_AGENT_KEY = "laptop_agent_key"
#: The GitHub gap-issue transport and the App private key it signs JWTs with (R-11.5, R-11.10).
GITHUB_ADAPTER = "github"
GITHUB_APP_PRIVATE_KEY = "github_app_private_key"
#: The adapter and secret the Google OAuth clients sign in with (R-9.10, D128).
GOOGLE_OAUTH_ADAPTER = "google_oauth"
GOOGLE_CLIENT_SECRET = "google_client_secret"  # noqa: S105 - the age-store key's name, not a value
#: The sink kinds the deliver dispatcher picks a flow's deliverer by (D132).
GOOGLE_DRIVE_KIND = "google_drive"
ONEDRIVE_FOLDER_KIND = "onedrive_folder"
#: Pause between two outbox passes.
OUTBOX_INTERVAL_S = 2.0
#: Pause between two laptop sweeps: receipts first, then the overdue alerts (R-7.8, R-7.14).
LAPTOP_SWEEP_S = 300.0
#: Pause before a background task that died is started again.
RESTART_DELAY_S = 5.0
#: The only backup target this build uploads to: the personal OneDrive (D119).
BACKUP_TARGET_KIND = "onedrive"
#: The dump command; the connection details reach it through the child's environment (D119).
PG_DUMP_CMD = ("pg_dump",)
#: BR-O04's alert ceiling: at most one backup-failure alert per twelve hours.
BACKUP_ALERT_EVERY = timedelta(hours=12)
#: Retention runs half an hour after the backup, so it prunes what the dump kept (S15.1).
RETENTION_DELAY = timedelta(minutes=30)
#: The rule id and rate-limit key of the backup-failure alert (BR-O04).
RULE_BACKUP_ALERT = "BR-O04"


@dataclass(frozen=True)
class DeliverHandlers:
    """Flow B's deliver handlers and the laptop sweep sharing one drive (R-7.1, R-7.14)."""

    deliver: Handler
    folder_status: Handler
    resends: Mapping[str, Handler]
    #: None when no flow lists ``laptop_outlook``.
    laptop: TaskFactory | None


async def _no_db_ping() -> bool:
    """Database ping used when DATABASE_URL is not configured."""
    return False


def _log_config_reload_request() -> None:
    """Acknowledge SIGHUP; the actual config reload arrives in a later phase."""
    logger.info("config reload requested (SIGHUP)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool, install SIGHUP and run the runtime until shutdown."""
    settings = Settings()
    pool = create_pool(settings.database_url) if settings.database_url else None
    if pool is not None:
        # Do not block startup when the database is down; /health reports it.
        await pool.open(wait=False)
        app.state.db_ping = partial(ping, pool)

    loop = asyncio.get_running_loop()
    sighup_installed = False
    try:
        loop.add_signal_handler(signal.SIGHUP, _log_config_reload_request)
        sighup_installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Platforms or threads without signal support still start normally.
        pass

    try:
        async with _runtime(app, settings, pool):
            yield
    finally:
        if sighup_installed:
            loop.remove_signal_handler(signal.SIGHUP)
        if pool is not None:
            await pool.close()


@asynccontextmanager
async def _runtime(app: FastAPI, settings: Settings, pool: Pool | None) -> AsyncIterator[None]:
    """Wire the WhatsApp channel when config, secrets and a database are present."""
    if not settings.runtime_files_present():
        logger.warning("no config or secrets file: serving /health only")
        yield
        return
    if pool is None:
        logger.error("DATABASE_URL is not set: serving /health only")
        yield
        return
    assert_no_plain_secrets(os.environ)
    cfg = load_config(settings.config_path)
    directory = load_directory(settings.config_path.parent)
    # Fail fast: a missing or invalid rules file stops startup (S3.6).
    load_rules(settings.parsing_rules_path)
    rules = compile_rules(cfg.routing)
    store = _load_secrets(settings)
    configure_logging(store.values())
    secrets = store.scoped(WHATSAPP_ADAPTER, set(WHATSAPP_SECRETS))
    channel = _whatsapp_channel(cfg)
    if channel is None or not secrets.enabled:
        # Fail closed: without its secrets or config the webhook is not mounted.
        logger.error("whatsapp channel disabled: missing %s", sorted(secrets.missing) or ["channel config"])
        yield
        return
    sunat_secrets = _validation_secrets(cfg, store)
    if sunat_secrets is not None and not sunat_secrets.enabled:
        # Fail closed (D124): an enabled API without its secrets must not start, rather
        # than answer every invoice BR-V04 for a configuration mistake.
        logger.error("sunat validation disabled: missing %s", sorted(sunat_secrets.missing))
        yield
        return
    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http,
        httpx.AsyncClient(
            transport=GuardedTransport(
                # A pinned request reaches httpcore by IP, so a pooled connection would carry an
                # earlier host's TLS check to another host on the same IP (R-5.10). HTTP/2 must
                # stay off for the same reason: one h2 connection multiplexes hosts on an IP.
                httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0)),
                SystemResolver(),
            ),
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as guarded_http,
    ):
        client = WhatsAppClient(
            http,
            phone_number_id=channel.phone_number_id,
            graph_api_version=channel.graph_api_version,
            token=secrets.get("whatsapp_token"),
        )
        app.include_router(make_router(_webhook_deps(cfg, pool, secrets)))
        handlers = {
            DOWNLOAD_STEP: make_download_handler(pool, client, settings.data_dir, cfg.limits.media),
            EXTRACT_STEP: make_extract_handler(
                pool,
                cfg.extraction,
                cfg.limits.media,
                settings.parsing_rules_path,
                GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps),
            ),
            ROUTE_STEP: make_route_handler(pool, cfg, directory, rules),
            FETCH_STEP: make_fetch_handler(
                pool,
                cfg,
                directory,
                _fetch_registries(settings, cfg, directory, guarded_http, http),
                settings.data_dir,
                schedules=_fetch_schedules(directory),
            ),
            ATTACH_STEP: make_attach_handler(
                pool,
                cfg,
                {WHATSAPP_ADAPTER: WhatsAppMediaSource(client)},
                settings.data_dir,
                cfg.limits.media,
            ),
        }
        handlers.update(_validation_handlers(cfg, pool, http, sunat_secrets))
        deliver = _deliver_handlers(settings, cfg, directory, http, pool, store)
        handlers.update(_deliver_registry(settings, cfg, deliver, http, pool, store))
        worker = Worker(pool, handlers)
        numbers = {name: profile.whatsapp for name, profile in cfg.profiles.items() if profile.whatsapp}
        transports = {
            WHATSAPP_ADAPTER: make_transport(
                client,
                pool,
                numbers=numbers,
                templates=channel.templates,
                language=channel.template_language,
                on_token_rejected=partial(_alert_channel_token, pool, cfg, "WhatsApp"),
            ),
            **_github_transports(cfg, store, http, pool),
        }
        sender = OutboxSender(pool, transports)
        drive = _personal_drive(settings, cfg, http)
        daily_tasks = _daily_tasks(
            cfg,
            partial(_run_backup, settings, cfg, pool, drive),
            partial(_run_retention, pool, cfg),
            partial(daily_digest, pool, cfg.profiles, cfg.gaps),
        )
        background: dict[str, TaskFactory] = {
            "worker": worker.run,
            "outbox": partial(_send_forever, sender),
            "park": partial(_park_forever, pool, cfg.limits.question_timeout_h),
        }
        background["daily"] = partial(daily_forever, pool, cfg.owner.timezone, daily_tasks)
        background["selfcheck"] = partial(
            selfcheck_forever, pool, cfg.profiles, cfg.ops.self_alerts, settings.data_dir, disk_used_pct
        )
        if deliver is not None and deliver.laptop is not None:
            background["laptop"] = deliver.laptop
        async with _background(background):
            yield


def _webhook_deps(cfg: Config, pool: Pool, secrets: Secrets) -> WebhookDeps:
    """Collect what the webhook routes need from the config and the channel's secrets."""
    return WebhookDeps(
        pool=pool,
        index=ProfileIndex.from_config(cfg),
        owner_profile=cfg.owner.profile,
        admin_profiles=frozenset(name for name, profile in cfg.profiles.items() if profile.admin),
        laptop_flows=_laptop_flow_ids(cfg),
        timezone=cfg.owner.timezone,
        app_secret=secrets.get("whatsapp_app_secret"),
        verify_token=secrets.get("whatsapp_verify_token"),
        sender_hash_key=secrets.get("sender_hash_key"),
        max_queued_per_sender=cfg.limits.max_queued_per_sender,
        media_max_mb=cfg.limits.media.max_bytes.image // MEGABYTE,
        payment_flow=_payment_flow(cfg),
        teach=_teach_setup(cfg),
    )


def _teach_setup(cfg: Config) -> TeachSetup | None:
    """Return the teaching setup, or None when chat teaching is off (R-11.4, BR-F02)."""
    if not cfg.gaps.teach_via_chat:
        return None
    return TeachSetup(
        profiles=cfg.profiles,
        resolver=SystemResolver(),
        github_repo=cfg.gaps.github.repo if cfg.gaps.github.enabled else None,
    )


def _payment_flow(cfg: Config) -> PaymentFlow | None:
    """Return the first flow that tracks payments, or None when no flow opts in (R-7.12)."""
    for name, flow in cfg.flows.items():
        reimbursement = flow.reimbursement
        if reimbursement is not None and reimbursement.track_payments:
            return PaymentFlow(flow_id=name, cap=reimbursement.cap)
    return None


def _load_secrets(settings: Settings) -> SecretStore:
    """Decrypt the secrets file, failing fast when the identity is not configured."""
    if settings.secrets_path is None or settings.age_identity_path is None:
        raise ValueError("SECRETS_PATH and AGE_IDENTITY_PATH must both be set")
    return SecretStore.load(settings.secrets_path, settings.age_identity_path)


def _whatsapp_channel(cfg: Config) -> WhatsAppChannel | None:
    """Return the configured WhatsApp channel, if any."""
    return next((channel for channel in cfg.channels if isinstance(channel, WhatsAppChannel)), None)


def _validation_secrets(cfg: Config, store: SecretStore) -> Secrets | None:
    """Return the SUNAT validez secrets, or None when validation is off (D124).

    A disabled API never reads the secrets. An enabled one still returns its view when
    keys are missing, so the runtime can refuse to start instead of asking SUNAT with
    nothing (D124).
    """
    if not cfg.sunat.validez_api.enabled:
        return None
    return store.scoped(sunat.ADAPTER, set(sunat.SECRETS))


def _validation_handlers(
    cfg: Config, pool: Pool, http: httpx.AsyncClient, secrets: Secrets | None
) -> dict[str, Handler]:
    """Return the validation handlers for both steps, asking SUNAT only with secrets (D124).

    ``secrets is None`` means validation is off: the validate step only sends the routed
    summary and moves the invoice on. A recheck needs the API's credentials, so with
    validation off the step is still registered but ends quietly with
    :func:`skip_recheck`, rather than a queued recheck dying as an unknown step and
    failing the invoice (D125).
    """
    recheck = skip_recheck if secrets is None else make_sunat_recheck_handler(pool, http, secrets, cfg)
    return {VALIDATE_STEP: make_validate_handler(pool, http, secrets, cfg), SUNAT_RECHECK_STEP: recheck}


def _deliver_handlers(
    settings: Settings,
    cfg: Config,
    directory: StoreDirectory,
    http: httpx.AsyncClient,
    pool: Pool,
    store: SecretStore,
) -> DeliverHandlers | None:
    """Build flow B's deliver handlers and laptop sweep, or None without OneDrive (R-7.1, D109).

    The provider only reads the age identity here; nothing signs in or calls Graph until
    the first delivery, where a missing cache surfaces as BR-D01 (R-7.9). Every handler
    and the laptop sweep share one drive and one mode mapping, so the folder the delivery
    filed is the one folder_status moves and a resend reuses. A flow that lists
    ``laptop_outlook`` needs its agent key, so a missing one stops startup (R-7.14).
    """
    if not any(isinstance(sink, OneDriveFolderSink) for flow in cfg.flows.values() for sink in flow.sinks):
        return None
    provider = build_token_provider(settings, cfg, MICROSOFT_ACCOUNT, scopes_for(cfg, MICROSOFT_ACCOUNT))
    drive = GraphDrive(http, provider, cfg.microsoft.timeout_s, account=MICROSOFT_ACCOUNT)
    mailer = GraphMailer(http, provider, cfg.microsoft.timeout_s, account=MICROSOFT_ACCOUNT)
    bot_provider = build_token_provider(settings, cfg, BOT_ACCOUNT, scopes_for(cfg, BOT_ACCOUNT))
    bot_mailer = GraphMailer(http, bot_provider, cfg.microsoft.timeout_s, account=BOT_ACCOUNT)
    laptop_flows = _laptop_flow_ids(cfg)
    found = _laptop_root_and_key(cfg, store)
    laptop: LaptopOutlook | None = None
    laptop_task: TaskFactory | None = None
    if found is not None:
        root, key = found
        laptop = LaptopOutlook(drive, root, key, partial(datetime.now, UTC))
        laptop_task = partial(_laptop_forever, pool, drive, root, key, cfg.mail, laptop_flows)
    modes = mail_modes(cfg, DryRun(settings.data_dir), GraphSend(mailer), bot_mailer, laptop)
    context = DeliverContext(pool, cfg, directory, drive, modes, settings.data_dir, laptop_flows=laptop_flows)
    return DeliverHandlers(
        deliver=make_deliver_handler(context),
        folder_status=make_folder_status_handler(pool, cfg, drive),
        resends=make_resend_handler(context),
        laptop=laptop_task,
    )


def _deliver_registry(
    settings: Settings,
    cfg: Config,
    deliver: DeliverHandlers | None,
    http: httpx.AsyncClient,
    pool: Pool,
    store: SecretStore,
) -> dict[str, Handler]:
    """Return the delivery steps each flow's sink kind selects (D132).

    The ``deliver`` step dispatches to the handler of the invoice's flow. Flow B's folder
    status and resends stay registered as before, only when a OneDrive sink exists.
    """
    by_kind: dict[str, Handler] = {}
    google = _google_deliver_handler(cfg, http, pool, _google_tokens(settings, cfg, http, store))
    if google is not None:
        by_kind[GOOGLE_DRIVE_KIND] = google
    handlers: dict[str, Handler] = {}
    if deliver is not None:
        by_kind[ONEDRIVE_FOLDER_KIND] = deliver.deliver
        handlers[FOLDER_STATUS_STEP] = deliver.folder_status
        handlers.update(deliver.resends)
    by_flow = _deliverers_by_flow(cfg, by_kind)
    if by_flow:
        handlers[DELIVER_STEP] = make_deliver_dispatch(pool, by_flow)
    return handlers


def _google_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the Google OAuth clients read their secret through (R-9.10)."""
    return store.scoped(GOOGLE_OAUTH_ADAPTER, {GOOGLE_CLIENT_SECRET})


def _google_tokens(
    settings: Settings,
    cfg: Config,
    http: httpx.AsyncClient,
    store: SecretStore,
) -> dict[str, GoogleTokens]:
    """Build one token provider per Google account, or none when Google is off (R-6.5, D128).

    Every account shares the one client secret and the one age identity. With ``google``
    config set, a missing ``google_client_secret`` or ``AGE_IDENTITY_PATH`` is a
    configuration mistake, so it fails at startup rather than on the first delivery
    (R-9.9, R-9.10). The secret never appears in the message.
    """
    google = cfg.google
    if google is None:
        return {}
    secrets = _google_secrets(store)
    if not secrets.enabled:
        raise ValueError(
            f"google config needs the missing secret(s) {', '.join(sorted(secrets.missing))} (R-9.9)"
        )
    if settings.age_identity_path is None:
        raise ValueError("google config needs AGE_IDENTITY_PATH to decrypt the refresh tokens (D128)")
    client_secret = secrets.get(GOOGLE_CLIENT_SECRET)
    identity = load_identity(settings.age_identity_path)
    return {
        account: GoogleTokens(
            http,
            google.client_id,
            client_secret,
            account,
            google_token_path(settings.data_dir, account),
            identity,
            google.timeout_s,
        )
        for account in google_accounts(cfg)
    }


def _google_deliver_handler(
    cfg: Config,
    http: httpx.AsyncClient,
    pool: Pool,
    tokens: Mapping[str, GoogleTokens],
) -> Handler | None:
    """Build flow A's deliver handler, or None when no flow files in Google Drive (R-6.1, D129).

    Each sink calls Google with its own account's token provider. A Google sink without
    ``google`` config is a configuration mistake, so it fails at startup rather than on
    the first delivery (D128).
    """
    sinks = _google_sinks(cfg)
    if sinks is None:
        return None
    if cfg.google is None:
        raise ValueError("a google_drive sink needs the google config (D128)")
    drive_sink, sheets_sink = sinks
    drive = GoogleDrive(http, tokens[drive_sink.account], cfg.google.timeout_s, account=drive_sink.account)
    sheets = GoogleSheets(
        http,
        tokens[sheets_sink.account],
        cfg.google.timeout_s,
        account=sheets_sink.account,
        max_rows=cfg.google.max_rows,
    )
    return make_google_deliver_handler(GoogleDeliverContext(pool, cfg, drive, sheets, cfg.profiles))


def _google_sinks(cfg: Config) -> tuple[GoogleDriveSink, GoogleSheetsSink] | None:
    """Return the first flow's Drive sink and its Sheets sink, or None without a Drive sink (R-6.1)."""
    for flow in cfg.flows.values():
        drive = next((sink for sink in flow.sinks if isinstance(sink, GoogleDriveSink)), None)
        if drive is None:
            continue
        sheets = next((sink for sink in flow.sinks if isinstance(sink, GoogleSheetsSink)), None)
        if sheets is None:
            raise ValueError("a flow with a google_drive sink needs a google_sheets sink (R-6.2)")
        return drive, sheets
    return None


def _deliverers_by_flow(cfg: Config, by_kind: Mapping[str, Handler]) -> dict[str, Handler]:
    """Return each flow's deliverer by flow name, chosen from its first sink's kind (D132)."""
    deliverers: dict[str, Handler] = {}
    for name, flow in cfg.flows.items():
        first = next(iter(flow.sinks), None)
        handler = None if first is None else by_kind.get(first.kind)
        if handler is not None:
            deliverers[name] = handler
    return deliverers


def _laptop_flow(cfg: Config) -> Flow | None:
    """Return the first flow that lists ``laptop_outlook``, or None when none does (R-7.14)."""
    return next((flow for flow in cfg.flows.values() if _lists_laptop(flow)), None)


def _laptop_root_and_key(cfg: Config, store: SecretStore) -> tuple[str, bytes] | None:
    """Return the laptop flow's OneDrive root and agent key, or None when no flow lists it (R-7.14).

    The flow that lists ``laptop_outlook`` names the personal OneDrive root that holds
    the agent's ``_outbox``; the config validator guarantees it has a folder sink. The
    HMAC key comes from the age store, so a missing secret raises at startup (R-9.9).
    """
    flow = _laptop_flow(cfg)
    if flow is None:
        return None
    root = _laptop_root(flow)
    key = store.scoped(LAPTOP_ADAPTER, {LAPTOP_AGENT_KEY}).get(LAPTOP_AGENT_KEY).encode()
    return root, key


def _github_transports(
    cfg: Config, store: SecretStore, http: httpx.AsyncClient, pool: Pool
) -> dict[str, Transport]:
    """Return the GitHub gap transport, or none when gaps stay local (R-11.5, R-11.10).

    A disabled transport reads no secret and asks the store for nothing. With it
    enabled, the App's private key comes from the age store, so a missing one
    stops startup (R-9.9); the hooks read the daily write budget, report an
    opened issue and raise BR-D05's token alert.
    """
    github = cfg.gaps.github
    if not github.enabled:
        return {}
    private_key_pem = store.scoped(GITHUB_ADAPTER, {GITHUB_APP_PRIVATE_KEY}).get(GITHUB_APP_PRIVATE_KEY)
    app = GitHubApp(
        http,
        app_id=github.app_id,
        installation_id=github.installation_id,
        private_key_pem=private_key_pem,
        timeout_s=REQUEST_TIMEOUT_SECONDS,
    )
    hooks = GitHubHooks(
        writes_today=partial(writes_today, pool, cfg.owner.timezone),
        on_issue=partial(issue_opened, pool, cfg.profiles, cfg.gaps),
        on_token_error=partial(_alert_token_error, pool, cfg),
    )
    return {
        GITHUB_ADAPTER: make_github_transport(
            app,
            http,
            github.repo,
            github.labels,
            budget=github.daily_write_budget,
            hooks=hooks,
        )
    }


async def _alert_token_error(pool: Pool, cfg: Config, reason: str) -> None:
    """Queue BR-D05's rate-limited owner alert for a rejected installation token (BR-D05)."""
    async with pool.connection() as conn, conn.transaction():
        await queue_token_error_alert(conn, cfg.profiles, cfg.gaps.github.repo, reason)


async def _alert_channel_token(pool: Pool, cfg: Config, channel: str) -> None:
    """Queue BR-O03's rate-limited owner alert for a rejected channel token (BR-O03)."""
    logger.error("channel token rejected", extra={"channel": channel})
    # shortcut: the alert goes out through the same WhatsApp channel whose token
    # failed, so it waits in the outbox until the token works; the ERROR log is the
    # only immediate signal until a second channel exists.
    async with pool.connection() as conn, conn.transaction():
        await queue_rate_limited_alert(
            conn,
            cfg.profiles,
            f"BR-O03:{channel.lower()}",
            timedelta(hours=cfg.ops.self_alerts.realert_hours),
            "BR-O03",
            MESSAGES["BR-O03"].format(channel=channel),
        )


def _daily_tasks(
    cfg: Config,
    backup: Callable[[], Awaitable[None]],
    retention: Callable[[], Awaitable[None]],
    digest: Callable[[], Awaitable[None]],
) -> list[DailyTask]:
    """Build the backup, retention and digest tasks, failing on an unknown target (R-9.11).

    Retention runs ``RETENTION_DELAY`` after the backup; the sum is taken on a
    ``date.min`` base so a late backup wraps to the next day's early time.
    """
    kind = cfg.ops.backup.target.kind
    if kind != BACKUP_TARGET_KIND:
        raise ValueError(
            f"unsupported backup target kind {kind!r}: only {BACKUP_TARGET_KIND!r} is implemented"
        )
    backup_at = cfg.ops.backup.at
    retention_at = (datetime.combine(date.min, backup_at) + RETENTION_DELAY).time()
    return [
        DailyTask(key="backup", at=backup_at, run=backup),
        DailyTask(key="retention", at=retention_at, run=retention),
        DailyTask(key="digest", at=cfg.gaps.digest_at, run=digest),
    ]


def _personal_drive(settings: Settings, cfg: Config, http: httpx.AsyncClient) -> GraphDrive:
    """Build the personal OneDrive drive the backup uploads through (D119)."""
    provider = build_token_provider(settings, cfg, MICROSOFT_ACCOUNT, scopes_for(cfg, MICROSOFT_ACCOUNT))
    return GraphDrive(http, provider, cfg.microsoft.timeout_s, account=MICROSOFT_ACCOUNT)


def _backup_job(settings: Settings, cfg: Config) -> BackupJob:
    """Build the nightly backup's inputs; the database URL is required (D119)."""
    database_url = settings.database_url
    if database_url is None:
        raise RuntimeError("the backup needs DATABASE_URL")
    backup = cfg.ops.backup
    return BackupJob(
        cmd=PG_DUMP_CMD,
        database_url=database_url,
        config_dir=settings.config_path.parent,
        excluded_paths=tuple(
            path for path in (settings.secrets_path, settings.age_identity_path) if path is not None
        ),
        recipient=backup.age_recipient,
        target=backup.target.path,
        max_bytes=backup.max_mb * 1024 * 1024,
    )


async def _owner_today(pool: Pool, timezone: str) -> date:
    """Return the owner's local date from the database clock, for the artifact names (D119)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT (now() AT TIME ZONE %s)::date yielded no row")
    return cast(date, row[0])


async def _run_backup(settings: Settings, cfg: Config, pool: Pool, drive: Drive) -> None:
    """Back up once, alerting BR-O04 by stage instead of retrying or raising (BR-O04)."""
    today = await _owner_today(pool, cfg.owner.timezone)
    try:
        await run_backup(_backup_job(settings, cfg), drive, today)
    except BackupError as error:
        logger.warning("backup failed", extra={"reason": error.reason})
        await _alert_backup_failure(pool, cfg, error.reason)


async def _alert_backup_failure(pool: Pool, cfg: Config, reason: BackupReason) -> None:
    """Queue BR-O04's rate-limited admin alert for a failed backup stage (BR-O04)."""
    text = MESSAGES[RULE_BACKUP_ALERT].format(reason_desc=BACKUP_REASON_DESC[reason])
    async with pool.connection() as conn, conn.transaction():
        await queue_rate_limited_alert(
            conn, cfg.profiles, RULE_BACKUP_ALERT, BACKUP_ALERT_EVERY, RULE_BACKUP_ALERT, text
        )


async def _run_retention(pool: Pool, cfg: Config) -> None:
    """Apply the retention windows in one transaction and log the counts once (R-9.12)."""
    async with pool.connection() as conn, conn.transaction():
        counts = await prune(conn, cfg.ops.retention_days)
    logger.info("retention pruned", extra={"counts": counts})


def _laptop_flow_ids(cfg: Config) -> frozenset[str]:
    """Return the names of the flows whose mailer sink lists ``laptop_outlook`` (D109)."""
    return frozenset(name for name, flow in cfg.flows.items() if _lists_laptop(flow))


def _lists_laptop(flow: Flow) -> bool:
    """Return whether the flow's mailer sink lists the laptop mode (R-7.14)."""
    return any(isinstance(sink, MailerSink) and LAPTOP_ADAPTER in sink.modes for sink in flow.sinks)


def _laptop_root(flow: Flow) -> str:
    """Return the root of the flow's OneDrive folder sink, which holds ``_outbox`` (R-7.14)."""
    for sink in flow.sinks:
        if isinstance(sink, OneDriveFolderSink):
            return sink.root
    raise RuntimeError("a flow listing laptop_outlook needs a onedrive_folder sink")


def _fetch_registries(
    settings: Settings,
    cfg: Config,
    directory: StoreDirectory,
    guarded_http: httpx.AsyncClient,
    graph_http: httpx.AsyncClient,
) -> dict[str, dict[str, Fetcher]]:
    """Return each flow's fetcher registry, sharing one instance where it can (S7.1, D98)."""
    builders = _fetcher_builders(settings, cfg, directory, guarded_http, graph_http)
    return {name: _flow_registry(name, flow, builders) for name, flow in cfg.flows.items()}


def _fetcher_builders(
    settings: Settings,
    cfg: Config,
    directory: StoreDirectory,
    guarded_http: httpx.AsyncClient,
    graph_http: httpx.AsyncClient,
) -> dict[str, _FetcherBuilder]:
    """Return a builder per fetcher this build runs; adding a fetcher adds an entry (S7.1)."""
    portal = PortalFetcher(directory, cfg.limits.media.max_bytes, cfg.retrieval.portal_timeout_s)
    qr_url = QrUrlFetcher(
        guarded_http,
        allowed_hosts(directory),
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
    )
    return {
        PORTAL_FETCHER: lambda _name, _flow: portal,
        QR_URL_FETCHER: lambda _name, _flow: qr_url,
        EMAIL_INBOX_FETCHER: lambda name, flow: _email_inbox_fetcher(name, flow, settings, cfg, graph_http),
    }


def _fetch_schedules(directory: StoreDirectory) -> Mapping[str, Schedule]:
    """Return each fetcher's retry schedule: its flow's inbox or its store's portal (D65, D98)."""

    def inbox(flow: Flow, _invoice: FetchInvoice) -> tuple[timedelta, ...]:
        inbox_cfg = flow.email_inbox
        return DEFAULT_RETRY_SCHEDULE if inbox_cfg is None else inbox_cfg.retry_schedule

    def portal(_flow: Flow, invoice: FetchInvoice) -> tuple[timedelta, ...]:
        portal_cfg = portal_for(invoice.ruc_emisor, directory)
        return DEFAULT_RETRY_SCHEDULE if portal_cfg is None else portal_cfg.retry_schedule

    return {EMAIL_INBOX_FETCHER: inbox, PORTAL_FETCHER: portal}


def _flow_registry(name: str, flow: Flow, builders: Mapping[str, _FetcherBuilder]) -> dict[str, Fetcher]:
    """Return the fetchers ``flow`` lists that this build can run, in config order (S7.1, D98)."""
    registry: dict[str, Fetcher] = {}
    for fetcher_name in flow.fetchers:
        builder = builders.get(fetcher_name)
        if builder is None:
            continue
        fetcher = builder(name, flow)
        if fetcher is not None:
            registry[fetcher_name] = fetcher
    return registry


def _email_inbox_fetcher(
    name: str,
    flow: Flow,
    settings: Settings,
    cfg: Config,
    http: httpx.AsyncClient,
) -> Fetcher | None:
    """Return the flow's Graph mailbox fetcher, or ``None`` with a warning when it cannot run (D98)."""
    inbox = flow.email_inbox
    if inbox is None:
        reason = "the flow has no email_inbox settings"
    elif inbox.provider != GRAPH_MODE:
        reason = f"provider {inbox.provider} is not supported"
    elif settings.age_identity_path is None:
        reason = "AGE_IDENTITY_PATH is not set"
    else:
        provider = build_token_provider(settings, cfg, inbox.account, scopes_for(cfg, inbox.account))
        mailbox = GraphMailbox(http, provider, inbox.account, cfg.microsoft.timeout_s)
        return EmailInboxFetcher(mailbox, cfg.limits.media.max_bytes)
    logger.warning("email_inbox fetcher disabled", extra={"flow": name, "reason": reason})
    return None


async def _send_forever(sender: OutboxSender) -> None:
    """Deliver due outbox rows every ``OUTBOX_INTERVAL_S`` seconds."""
    while True:
        await sender.send_pending()
        await asyncio.sleep(OUTBOX_INTERVAL_S)


async def _park_forever(pool: Pool, timeout_h: int) -> None:
    """Park unanswered questions every ``PARK_SWEEP_S`` seconds (BR-S08)."""
    while True:
        await park_stale_questions(pool, timeout_h)
        await asyncio.sleep(PARK_SWEEP_S)


async def _laptop_forever(
    pool: Pool, drive: Drive, root: str, key: bytes, mail: Mail, laptop_flows: frozenset[str]
) -> None:
    """Apply signed receipts and alert on overdue jobs every ``LAPTOP_SWEEP_S`` seconds (R-7.8, R-7.14)."""
    while True:
        await poll_receipts(pool, drive, root, key, laptop_flows)
        await check_overdue(pool, drive, root, mail, laptop_flows)
        await asyncio.sleep(LAPTOP_SWEEP_S)


@asynccontextmanager
async def _background(factories: dict[str, TaskFactory]) -> AsyncIterator[None]:
    """Run each factory as a supervised task; cancel and await them all on exit."""
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_supervise(name, factory)) for name, factory in factories.items()]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()


async def _supervise(name: str, factory: TaskFactory) -> None:
    """Run ``factory`` until cancelled, restarting it after a failure or return."""
    while True:
        try:
            await factory()
            logger.error("background task %s returned; restarting", name)
        except Exception:  # top-level boundary: log, wait, restart
            logger.exception("background task %s died; restarting", name)
        await asyncio.sleep(RESTART_DELAY_S)


def create_app(db_ping: DbPing, *, lifespan: Lifespan | None = None) -> FastAPI:
    """Build the application with the given database ping callable."""
    app = FastAPI(lifespan=lifespan)
    app.state.db_ping = db_ping

    @app.get("/health")
    async def health() -> JSONResponse:
        """Report service health, degrading when the database ping fails."""
        try:
            db_ok = await app.state.db_ping()
        except Exception:  # any ping failure means degraded
            db_ok = False
        if db_ok:
            return JSONResponse({"status": "ok", "db": "ok"}, status_code=200)
        return JSONResponse({"status": "degraded", "db": "down"}, status_code=503)

    return app


app = create_app(_no_db_ping, lifespan=lifespan)
