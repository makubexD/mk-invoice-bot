# Plan: MVP phase 1 · foundation (E1)

Roadmap: `docs/plan-mvp.md`. Backlog: `specs/04-tasks.md` E1 (S1.1–S1.10, S5.1, S12.2). Design: `specs/03-design.md` §2, §3, §8, §9, §10.

Conventions for every step:
- Python 3.12, `uv`, package `src/invoicebot/`, tests in `tests/unit/` (no DB) and `tests/integration/` (real Postgres).
- Libraries fixed for this phase: FastAPI + uvicorn, pydantic v2 + pydantic-settings, PyYAML (`safe_load` only), psycopg 3 + `psycopg_pool` (async), Alembic (plain-SQL `op.execute` revisions; SQLAlchemy only as Alembic's engine), pytest + pytest-asyncio, ruff, mypy (strict on `src/`), `pyrage` (age), `import-linter`. Logging uses the stdlib with a JSON formatter (no structlog).
- Integration tests read `TEST_DATABASE_URL` (default `postgresql://invoicebot:invoicebot@localhost:5432/invoicebot_test`) and fail, not skip, when Postgres is unreachable. Start it with `podman compose -f deploy/docker-compose.yml up -d postgres`.
- Container engine: Podman and Docker are both supported from the same files (`deploy/Dockerfile`, `deploy/docker-compose.yml`). Nothing engine-specific goes in them: image names are fully qualified (`docker.io/library/...`, Podman does not resolve short names by default), no BuildKit-only syntax, no `container_name`. Local commands in this plan use `podman`; `docker` works by swapping the word. Scripts read `CONTAINER_ENGINE` (default: `podman` if on PATH, else `docker`) and never hardcode either.
- Every test docstring cites its requirement or rule ID. No real RUCs or phone numbers: use the synthetic values from `config/example.yaml`.

## Step 1: Project scaffold and health endpoint `[worker]`

Files: `pyproject.toml`, `src/invoicebot/__init__.py`, `src/invoicebot/app.py`, `tests/unit/test_health.py`, `.pre-commit-config.yaml`, `.gitignore`.

- `pyproject.toml`: project `mk-invoice-bot`, `requires-python = ">=3.12"`, runtime deps FastAPI, uvicorn[standard], pydantic, pydantic-settings, PyYAML, psycopg[binary], psycopg-pool, alembic, sqlalchemy, pyrage; dev group pytest, pytest-asyncio, httpx, ruff, mypy, types-PyYAML, import-linter, bandit, pip-audit. Ruff: line length 110, rules `E,F,I,B,UP,S,ASYNC`. Mypy: `strict = true` for `src/`. Pytest: `asyncio_mode = "auto"`, testpaths `tests`.
- `app.py`: `create_app(db_ping: Callable[[], Awaitable[bool]]) -> FastAPI` with `GET /health` returning `{"status":"ok","db":"ok"}` 200 when `db_ping()` is true, and `{"status":"degraded","db":"down"}` 503 when it is false or raises. A module-level `app` uses a placeholder ping that returns false until step 6 wires the DB.
- `.pre-commit-config.yaml`: ruff (lint + format) and mypy hooks.
- `.gitignore`: Python defaults, `.env`, `*.age.key`, `/data/`.
- Tests (R-9.14): 200 with a ping returning true; 503 with a ping returning false; 503 with a ping that raises.

Test: `uv sync && uv run pytest tests/unit/test_health.py -q && uv run ruff check src tests && uv run mypy src`

## Step 2: Docker image and local Compose `[worker]`

Files: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `.dockerignore`.

- `Dockerfile`: multi-stage, `docker.io/library/python:3.12-slim`, `uv sync --frozen --no-dev` into `/app/.venv`, non-root user `bot`, `CMD uvicorn invoicebot.app:app --host 0.0.0.0 --port 8080 --workers 1`. No Tesseract or Chromium yet (later phases).
- Compose project `invoicebot`: service `app` (build `..`, `mem_limit: 900m`, env `DATABASE_URL`, port `8080:8080`, `depends_on: postgres: condition: service_healthy`); service `postgres` (`docker.io/library/postgres:16-alpine`, `mem_limit: 300m`, command `-c shared_buffers=64MB -c max_connections=20`, volume `pgdata`, healthcheck `pg_isready`, port `5432:5432` for local dev only, user/password/db `invoicebot`). Credentials here are local-dev only.
- `uv.lock` must be committed (generated in step 1).
- Must build and run unchanged under both `podman compose` and `docker compose` (see the container engine convention): the build sets `dockerfile: deploy/Dockerfile` with context `..`, and the image gets an explicit `image: localhost/invoicebot-app:dev` name.

Test: `podman compose -f deploy/docker-compose.yml up -d --build && sleep 5 && curl -s -o /dev/null -w '%{http_code}' localhost:8080/health; podman compose -f deploy/docker-compose.yml down` (prints 503 until step 6, which is expected here; the test is that the image builds and both services start).

## Step 3: Typed config models `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/config/loader.py`, `tests/unit/test_config_models.py`.

- Pydantic v2 models for every top-level section of `config/example.yaml` (owner, work, profiles, admins, channels, routing, flows with fetchers and sinks, llm_fallback, limits, gaps, ops, deploy and any other section present). Every model uses `model_config = ConfigDict(extra="forbid")`.
- `loader.load_config(path) -> Config`: reads with `yaml.safe_load` only, resolves `{owner.ruc}`-style placeholders from the loaded document (unknown placeholder → `ValueError` naming it), then validates.
- Tests (R-9.6): `config/example.yaml` loads; an unknown key anywhere fails and names the path; a placeholder resolves; an unknown placeholder fails; a YAML tag such as `!!python/object` is refused.

Test: `uv run pytest tests/unit/test_config_models.py -q && uv run mypy src`

## Step 4: `check_config.py` rules `[worker]`

Files: `scripts/check_config.py`, `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

- `checks.run_checks(cfg, *, pr_mode: bool) -> list[Problem]` where a `Problem` has `code`, `path`, `message`, `blocking`.
  - Portals with `status: todo|broken`: blocking only when `pr_mode` (R-11.6).
  - A `python:` adapter whose module is outside `invoicebot.fetchers.portals.` is blocking (R-5.8, R-11.12).
  - `llm_fallback` enabled for `work_microsoft` while the provider is `deepseek` is blocking (D43).
- `scripts/check_config.py [--pr] [path]`: default path `config/example.yaml`; prints each problem on one line; exits 1 if any blocking problem or if loading fails, else 0.
- Tests: one per rule, plus `todo` portal → exit 1 with `--pr` and exit 0 without; the example config passes.

Test: `uv run pytest tests/unit/test_config_checks.py -q && uv run python scripts/check_config.py`

## Step 5: Alembic setup, connection pool and DB health `[worker]`

Files: `alembic.ini`, `migrations/env.py`, `src/invoicebot/store/db.py`, `src/invoicebot/app.py`, `tests/integration/conftest.py`, `tests/integration/test_db_health.py`.

- `db.py`: `create_pool(dsn) -> AsyncConnectionPool` (min 1, max 5) and `async ping(pool) -> bool` running `SELECT 1` with a 2 s timeout.
- `app.py`: lifespan opens the pool from `DATABASE_URL` and wires `ping` into `/health`.
- `migrations/env.py`: online mode from `DATABASE_URL`, no autogenerate, no metadata.
- `tests/integration/conftest.py`: session fixture that drops and recreates database `invoicebot_test`, runs `alembic upgrade head`, and yields a pool.
- Tests (R-9.14): `/health` is 200 with the DB up; 503 when the pool points at a closed port.

Test: `podman compose -f deploy/docker-compose.yml up -d postgres && uv run pytest tests/integration/test_db_health.py -q`

## Step 6: Initial schema migration `[worker]`

Files: `migrations/versions/0001_initial.py`, `tests/integration/test_schema.py`.

- One revision creating every table in `specs/03-design.md` §3 exactly as listed there (names, columns, primary keys, uniques, indices), with `messages` PK `(channel, external_id)`, invoices carrying `profile_id` and `origin_channel`, `kv` as `UNLOGGED`. Use plain SQL in `op.execute`. `downgrade` drops them in reverse order.
- Tests (R-9.4): after `upgrade head` every §3 table exists (the test holds the expected table list); a second `upgrade head` is a no-op; `downgrade -1` then `upgrade head` works; inserting the same `(channel, external_id)` twice violates the PK, while the same id on two channels doesn't (R-1.8).

Test: `uv run pytest tests/integration/test_schema.py -q`

## Step 7: Business-rule catalog and parity test `[worker]`

Files: `src/invoicebot/rules/catalog.py`, `src/invoicebot/rules/messages_es.py`, `tests/unit/test_rules_parity.py`.

- `catalog.py`: `RULES: dict[str, Rule]` with `Rule(id, state, message_key)` for every BR-* row in `specs/06-business-rules.md` (state = the "To/State" column; `None` when the rule doesn't change state).
- `messages_es.py`: `MESSAGES: dict[str, str]` with the exact es-PE texts from spec 06, placeholders as `{name}`.
- Test (R-11.1): parses the BR table rows of spec 06 and fails listing any ID missing from `RULES`, any `RULES` ID missing from the spec, and any `message_key` missing from `MESSAGES`.

Test: `uv run pytest tests/unit/test_rules_parity.py -q`

## Step 8: Channel port and canonical messages `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/channels/profiles.py`, `tests/unit/test_channels_base.py`.

- `base.py` (PT-1): pydantic `Attachment(kind, media_ref, mime, size)`, `InboundMessage(channel, external_id, profile_id, sender_role, text, attachments, reply_to_external_id, received_at)`, `OutboundMessage(channel, recipient, profile_id, rule_id, text, buttons, template)`; `Protocol Channel` with `async send(msg) -> str`, `async fetch_media(media_ref) -> Path`, `name`.
- `profiles.py`: `ProfileIndex.from_config(cfg)` resolving a WhatsApp number or Telegram user id to `profile_id`; unknown sender → `None`; `reply_channel(invoice_origin_channel)` returns the origin; `admin_channel(owner_profile)` returns the preferred channel.
- Tests (R-1.9): a WhatsApp number and a Telegram id of the same profile resolve to one `profile_id`; an unknown id resolves to `None`; replies go to the origin channel; admin alerts go to the preferred channel.

Test: `uv run pytest tests/unit/test_channels_base.py -q && uv run mypy src`

## Step 9: Inbound message store and attachment matching `[worker]`

Files: `src/invoicebot/store/messages.py`, `tests/integration/test_messages_store.py`.

- `async store_inbound(conn, msg) -> bool`: inserts into `messages`; returns false (no error) when `(channel, external_id)` already exists (R-1.8 idempotency).
- `async find_open_invoice_for_attachment(conn, profile_id, reply_to) -> int | None` (R-1.5): the quoted invoice if `reply_to` maps to one in `MANUAL_REQUIRED` or `CONFIRMING`, else the most recent such invoice of the profile, across all its channels.
- Tests: duplicate delivery stored once; invoice opened from `whatsapp` matched by a document arriving on `telegram`; quoted invoice wins over the most recent one.

Test: `uv run pytest tests/integration/test_messages_store.py -q`

## Step 10: Job queue with lease, backoff and crash-loop guard `[worker:high: reasoning: lease expiry, SKIP LOCKED claims and the died-twice counter interact under concurrency and must stay correct across process crashes]`

Files: `src/invoicebot/queue/jobs.py`, `tests/integration/test_jobs.py`.

- `enqueue(conn, invoice_id, step, run_after=now)`, `claim(conn, worker_id, lease_s) -> Job | None` using `FOR UPDATE SKIP LOCKED` on `run_after <= now() AND (lease_until IS NULL OR lease_until < now())`, `complete(conn, job)`, `fail(conn, job, error)` with exponential backoff `2**attempts` minutes capped at 60.
- After 5 failed attempts the job is dead and its invoice goes to `FAILED` with rule `BR-S03`. A claim that finds an expired lease increments a `lease_expirations` counter; at 2 the job goes to `FAILED` with `BR-S03` (crash-loop guard, R-1.11). Add the counter column with a new migration `0002_jobs_lease_expirations.py` if §3 lacks it.
- `enqueue` issues `NOTIFY jobs` in the same transaction.
- Tests (R-9.4, R-1.11, BR-S03): two concurrent claimers never get the same job; an expired lease is re-claimed; backoff schedule; fifth failure → FAILED; two expired leases → FAILED; NOTIFY received by a listener.

Test: `uv run pytest tests/integration/test_jobs.py -q`

## Step 11: Worker loop and heavy semaphore `[worker]`

Files: `src/invoicebot/queue/worker.py`, `tests/integration/test_worker.py`.

- `Worker(pool, handlers: dict[str, Handler])` with `run()` that `LISTEN jobs`, claims and runs one job at a time, waits on the notification (60 s timeout as a safety net) instead of polling, and stops cleanly on cancel. Module-level `HEAVY = asyncio.Semaphore(1)` that heavy handlers must acquire.
- Tests: a job enqueued after start runs within 1 s; an unknown step fails the job; two heavy handlers never overlap (record timestamps).

Test: `uv run pytest tests/integration/test_worker.py -q`

## Step 12: Outbox and sender `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `tests/integration/test_outbox.py`.

- `add(conn, channel, to, payload)` writes an `outbox` row in the caller's transaction. `OutboxSender(pool, transports: dict[str, Transport])` sends pending rows, marks them `sent`, retries failures with backoff (max 8 attempts, then `dead` + log). Channels: `whatsapp`, `telegram`, `github`, `event`.
- Tests (R-1.8, R-11.5): a row written in a rolled-back transaction is never sent; a failing transport is retried with growing delay; each channel routes to its transport; an unknown channel is rejected at `add`.

Test: `uv run pytest tests/integration/test_outbox.py -q`

## Step 13: Encrypted secrets and per-adapter scoping `[worker]`

Files: `src/invoicebot/secrets.py`, `tests/unit/test_secrets.py`, `scripts/secrets_edit.py`.

- `SecretStore.load(path_age, identity_path)`: decrypts an age file (YAML map) with `pyrage` into memory only. `scoped(adapter_name, declared: set[str]) -> Secrets` returns a view that raises `SecretNotDeclared` for any other key. Only an adapter named `sunat_sol` may declare `sol_*` keys.
- Startup guard: if the environment or `.env` holds a key matching `*_TOKEN|*_SECRET|*_PASSWORD|*_KEY`, raise at start (R-9.9).
- `scripts/secrets_edit.py`: decrypt to a temp file with mode 600, open `$EDITOR`, re-encrypt, shred the temp file.
- Tests (R-9.9, R-9.10): round-trip encrypt/decrypt with a test identity; undeclared key raises; a non-`sunat_sol` adapter declaring `sol_password` raises; secret-looking env var fails startup; a missing secret makes `scoped` report the adapter as disabled.

Test: `uv run pytest tests/unit/test_secrets.py -q`

## Step 14: JSON logs, step timings and redaction `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

- JSON formatter emitting `ts, level, event, invoice_id, job_id, step, rule_id, duration_ms` (missing fields omitted); `step_timer(step)` context manager that logs `duration_ms`. A redaction filter replaces every loaded secret value and any `bot<digits>:<token>` URL segment with `***`, including in httpx and uvicorn loggers.
- Tests (R-9.5, R-9.16): a line parses as JSON with the fields; the timer records a duration; a fake Telegram token inside an httpx request URL and a fake secret value never appear in captured output.

Test: `uv run pytest tests/unit/test_logging.py -q`

## Step 15: CI workflow `[worker]`

Files: `.github/workflows/ci.yml`, `scripts/check_fixtures.py`, `tests/unit/test_check_fixtures.py`.

- `ci.yml` on push and pull_request: `uv sync`; ruff; mypy; pytest with a `postgres:16-alpine` service; `check_config.py` with `--pr` only on pull requests; gitleaks (`gitleaks/gitleaks-action` pinned to a commit SHA); `uv run pip-audit`; `uv run bandit -r src -q`; a step that fails if any workflow contains `pull_request_target` or `runs-on: self-hosted`; `check_fixtures.py`.
- `check_fixtures.py`: fails on any committed image under `tests/` with EXIF data, and on any 11-digit number in `tests/` or `config/` that passes the RUC check digit and isn't in the synthetic allowlist `tests/synthetic_rucs.txt`.
- Tests (R-9.17): an image with EXIF fails; a clean image passes; a valid non-allowlisted RUC fails; an allowlisted one passes.

Test: `uv run pytest tests/unit/test_check_fixtures.py -q && uv run python scripts/check_fixtures.py`

## Step 16: Zero-consumer import boundary `[worker]`

Files: `.importlinter`, `pyproject.toml`, `tests/unit/test_import_boundary.py`.

- Import-linter contracts: `invoicebot.pipeline`, `invoicebot.queue`, `invoicebot.store`, `invoicebot.channels`, `invoicebot.rules` must not import `invoicebot.api` or `invoicebot.events` (the contract surface depends on the core, never the reverse). Add `lint-imports` to `ci.yml`.
- Test (R-12.7): runs `lint-imports` in a subprocess and asserts exit 0; a temporary forbidden import in a scratch copy makes it fail.

Test: `uv run lint-imports && uv run pytest tests/unit/test_import_boundary.py -q`

## Step 17: Deploy script with health check and rollback `[worker]`

Files: `deploy/deploy.sh`, `deploy/README.md`, `tests/deploy/test_deploy.sh`.

- `deploy.sh <image_tag>`: records the current image tag; `pg_dump` (excluding `kv`) to `/data/backups/pre-<tag>.sql.gz`; runs `alembic upgrade head` with the new image; starts `app` with the new tag; polls `/health` for 60 s; on failure restarts the previous tag and exits 1. All container calls go through `$CONTAINER_ENGINE` (see conventions). Config-only mode `deploy.sh --config`: validates with `check_config.py`, copies config, sends `SIGHUP`; an invalid config exits 1 and leaves the old one.
- `tests/deploy/test_deploy.sh`: against local Compose, deploys a good tag (health 200), then a tag whose app exits at start (rollback happens, previous tag running, exit 1), then an invalid config (refused).
- `deploy/README.md`: how to run it locally and on a VPS with either Podman or Docker (R-9.13).

Test: `bash tests/deploy/test_deploy.sh`

## Step 18: Deploy workflow and schema compatibility check `[worker]`

Files: `.github/workflows/deploy.yml`, `.github/workflows/ci.yml`.

- `deploy.yml`: `workflow_run` on CI `completed` for `main` with `conclusion == success`; builds and pushes the image to GHCR; runs `deploy.sh` over SSH. The whole job is skipped unless the repository variable `DEPLOY_ENABLED == 'true'` (off until a VPS exists, D31). SSH key and host come from encrypted secrets.
- `ci.yml`: a job that builds the previous `main` image, migrates a fresh Postgres to the new head, and runs the previous image's `/health` against it (expand/contract check, S12.2).
- Check: the workflows pass `actionlint`.

Test: `podman run --rm -v "$PWD:/repo:Z" -w /repo docker.io/rhysd/actionlint:1.7.7`
