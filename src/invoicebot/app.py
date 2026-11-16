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
from datetime import timedelta
from functools import partial

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.auth.providers import GRAPH_MODE, build_token_provider, scopes_for
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
from invoicebot.logging import configure_logging
from invoicebot.net.guard import SystemResolver
from invoicebot.net.http import GuardedTransport
from invoicebot.pipeline.commands import PaymentFlow
from invoicebot.pipeline.deliver import DELIVER_STEP, make_deliver_handler, make_resend_handler
from invoicebot.pipeline.extract import ROUTE_STEP, make_extract_handler
from invoicebot.pipeline.fetch import FETCH_STEP, PORTAL_FETCHER, Schedule, make_fetch_handler
from invoicebot.pipeline.folder_status import make_folder_status_handler
from invoicebot.pipeline.handlers import EXTRACT_STEP, make_download_handler
from invoicebot.pipeline.intake import DOWNLOAD_STEP
from invoicebot.pipeline.laptop import check_overdue, poll_receipts
from invoicebot.pipeline.manual_message import portal_for
from invoicebot.pipeline.park import PARK_SWEEP_S, park_stale_questions
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.steps import ATTACH_STEP, FOLDER_STATUS_STEP
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.queue.outbox import OutboxSender
from invoicebot.queue.worker import Handler, Worker
from invoicebot.routing.router import compile_rules
from invoicebot.secrets import Secrets, SecretStore, assert_no_plain_secrets
from invoicebot.settings import Settings
from invoicebot.sinks.base import Drive
from invoicebot.sinks.mailer import DryRun, GraphSend, LaptopOutlook, mail_modes
from invoicebot.sinks.mailer_graph import GraphMailer
from invoicebot.sinks.onedrive import GraphDrive
from invoicebot.store.db import Pool, create_pool, ping

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
#: Pause between two outbox passes.
OUTBOX_INTERVAL_S = 2.0
#: Pause between two laptop sweeps: receipts first, then the overdue alerts (R-7.8, R-7.14).
LAPTOP_SWEEP_S = 300.0
#: Pause before a background task that died is started again.
RESTART_DELAY_S = 5.0


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
        deliver = _deliver_handlers(settings, cfg, directory, http, pool, store)
        if deliver is not None:
            handlers[DELIVER_STEP] = deliver.deliver
            handlers[FOLDER_STATUS_STEP] = deliver.folder_status
            handlers.update(deliver.resends)
        worker = Worker(pool, handlers)
        numbers = {name: profile.whatsapp for name, profile in cfg.profiles.items() if profile.whatsapp}
        sender = OutboxSender(
            pool,
            {
                WHATSAPP_ADAPTER: make_transport(
                    client,
                    pool,
                    numbers=numbers,
                    templates=channel.templates,
                    language=channel.template_language,
                )
            },
        )
        background: dict[str, TaskFactory] = {
            "worker": worker.run,
            "outbox": partial(_send_forever, sender),
            "park": partial(_park_forever, pool, cfg.limits.question_timeout_h),
        }
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
        timezone=cfg.owner.timezone,
        app_secret=secrets.get("whatsapp_app_secret"),
        verify_token=secrets.get("whatsapp_verify_token"),
        sender_hash_key=secrets.get("sender_hash_key"),
        max_queued_per_sender=cfg.limits.max_queued_per_sender,
        payment_flow=_payment_flow(cfg),
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
    laptop_flow = _laptop_flow(cfg)
    laptop: LaptopOutlook | None = None
    laptop_task: TaskFactory | None = None
    if laptop_flow is not None:
        root = _laptop_root(laptop_flow)
        key = store.scoped(LAPTOP_ADAPTER, {LAPTOP_AGENT_KEY}).get(LAPTOP_AGENT_KEY).encode()
        laptop = LaptopOutlook(drive, root, key)
        laptop_task = partial(_laptop_forever, pool, drive, root, cfg.mail)
    modes = mail_modes(cfg, DryRun(settings.data_dir), GraphSend(mailer), bot_mailer, laptop)
    return DeliverHandlers(
        deliver=make_deliver_handler(pool, cfg, directory, drive, modes, settings.data_dir),
        folder_status=make_folder_status_handler(pool, cfg, drive),
        resends=make_resend_handler(pool, cfg, directory, drive, modes, settings.data_dir),
        laptop=laptop_task,
    )


def _laptop_flow(cfg: Config) -> Flow | None:
    """Return the first flow that lists ``laptop_outlook``, or None when none does (R-7.14)."""
    return next((flow for flow in cfg.flows.values() if _lists_laptop(flow)), None)


def _laptop_mode(cfg: Config, store: SecretStore, drive: Drive) -> LaptopOutlook | None:
    """Build the laptop mode when a flow lists it, or None when none does (R-7.14, D108).

    The flow that lists ``laptop_outlook`` names the personal OneDrive root that holds
    the agent's ``_outbox``; the config validator guarantees it has a folder sink. The
    HMAC key comes from the age store, so a missing secret raises at startup (R-9.9).
    """
    flow = _laptop_flow(cfg)
    if flow is None:
        return None
    root = _laptop_root(flow)
    key = store.scoped(LAPTOP_ADAPTER, {LAPTOP_AGENT_KEY}).get(LAPTOP_AGENT_KEY).encode()
    return LaptopOutlook(drive, root, key)


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


async def _laptop_forever(pool: Pool, drive: Drive, root: str, mail: Mail) -> None:
    """Apply receipts and alert on overdue jobs every ``LAPTOP_SWEEP_S`` seconds (R-7.8, R-7.14)."""
    while True:
        await poll_receipts(pool, drive, root)
        await check_overdue(pool, drive, root, mail)
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
