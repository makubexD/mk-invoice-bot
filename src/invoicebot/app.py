"""FastAPI application: the health endpoint and the runtime wiring at startup.

With no config or secrets file the app serves ``/health`` only. With both, the
lifespan loads them, mounts the WhatsApp webhook and runs the job worker and
the outbox sender as supervised background tasks until shutdown.
"""

import asyncio
import hmac
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

import httpx
import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.auth.google_oauth import GOOGLE_CLIENT_SECRET, GoogleTokens, google_secrets
from invoicebot.auth.providers import (
    build_token_provider,
    google_accounts,
    google_token_path,
    microsoft_accounts,
    scopes_for,
)
from invoicebot.channels.base import MediaSource
from invoicebot.channels.console import CONSOLE, ConsoleMediaSource, console_channel, console_transport
from invoicebot.config.models import (
    DEFAULT_RETRY_SCHEDULE,
    EMAIL_INBOX_FETCHER,
    GMAIL_PROVIDER,
    GOOGLE_DRIVE_KIND,
    GOOGLE_SHEETS_KIND,
    GRAPH_MODE,
    ONEDRIVE_FOLDER_KIND,
    PERSONAL_MICROSOFT_ACCOUNT,
    Config,
    Flow,
    FlowInbox,
    GoogleDriveSink,
    GoogleSheetsSink,
    Mail,
    OneDriveFolderSink,
    StoreDirectory,
    SunatSolSite,
    enabled_chat_channels,
)
from invoicebot.fetchers.base import Fetcher, FetchInvoice
from invoicebot.fetchers.email_inbox import EmailInboxFetcher, Mailbox
from invoicebot.fetchers.gmail import GmailMailbox
from invoicebot.fetchers.graph_mail import GraphMailbox
from invoicebot.fetchers.portal import DEFAULT_HELPER_COMMAND, PortalFetcher
from invoicebot.fetchers.qr_url import QR_URL_FETCHER, QrUrlFetcher, allowed_hosts
from invoicebot.fetchers.sunat_sol import SECRETS, SOL_HELPER_COMMAND, SunatSolFetcher
from invoicebot.github.app_token import GitHubApp, GitHubTokenError
from invoicebot.github.transport import GitHubHooks, TokenSource, make_github_transport
from invoicebot.logging import configure_logging
from invoicebot.net.guard import SystemResolver
from invoicebot.net.http import GuardedTransport
from invoicebot.ops.backup import BackupError, BackupJob, BackupReason, pg_environment, run_backup
from invoicebot.ops.daily import DailyTask, daily_forever
from invoicebot.ops.housekeeping import housekeep
from invoicebot.ops.padron_refresh import PadronRefreshError, refresh_padron
from invoicebot.ops.selfcheck import disk_used_pct, selfcheck_forever
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
from invoicebot.pipeline.handlers import EXTRACT_STEP, make_download_handler
from invoicebot.pipeline.intake import DOWNLOAD_STEP
from invoicebot.pipeline.intake_config import (
    LAPTOP_ADAPTER,
    intake_settings,
    laptop_flow_ids,
    lists_laptop,
)
from invoicebot.pipeline.laptop import check_overdue, poll_receipts
from invoicebot.pipeline.manual_message import portal_for
from invoicebot.pipeline.outgoing import queue_rate_limited_alert, queue_token_error_alert
from invoicebot.pipeline.park import PARK_SWEEP_S, park_stale_questions
from invoicebot.pipeline.reevaluate import reevaluate
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.steps import ATTACH_STEP, FOLDER_STATUS_STEP, SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import make_sunat_recheck_handler, skip_recheck
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.outbox import OutboxSender, Transport
from invoicebot.queue.worker import Handler, Worker
from invoicebot.reload import reload_config
from invoicebot.rules.messages_es import BACKUP_REASON_DESC, MESSAGES
from invoicebot.runtime_config import ConfigRejected, RuntimeConfig, load_runtime_config
from invoicebot.sandbox import (
    HelperPrefixes,
    browser_command,
    check_sandbox,
    helper_prefixes,
    hidden_dirs,
    passfile_path,
)
from invoicebot.secrets import Secrets, SecretStore, assert_no_plain_secrets, load_identity
from invoicebot.settings import Settings
from invoicebot.sinks.base import Drive
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.sinks.mailer import DryRun, GraphSend, LaptopOutlook, mail_modes
from invoicebot.sinks.mailer_graph import GraphMailer
from invoicebot.sinks.onedrive import GraphDrive
from invoicebot.store.db import Pool, create_pool, ping
from invoicebot.store.gaps import get_gap
from invoicebot.store.retention import prune
from invoicebot.sunat import token as sunat
from invoicebot.sunat_origins import SUNAT_SOL
from invoicebot.wiring.channels import ChannelBuilder, ChannelWiring, channel_secrets
from invoicebot.wiring.whatsapp import whatsapp_wiring

logger = logging.getLogger(__name__)

DbPing = Callable[[], Awaitable[bool]]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
TaskFactory = Callable[[], Awaitable[None]]
#: Builds one flow's instance of a fetcher, or ``None`` when the flow cannot use it (S7.1).
_FetcherBuilder = Callable[[str, Flow], Fetcher | None]
#: Builds one flow's mailbox and, when it cannot, the reason it is disabled (D98).
_InboxBuilder = Callable[[FlowInbox], tuple[Mailbox | None, str]]

#: Builds each chat channel's wiring, or ``None`` when the channel is not enabled (D174).
CHANNEL_BUILDERS: tuple[ChannelBuilder, ...] = (whatsapp_wiring,)
#: Per-request timeout for the shared HTTP clients and the GitHub App (R-1.4, R-11.10).
HTTP_TIMEOUT_S = 30.0
#: The Microsoft account flow B files through and the backup uploads with (R-7.1, D119).
MICROSOFT_ACCOUNT = PERSONAL_MICROSOFT_ACCOUNT
#: The bot's own mailbox, which sends the ``inbox_rule`` mail (R-7.7, D108).
BOT_ACCOUNT = "bot"
#: The age-store secret the laptop job's HMAC is keyed with (R-7.14).
LAPTOP_AGENT_KEY = "laptop_agent_key"
#: The GitHub gap-issue transport and the App private key it signs JWTs with (R-11.5, R-11.10).
GITHUB_ADAPTER = "github"
GITHUB_APP_PRIVATE_KEY = "github_app_private_key"
#: Pause between two outbox passes.
OUTBOX_INTERVAL_S = 2.0
#: How long each helper wrapper gets to start once at startup (D147).
SANDBOX_CHECK_TIMEOUT_S = 10.0
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
#: BR-O05's alert ceiling: at most one rejected-config alert per hour.
CONFIG_ALERT_EVERY = timedelta(hours=1)
#: Retention runs half an hour after the backup, so it prunes what the dump kept (S15.1).
RETENTION_DELAY = timedelta(minutes=30)
#: The rule id and rate-limit key of the backup-failure alert (BR-O04).
RULE_BACKUP_ALERT = "BR-O04"
#: The rule id and rate-limit key of the rejected-config alert (BR-O05).
RULE_CONFIG_ALERT = "BR-O05"


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool, install SIGHUP and run the runtime until shutdown."""
    settings = Settings()
    pool = create_pool(settings.database_url) if settings.database_url else None
    if pool is not None:
        # Do not block startup when the database is down; /health reports it.
        await pool.open(wait=False)
        app.state.db_ping = partial(ping, pool)

    app.state.reload = None
    loop = asyncio.get_running_loop()
    sighup_installed = False
    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup, app)
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


def _on_sighup(app: FastAPI) -> None:
    """Hand SIGHUP to the runtime's reload, or ignore it when no runtime is wired (R-9.6, D155).

    Health-only mode has no config to reload, so the signal must not end the process.
    """
    reload: Callable[[], None] | None = app.state.reload
    if reload is None:
        logger.info("config reload ignored: no runtime config is loaded")
        return
    reload()


def _load_runtime(settings: Settings) -> RuntimeConfig:
    """Load the runtime config, logging why startup rejects it (R-9.6)."""
    try:
        return load_runtime_config(settings)
    except ConfigRejected as err:
        logger.error("config rejected at startup", extra={"reason": err.reason})
        raise


@asynccontextmanager
async def _runtime(app: FastAPI, settings: Settings, pool: Pool | None) -> AsyncIterator[None]:
    """Wire the enabled chat channels when config, secrets and a database are present."""
    if not settings.runtime_files_present():
        logger.warning("no config or secrets file: serving /health only")
        yield
        return
    if pool is None:
        logger.error("DATABASE_URL is not set: serving /health only")
        yield
        return
    assert_no_plain_secrets(os.environ)
    runtime = _load_runtime(settings)
    cfg = runtime.cfg
    directory = runtime.directory
    rules = runtime.rules
    store = _load_secrets(settings)
    configure_logging(store.values())
    intake_deps = IntakeDeps(pool=pool, intake=intake_settings(cfg))
    async with (
        httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http,
        httpx.AsyncClient(
            transport=GuardedTransport(
                # A pinned request reaches httpcore by IP, so a pooled connection would carry an
                # earlier host's TLS check to another host on the same IP (R-5.10). HTTP/2 must
                # stay off for the same reason: one h2 connection multiplexes hosts on an IP.
                httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0)),
                SystemResolver(),
            ),
            follow_redirects=False,
            timeout=HTTP_TIMEOUT_S,
        ) as guarded_http,
    ):
        wirings = [w for b in CHANNEL_BUILDERS if (w := b(cfg, store, http, intake_deps)) is not None]
        if not wirings:
            # Fail closed (D174): without an enabled chat channel no router is mounted and
            # nothing is started.
            logger.error("no chat channel enabled")
            yield
            return
        sunat_secrets = _validation_secrets(cfg, store)
        if sunat_secrets is not None and not sunat_secrets.enabled:
            # Fail closed (D124): an enabled API without its secrets must not start, rather
            # than answer every invoice BR-V04 for a configuration mistake.
            logger.error("sunat validation disabled: missing %s", sorted(sunat_secrets.missing))
            yield
            return
        prefixes = helper_prefixes(
            settings.helper_sandbox,
            hidden_dirs(
                [
                    settings.config_path,
                    settings.secrets_path,
                    settings.age_identity_path,
                    passfile_path(settings.database_url),
                ]
            ),
            settings.helper_tmp_mb,
            settings.data_dir,
        )
        # Fail fast: a host without the namespaces rolls back instead of running bare helpers (D147).
        await check_sandbox(prefixes, SANDBOX_CHECK_TIMEOUT_S)
        # Mounted only once every fail-closed check passed: a stored message needs workers.
        for wiring in wirings:
            if wiring.router is not None:
                app.include_router(wiring.router)
        # One token source per Google account for the whole process: two instances on the
        # same token file would race on a refresh (D135).
        google_tokens = _google_tokens(settings, cfg, http, store)
        sources = _media_sources(wirings, cfg, settings.data_dir)
        handlers = {
            **_media_handlers(cfg, sources, settings, pool),
            EXTRACT_STEP: make_extract_handler(
                pool,
                cfg.extraction,
                cfg.limits.media,
                settings.parsing_rules_path,
                GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps),
                helper_prefix=prefixes.image,
            ),
            ROUTE_STEP: make_route_handler(pool, cfg, directory, rules),
            FETCH_STEP: make_fetch_handler(
                pool,
                cfg,
                directory,
                _fetch_registries(
                    settings,
                    cfg,
                    directory,
                    guarded_http,
                    _InboxDeps(http, google_tokens),
                    _BrowserDeps(_sol_fetcher(cfg, directory, store, prefixes), prefixes),
                ),
                settings.data_dir,
                schedules=_fetch_schedules(directory),
            ),
        }
        handlers.update(_validation_handlers(cfg, pool, http, sunat_secrets))
        deliver = _deliver_handlers(settings, cfg, directory, http, pool, store)
        handlers.update(_deliver_registry(cfg, deliver, http, pool, google_tokens))
        worker = Worker(pool, handlers)
        github_app = _github_app(cfg, store, http)
        transports: dict[str, Transport] = {
            **{wiring.name: wiring.transport for wiring in wirings},
            **_github_transports(cfg, github_app, http, pool),
            **_console_transports(cfg),
        }
        sender = OutboxSender(pool, transports)
        drive = _personal_drive(settings, cfg, http)
        # Fail fast: a query key the dump cannot pass on would fail every nightly backup,
        # so the URL is checked when the job is wired, not at backup time (D148, R-9.11).
        database_url = settings.database_url
        if database_url is not None:
            pg_environment(database_url)
        daily_tasks = _daily_tasks(
            cfg,
            DailyRuns(
                backup=partial(_run_backup, settings, cfg, pool, drive),
                retention=partial(_run_retention, pool, cfg),
                housekeeping=partial(housekeep, pool, settings.data_dir),
                padron=partial(_run_padron_refresh, pool, guarded_http, cfg, settings.data_dir),
                digest=partial(daily_digest, pool, cfg.profiles, cfg.gaps),
                github_health=_github_health_run(github_app, pool, cfg),
            ),
        )
        background: dict[str, TaskFactory] = {
            "worker": worker.run,
            "outbox": partial(_send_forever, sender),
            "park": partial(_park_forever, pool, cfg.limits.question_timeout_h),
            "reevaluate": partial(_reevaluate_at_start, pool, cfg, directory),
        }
        background["daily"] = partial(daily_forever, pool, cfg.owner.timezone, daily_tasks)
        background["selfcheck"] = partial(
            selfcheck_forever, pool, cfg.profiles, cfg.ops.self_alerts, settings.data_dir, disk_used_pct
        )
        if deliver is not None and deliver.laptop is not None:
            background["laptop"] = deliver.laptop
        async with _reload_ready(app, settings, pool, cfg), _background(background):
            yield


async def _reevaluate_at_start(pool: Pool, cfg: Config, directory: StoreDirectory) -> None:
    """Re-check the gaps once at this start, then idle (D156).

    A reload is a start, so a deploy or config change that fixes a gap is seen here. It runs
    as a supervised task, so a database that is down at startup neither blocks the app nor
    skips the check: the supervisor retries it until it succeeds.
    """
    await reevaluate(pool, cfg, directory)
    await asyncio.Event().wait()


@asynccontextmanager
async def _reload_ready(app: FastAPI, settings: Settings, pool: Pool, cfg: Config) -> AsyncIterator[None]:
    """Accept a SIGHUP config reload while the runtime runs (R-9.6, D155)."""
    reloads: set[asyncio.Task[bool]] = set()
    app.state.reload = partial(_start_config_reload, reloads, settings, pool, cfg)
    try:
        yield
    finally:
        app.state.reload = None


def _start_config_reload(
    reloads: set[asyncio.Task[bool]], settings: Settings, pool: Pool, cfg: Config
) -> None:
    """Start one config reload task, ignoring a SIGHUP while one is running (R-9.6, D155)."""
    if reloads:
        logger.info("config reload already running")
        return
    task = asyncio.create_task(
        reload_config(
            partial(_check_reload_candidate, settings),
            partial(_alert_config_rejected, pool, cfg),
            partial(os.kill, os.getpid(), signal.SIGTERM),
        )
    )
    reloads.add(task)
    task.add_done_callback(reloads.discard)
    task.add_done_callback(_log_reload_failure)


def _check_reload_candidate(settings: Settings) -> RuntimeConfig:
    """Load a candidate config and the secrets it needs, rejecting either problem (R-9.6, D155).

    The config loader alone cannot see a feature switched on without its secret, so the
    candidate is checked against the age store too, before the restart into it.
    """
    runtime = load_runtime_config(settings)
    try:
        store = _load_secrets(settings)
    except (OSError, ValueError, pyrage.DecryptError, pyrage.IdentityError) as err:
        raise ConfigRejected("secrets") from err
    problem = _secret_problem(runtime.cfg, runtime.directory, store)
    if problem is not None:
        raise ConfigRejected(problem)
    return runtime


def _log_reload_failure(task: asyncio.Task[bool]) -> None:
    """Log a reload that raised instead of rejecting, so it never fails silently (R-9.6)."""
    if not task.cancelled() and (err := task.exception()) is not None:
        logger.error("config reload failed", exc_info=err)


def _load_secrets(settings: Settings) -> SecretStore:
    """Decrypt the secrets file, failing fast when the identity is not configured."""
    if settings.secrets_path is None or settings.age_identity_path is None:
        raise ValueError("SECRETS_PATH and AGE_IDENTITY_PATH must both be set")
    return SecretStore.load(settings.secrets_path, settings.age_identity_path)


def _media_sources(wirings: list[ChannelWiring], cfg: Config, data_dir: Path) -> dict[str, MediaSource]:
    """Return each wiring's media source plus the console source when configured (R-1.4, D165)."""
    sources: dict[str, MediaSource] = {
        wiring.name: wiring.media_source for wiring in wirings if wiring.media_source is not None
    }
    if console_channel(cfg) is not None:
        sources[CONSOLE] = ConsoleMediaSource(data_dir)
    return sources


def _media_handlers(
    cfg: Config, sources: Mapping[str, MediaSource], settings: Settings, pool: Pool
) -> dict[str, Handler]:
    """Return the media handlers, built once over one shared source map (R-1.4, D165)."""
    return {
        DOWNLOAD_STEP: make_download_handler(pool, sources, settings.data_dir, cfg.limits.media),
        ATTACH_STEP: make_attach_handler(pool, cfg, sources, settings.data_dir, cfg.limits.media),
    }


def _console_transports(cfg: Config) -> dict[str, Transport]:
    """Return the console outbox transport, or none without a console channel (D165)."""
    if console_channel(cfg) is None:
        return {}
    return {CONSOLE: console_transport()}


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
    # Only a flow that lists ``inbox_rule`` signs the bot mailbox in; a single-account
    # config never pins it (D164).
    bot_mailer: GraphMailer | None = None
    if BOT_ACCOUNT in microsoft_accounts(cfg):
        bot_provider = build_token_provider(settings, cfg, BOT_ACCOUNT, scopes_for(cfg, BOT_ACCOUNT))
        bot_mailer = GraphMailer(http, bot_provider, cfg.microsoft.timeout_s, account=BOT_ACCOUNT)
    laptop_flows = laptop_flow_ids(cfg)
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
    cfg: Config,
    deliver: DeliverHandlers | None,
    http: httpx.AsyncClient,
    pool: Pool,
    tokens: Mapping[str, GoogleTokens],
) -> dict[str, Handler]:
    """Return the delivery steps each flow's sink kind selects (D132).

    The ``deliver`` step dispatches to the handler of the invoice's flow. Flow B's folder
    status and resends stay registered as before, only when a OneDrive sink exists.
    """
    by_kind: dict[str, Handler] = {}
    google = _google_deliver_handler(cfg, http, pool, tokens)
    if google is not None:
        by_kind[GOOGLE_DRIVE_KIND] = google
        by_kind[GOOGLE_SHEETS_KIND] = google
    handlers: dict[str, Handler] = {}
    if deliver is not None:
        by_kind[ONEDRIVE_FOLDER_KIND] = deliver.deliver
        handlers[FOLDER_STATUS_STEP] = deliver.folder_status
        handlers.update(deliver.resends)
    by_flow = _deliverers_by_flow(cfg, by_kind)
    if by_flow:
        handlers[DELIVER_STEP] = make_deliver_dispatch(pool, by_flow)
    return handlers


def _github_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the GitHub App signs its JWTs with (R-9.10)."""
    return store.scoped(GITHUB_ADAPTER, {GITHUB_APP_PRIVATE_KEY})


def _laptop_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the laptop job keys its HMAC with (R-9.10)."""
    return store.scoped(LAPTOP_ADAPTER, {LAPTOP_AGENT_KEY})


def _sol_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the SOL helper signs in with (R-9.10)."""
    return store.scoped(SUNAT_SOL, set(SECRETS))


def _sol_site(cfg: Config, directory: StoreDirectory) -> SunatSolSite | None:
    """Return the SOL site once it is enabled and active, or None until then (S10.1, R-5.2)."""
    site = directory.sunat_sol
    if not cfg.sunat.sol.enabled or site is None or site.status != "active":
        return None
    return site


def _secret_problem(cfg: Config, directory: StoreDirectory, store: SecretStore) -> str | None:
    """Return the first secret the candidate needs but the store lacks (R-9.6, R-9.9, R-9.10, D155, D174).

    Startup keeps its own fail-closed checks; this one lets a reload refuse the same
    candidate before it restarts into a crash. Only the problem code is returned, never
    a key or a value.
    """
    validation = _validation_secrets(cfg, store)
    enabled = enabled_chat_channels(cfg.channels)
    chat_enabled = bool(enabled) or console_channel(cfg) is not None
    failed: dict[str, bool] = {"channels:none": not enabled}
    for builder in CHANNEL_BUILDERS:
        problem = builder.secret_problem(cfg, store)
        if problem is not None:
            failed[problem] = True
    failed.update(
        {
            "secrets:channels": chat_enabled and not channel_secrets(store).enabled,
            "secrets:sunat_validez": validation is not None and not validation.enabled,
            "secrets:google": cfg.google is not None and not google_secrets(store).enabled,
            "secrets:github": cfg.gaps.github.enabled and not _github_secrets(store).enabled,
            "secrets:laptop": _laptop_flow(cfg) is not None and not _laptop_secrets(store).enabled,
            "secrets:sunat_sol": _sol_secrets_unusable(cfg, directory, store),
        }
    )
    return next((code for code, problem in failed.items() if problem), None)


def _sol_secrets_unusable(cfg: Config, directory: StoreDirectory, store: SecretStore) -> bool:
    """Return whether an active SOL site lacks its keys or names another taxpayer (R-9.10)."""
    if _sol_site(cfg, directory) is None:
        return False
    secrets = _sol_secrets(store)
    return not secrets.enabled or not hmac.compare_digest(
        secrets.get("sol_ruc").encode(), cfg.owner.ruc.encode()
    )


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
    secrets = google_secrets(store)
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
    """Build the Google flows' deliver handler, or None when no flow has a Google sink (R-6.1, D129, D144).

    Each sink calls Google with its own account's token provider. A Google sink without
    ``google`` config is a configuration mistake, so it fails at startup rather than on
    the first delivery (D128).
    """
    has_google_sink = any(
        isinstance(sink, (GoogleDriveSink, GoogleSheetsSink))
        for flow in cfg.flows.values()
        for sink in flow.sinks
    )
    if not has_google_sink:
        return None
    if cfg.google is None:
        raise ValueError("a Google sink needs the google config (D128)")
    drives = {
        account: GoogleDrive(http, tokens[account], cfg.google.timeout_s, account=account)
        for account in tokens
    }
    sheets = {
        account: GoogleSheets(
            http, tokens[account], cfg.google.timeout_s, account=account, max_rows=cfg.google.max_rows
        )
        for account in tokens
    }
    return make_google_deliver_handler(GoogleDeliverContext(pool, cfg, drives, sheets, cfg.profiles))


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
    return next((flow for flow in cfg.flows.values() if lists_laptop(flow)), None)


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
    key = _laptop_secrets(store).get(LAPTOP_AGENT_KEY).encode()
    return root, key


def _github_app(cfg: Config, store: SecretStore, http: httpx.AsyncClient) -> GitHubApp | None:
    """Return the GitHub App, or None when gaps stay local (R-11.5, R-11.10).

    A disabled transport reads no secret. With it enabled, the App's private
    key comes from the age store, so a missing one stops startup (R-9.9).
    """
    github = cfg.gaps.github
    if not github.enabled:
        return None
    private_key_pem = _github_secrets(store).get(GITHUB_APP_PRIVATE_KEY)
    return GitHubApp(
        http,
        app_id=github.app_id,
        installation_id=github.installation_id,
        private_key_pem=private_key_pem,
        timeout_s=HTTP_TIMEOUT_S,
    )


def _github_transports(
    cfg: Config, app: GitHubApp | None, http: httpx.AsyncClient, pool: Pool
) -> dict[str, Transport]:
    """Return the GitHub gap transport, or none without a GitHub App (R-11.5, R-11.10).

    The hooks read the daily write budget, report an opened issue and raise
    BR-D05's token alert.
    """
    if app is None:
        return {}
    github = cfg.gaps.github
    hooks = GitHubHooks(
        writes_today=partial(writes_today, pool, cfg.owner.timezone),
        on_issue=partial(issue_opened, pool, cfg.profiles, cfg.gaps),
        on_token_error=partial(_alert_token_error, pool, cfg),
        issue_url=partial(_gap_issue_url, pool),
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


def _github_health_run(
    github_app: GitHubApp | None, pool: Pool, cfg: Config
) -> Callable[[], Awaitable[None]] | None:
    """Return the daily GitHub App health check, or None without a GitHub App (BR-D05)."""
    if github_app is None:
        return None
    return partial(_github_health, github_app, pool, cfg)


async def _github_health(app: TokenSource, pool: Pool, cfg: Config) -> None:
    """Prove the App can mint a token, queueing BR-D05's alert when it cannot (BR-D05)."""
    app.invalidate()
    try:
        await app.token()
    except GitHubTokenError as exc:
        await _alert_token_error(pool, cfg, exc.reason)


async def _alert_token_error(pool: Pool, cfg: Config, reason: str) -> None:
    """Queue BR-D05's rate-limited owner alert for a rejected installation token (BR-D05)."""
    async with pool.connection() as conn, conn.transaction():
        await queue_token_error_alert(conn, cfg.profiles, cfg.gaps.github.repo, reason)


async def _gap_issue_url(pool: Pool, fingerprint: str) -> str | None:
    """Return the GitHub issue URL of the gap, or None while it is still pending (D118)."""
    async with pool.connection() as conn:
        gap = await get_gap(conn, fingerprint)
    return None if gap is None else gap.issue_url


@dataclass(frozen=True)
class DailyRuns:
    """What each daily task runs; ``_daily_tasks`` takes their times from the config (R-9.11)."""

    backup: Callable[[], Awaitable[None]]
    retention: Callable[[], Awaitable[None]]
    housekeeping: Callable[[], Awaitable[None]]
    padron: Callable[[], Awaitable[None]]
    digest: Callable[[], Awaitable[None]]
    github_health: Callable[[], Awaitable[None]] | None


def _daily_tasks(cfg: Config, runs: DailyRuns) -> list[DailyTask]:
    """Build the daily tasks, failing on an unknown target (R-9.11, BR-D05).

    Retention runs ``RETENTION_DELAY`` after the backup; the sum is taken on a
    ``date.min`` base so a late backup wraps to the next day's early time.
    Housekeeping runs at the retention time. The padrón refresh runs at its
    configured weekly time, its task checking the weekday when it runs (S12.4).
    The GitHub App health check runs at the digest time only when GitHub is
    configured.
    """
    kind = cfg.ops.backup.target.kind
    if kind != BACKUP_TARGET_KIND:
        raise ValueError(
            f"unsupported backup target kind {kind!r}: only {BACKUP_TARGET_KIND!r} is implemented"
        )
    backup_at = cfg.ops.backup.at
    retention_at = (datetime.combine(date.min, backup_at) + RETENTION_DELAY).time()
    tasks = [
        DailyTask(key="backup", at=backup_at, run=runs.backup),
        DailyTask(key="retention", at=retention_at, run=runs.retention),
        DailyTask(key="housekeeping", at=retention_at, run=runs.housekeeping),
        DailyTask(key="padron", at=cfg.sunat.padron.at, run=runs.padron),
        DailyTask(key="digest", at=cfg.gaps.digest_at, run=runs.digest),
    ]
    if runs.github_health is not None:
        tasks.append(DailyTask(key="github_health", at=cfg.gaps.digest_at, run=runs.github_health))
    return tasks


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
        sizes = await run_backup(_backup_job(settings, cfg), drive, today)
    except BackupError as error:
        logger.warning("backup failed", extra={"reason": error.reason})
        await _alert_backup_failure(pool, cfg, error.reason)
        return
    logger.info(
        "backup uploaded",
        extra={"dump_bytes": sizes.dump_bytes, "config_bytes": sizes.config_bytes},
    )


async def _run_padron_refresh(pool: Pool, http: httpx.AsyncClient, cfg: Config, data_dir: Path) -> None:
    """Refresh the padrón on its configured weekday, logging a failed download (S12.4)."""
    today = await _owner_today(pool, cfg.owner.timezone)
    if today.weekday() != cfg.sunat.padron.weekday:
        return
    try:
        await refresh_padron(pool, http, cfg.sunat.padron, data_dir)
    except PadronRefreshError as error:
        logger.warning("padron refresh failed", extra={"reason": error.reason})


async def _alert_backup_failure(pool: Pool, cfg: Config, reason: BackupReason) -> None:
    """Queue BR-O04's rate-limited admin alert for a failed backup stage (BR-O04)."""
    text = MESSAGES[RULE_BACKUP_ALERT].format(reason_desc=BACKUP_REASON_DESC[reason])
    async with pool.connection() as conn, conn.transaction():
        await queue_rate_limited_alert(
            conn, cfg.profiles, RULE_BACKUP_ALERT, BACKUP_ALERT_EVERY, RULE_BACKUP_ALERT, text
        )


async def _alert_config_rejected(pool: Pool, cfg: Config, reason: str) -> None:
    """Queue BR-O05's rate-limited admin alert for a rejected config reload (BR-O05, D155)."""
    text = MESSAGES[RULE_CONFIG_ALERT].format(reason=reason)
    async with pool.connection() as conn, conn.transaction():
        await queue_rate_limited_alert(
            conn, cfg.profiles, RULE_CONFIG_ALERT, CONFIG_ALERT_EVERY, RULE_CONFIG_ALERT, text
        )


async def _run_retention(pool: Pool, cfg: Config) -> None:
    """Apply the retention windows in one transaction and log the counts once (R-9.12)."""
    async with pool.connection() as conn, conn.transaction():
        counts = await prune(conn, cfg.ops.retention_days)
    logger.info("retention pruned", extra={"counts": counts})


def _laptop_root(flow: Flow) -> str:
    """Return the root of the flow's OneDrive folder sink, which holds ``_outbox`` (R-7.14)."""
    for sink in flow.sinks:
        if isinstance(sink, OneDriveFolderSink):
            return sink.root
    raise RuntimeError("a flow listing laptop_outlook needs a onedrive_folder sink")


@dataclass(frozen=True)
class _InboxDeps:
    """What an ``email_inbox`` fetcher needs: the HTTP client and the Google token sources (S7.1)."""

    http: httpx.AsyncClient
    google_tokens: Mapping[str, GoogleTokens]


@dataclass(frozen=True)
class _BrowserDeps:
    """What the browser-backed fetchers share: the SOL fetcher and the sandbox prefixes (D147)."""

    sol: SunatSolFetcher | None
    prefixes: HelperPrefixes


def _sol_fetcher(
    cfg: Config,
    directory: StoreDirectory,
    store: SecretStore,
    prefixes: HelperPrefixes,
) -> SunatSolFetcher | None:
    """Build the SOL fetcher once the site is active, or None until then (S10.1, R-5.2).

    A missing secret or a ``sol_ruc`` that is not the owner's RUC stops startup, naming
    only the keys, so the helper never signs in as the wrong taxpayer (R-9.10).
    """
    site = _sol_site(cfg, directory)
    if site is None:
        return None
    secrets = _sol_secrets(store)
    if secrets.enabled and not hmac.compare_digest(secrets.get("sol_ruc").encode(), cfg.owner.ruc.encode()):
        raise ValueError("sunat_sol: secret sol_ruc does not match owner.ruc (R-9.10)")
    return SunatSolFetcher(
        site,
        secrets,
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
        helper_command=browser_command(prefixes, SOL_HELPER_COMMAND),
    )


def _fetch_registries(
    settings: Settings,
    cfg: Config,
    directory: StoreDirectory,
    guarded_http: httpx.AsyncClient,
    inbox: _InboxDeps,
    browser: _BrowserDeps,
) -> dict[str, dict[str, Fetcher]]:
    """Return each flow's fetcher registry, sharing one instance where it can (S7.1, D98)."""
    builders = _fetcher_builders(settings, cfg, directory, guarded_http, inbox, browser)
    return {name: _flow_registry(name, flow, builders) for name, flow in cfg.flows.items()}


def _fetcher_builders(
    settings: Settings,
    cfg: Config,
    directory: StoreDirectory,
    guarded_http: httpx.AsyncClient,
    inbox: _InboxDeps,
    browser: _BrowserDeps,
) -> dict[str, _FetcherBuilder]:
    """Return a builder per fetcher this build runs; adding a fetcher adds an entry (S7.1)."""
    portal = PortalFetcher(
        directory,
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
        helper_command=browser_command(browser.prefixes, DEFAULT_HELPER_COMMAND),
    )
    qr_url = QrUrlFetcher(
        guarded_http,
        allowed_hosts(directory),
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
    )
    return {
        PORTAL_FETCHER: lambda _name, _flow: portal,
        QR_URL_FETCHER: lambda _name, _flow: qr_url,
        EMAIL_INBOX_FETCHER: lambda name, flow: _email_inbox_fetcher(name, flow, settings, cfg, inbox),
        SUNAT_SOL: lambda _name, _flow: browser.sol,
    }


def _fetch_schedules(directory: StoreDirectory) -> Mapping[str, Schedule]:
    """Return each fetcher's retry schedule: its flow's inbox or its store's portal (D65, D98)."""

    def inbox(flow: Flow, _invoice: FetchInvoice) -> tuple[timedelta, ...]:
        inbox_cfg = flow.email_inbox
        return DEFAULT_RETRY_SCHEDULE if inbox_cfg is None else inbox_cfg.retry_schedule

    def portal(_flow: Flow, invoice: FetchInvoice) -> tuple[timedelta, ...]:
        portal_cfg = portal_for(invoice.ruc_emisor, directory)
        return DEFAULT_RETRY_SCHEDULE if portal_cfg is None else portal_cfg.retry_schedule

    def sunat_sol(_flow: Flow, _invoice: FetchInvoice) -> tuple[timedelta, ...]:
        site = directory.sunat_sol
        return DEFAULT_RETRY_SCHEDULE if site is None else site.query.retry_schedule

    return {EMAIL_INBOX_FETCHER: inbox, PORTAL_FETCHER: portal, SUNAT_SOL: sunat_sol}


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
    deps: _InboxDeps,
) -> Fetcher | None:
    """Return the flow's mailbox fetcher, or ``None`` with a warning when it cannot run (D98).

    The provider picks the mailbox: Graph for ``graph`` and Gmail for ``gmail``.
    """
    inbox = flow.email_inbox
    builders: dict[str, _InboxBuilder] = {
        GRAPH_MODE: partial(_graph_mailbox, settings, cfg, deps.http),
        GMAIL_PROVIDER: partial(_gmail_mailbox, cfg, deps.http, deps.google_tokens),
    }
    if inbox is None:
        reason = "the flow has no email_inbox settings"
    elif inbox.provider not in builders:
        reason = f"provider {inbox.provider} is not supported"
    else:
        mailbox, reason = builders[inbox.provider](inbox)
        if mailbox is not None:
            return EmailInboxFetcher(mailbox, cfg.limits.media.max_bytes)
    logger.warning("email_inbox fetcher disabled", extra={"flow": name, "reason": reason})
    return None


def _graph_mailbox(
    settings: Settings, cfg: Config, http: httpx.AsyncClient, inbox: FlowInbox
) -> tuple[Mailbox | None, str]:
    """Return the flow's Graph mailbox, or why it cannot run (R-5.2, D98)."""
    if settings.age_identity_path is None:
        return None, "AGE_IDENTITY_PATH is not set"
    provider = build_token_provider(settings, cfg, inbox.account, scopes_for(cfg, inbox.account))
    return GraphMailbox(http, provider, inbox.account, cfg.microsoft.timeout_s), ""


def _gmail_mailbox(
    cfg: Config,
    http: httpx.AsyncClient,
    google_tokens: Mapping[str, GoogleTokens],
    inbox: FlowInbox,
) -> tuple[Mailbox | None, str]:
    """Return the flow's Gmail mailbox, or why it cannot run (R-5.2, D135)."""
    google = cfg.google
    if google is None:
        return None, "google config is not set"
    tokens = google_tokens.get(inbox.account)
    if tokens is None:
        return None, f"the account {inbox.account} has no Google token source"
    return GmailMailbox(http, tokens, inbox.account, google.timeout_s, google.max_message_bytes), ""


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
