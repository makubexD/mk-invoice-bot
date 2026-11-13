"""FastAPI application: the health endpoint and the runtime wiring at startup.

With no config or secrets file the app serves ``/health`` only. With both, the
lifespan loads them, mounts the WhatsApp webhook and runs the job worker and
the outbox sender as supervised background tasks until shutdown.
"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.client import REQUEST_TIMEOUT_SECONDS, WhatsAppClient
from invoicebot.channels.whatsapp.transport import make_transport
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Config, WhatsAppChannel
from invoicebot.extract.parsing_rules import load_rules
from invoicebot.fetchers.portal import PortalFetcher
from invoicebot.logging import configure_logging
from invoicebot.pipeline.extract import ROUTE_STEP, make_extract_handler
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.handlers import EXTRACT_STEP, make_download_handler
from invoicebot.pipeline.intake import DOWNLOAD_STEP
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.outbox import OutboxSender
from invoicebot.queue.worker import Worker
from invoicebot.routing.router import compile_rules
from invoicebot.secrets import Secrets, SecretStore, assert_no_plain_secrets
from invoicebot.settings import Settings
from invoicebot.store.db import Pool, create_pool, ping

logger = logging.getLogger(__name__)

DbPing = Callable[[], Awaitable[bool]]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
TaskFactory = Callable[[], Awaitable[None]]

#: Secrets the WhatsApp channel declares (R-9.10).
WHATSAPP_ADAPTER = "whatsapp"
WHATSAPP_SECRETS = frozenset(
    {"whatsapp_token", "whatsapp_app_secret", "whatsapp_verify_token", "sender_hash_key"}
)
#: Pause between two outbox passes.
OUTBOX_INTERVAL_S = 2.0
#: Pause before a background task that died is started again.
RESTART_DELAY_S = 5.0


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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http:
        client = WhatsAppClient(
            http,
            phone_number_id=channel.phone_number_id,
            graph_api_version=channel.graph_api_version,
            token=secrets.get("whatsapp_token"),
        )
        app.include_router(make_router(_webhook_deps(cfg, pool, secrets)))
        registry = {
            "portal_registry": PortalFetcher(
                directory, cfg.limits.media.max_bytes, cfg.extraction.helper_timeout_s
            )
        }
        worker = Worker(
            pool,
            {
                DOWNLOAD_STEP: make_download_handler(pool, client, settings.data_dir, cfg.limits.media),
                EXTRACT_STEP: make_extract_handler(
                    pool,
                    cfg.extraction,
                    cfg.limits.media,
                    settings.parsing_rules_path,
                ),
                ROUTE_STEP: make_route_handler(pool, cfg, rules),
                FETCH_STEP: make_fetch_handler(pool, cfg, directory, registry, settings.data_dir),
            },
        )
        numbers = {name: profile.whatsapp for name, profile in cfg.profiles.items() if profile.whatsapp}
        sender = OutboxSender(
            pool,
            {WHATSAPP_ADAPTER: make_transport(client, pool, numbers=numbers, templates=channel.templates)},
        )
        async with _background({"worker": worker.run, "outbox": partial(_send_forever, sender)}):
            yield


def _webhook_deps(cfg: Config, pool: Pool, secrets: Secrets) -> WebhookDeps:
    """Collect what the webhook routes need from the config and the channel's secrets."""
    return WebhookDeps(
        pool=pool,
        index=ProfileIndex.from_config(cfg),
        owner_profile=cfg.owner.profile,
        admin_profiles=frozenset(name for name, profile in cfg.profiles.items() if profile.admin),
        app_secret=secrets.get("whatsapp_app_secret"),
        verify_token=secrets.get("whatsapp_verify_token"),
        sender_hash_key=secrets.get("sender_hash_key"),
        max_queued_per_sender=cfg.limits.max_queued_per_sender,
    )


def _load_secrets(settings: Settings) -> SecretStore:
    """Decrypt the secrets file, failing fast when the identity is not configured."""
    if settings.secrets_path is None or settings.age_identity_path is None:
        raise ValueError("SECRETS_PATH and AGE_IDENTITY_PATH must both be set")
    return SecretStore.load(settings.secrets_path, settings.age_identity_path)


def _whatsapp_channel(cfg: Config) -> WhatsAppChannel | None:
    """Return the configured WhatsApp channel, if any."""
    return next((channel for channel in cfg.channels if isinstance(channel, WhatsAppChannel)), None)


async def _send_forever(sender: OutboxSender) -> None:
    """Deliver due outbox rows every ``OUTBOX_INTERVAL_S`` seconds."""
    while True:
        await sender.send_pending()
        await asyncio.sleep(OUTBOX_INTERVAL_S)


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
