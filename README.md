# mk-invoice-bot

A WhatsApp bot that files Peruvian electronic invoices (SUNAT CPE). Send it a
photo or the PDF of an invoice (the XML can follow) and it:

1. reads the key data deterministically: the QR first, then the XML, the PDF
   text or OCR. An LLM fallback exists only as an opt-in per flow and is never
   used for work invoices;
2. picks the flow from the buyer's RUC (your company, your employer, or your
   own DNI) and asks only when it can't tell;
3. checks the invoice with SUNAT and fetches the official PDF and XML from the
   QR link, the store's portal or a mailbox;
4. files it: Google Drive and a Sheets row, or a OneDrive folder, the employer's
   Excel template and a mail to HR. A flow can hold the mail for the owner to
   check in the chat and send it with [Enviar] (`send: review`, D194);
5. replies in the chat. When it can't do something (an unknown store portal,
   for example), it says why, opens an issue in a private ops repo and alerts
   the owner.

Telegram is planned for v1.1. The bot is a standalone app: other systems, such
as the owner's personal agent or n8n, will reach it only through its REST API,
MCP server and signed event webhooks (v1.1).

## Status

In development, phase 9 of the MVP roadmap ([docs/plan-mvp.md](docs/plan-mvp.md)).
Flows A, B and `personal_dni` are built and the bot runs on the owner's machine
(milestone M7). The live chat runs (phase 9d2 `live-chat`) wait for Meta's
business verification. The Telegram adapter is built and tested offline (T1);
live setup is T2 ([docs/plan-telegram.md](docs/plan-telegram.md)).

## Quickstart (development)

Everything runs through [uv](https://docs.astral.sh/uv/); never call `pytest`,
`ruff` or `mypy` bare.

```sh
uv sync                                                        # the virtualenv from uv.lock (Python 3.12.4+)
podman compose -f deploy/docker-compose.yml up -d postgres     # integration tests need it
uv run pytest -q tests/unit                                    # no database, no network
uv run pytest -q --cov                                         # full suite, 90% branch floor
uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations
uv run mypy src && uv run lint-imports
```

To run the bot itself, start from the example config:

1. Copy `config/example.yaml` to `config/config.yaml` and fill in your flows,
   profiles and stores. `uv run python scripts/check_config.py config/config.yaml` validates it.
2. Create the age-encrypted secrets file with
   `uv run python scripts/secrets_edit.py config/secrets.age --identity <key file> --recipient <age1...>`.
   Secrets never come from the environment; startup refuses secret-looking
   variables (R-9.9).
3. Build the image and deploy it with `deploy/deploy.sh`, as described in
   [the deploy guide](deploy/README.md). The guide also covers the live host,
   Tailscale Funnel, the laptop agent and rollback. On Windows, run everything
   inside WSL2, as the guide's Windows section says.

The app reads these environment variables (names only; see
`src/invoicebot/settings.py`): `DATABASE_URL`, `CONFIG_PATH`,
`PARSING_RULES_PATH`, `SECRETS_PATH`, `AGE_IDENTITY_PATH`, `DATA_DIR`,
`HELPER_SANDBOX`, `HELPER_TMP_MB`.

## CLI

The `invoicebot` command (inside the container, or `uv run invoicebot`):

| Command                                      | What it does                                                    |
| -------------------------------------------- | --------------------------------------------------------------- |
| `invoicebot portal-test <key> --sample <f>`  | Runs one portal fetch on a sample invoice                       |
| `invoicebot auth-microsoft --account <a>`    | Signs a Microsoft account in and resumes the invoices it holds  |
| `invoicebot auth-google --account <a>`       | Signs a Google account in and resumes the invoices it holds     |
| `invoicebot google-setup --flow <f>`         | Creates or checks a flow's Drive folder and spreadsheet         |
| `invoicebot rehearse send\|replies`          | Runs the dev-only console channel and prints its replies (D165) |
| `invoicebot telegram ids\|webhook\|commands` | Lists sender ids; manages the webhook and menu (D186, D187)     |

Scripts in `scripts/`. The Python ones run with `uv run python scripts/<name>`;
`restore_drill.sh` runs with bash on the host and `laptop_agent.ps1` with
PowerShell on the laptop:

| Script               | What it does                                                    |
| -------------------- | --------------------------------------------------------------- |
| `check_config.py`    | Validates the runtime config and the store directory            |
| `secrets_edit.py`    | Edits the age-encrypted secrets file without leaving plaintext  |
| `import_padron.py`   | Imports SUNAT's padrón ZIP into the database                    |
| `import_claims.py`   | Registers the owner's existing sent, unpaid claim folders       |
| `restore_drill.sh`   | Restores a nightly backup into an empty database and checks it  |
| `memprofile.py`      | Reports peak memory and latency over synthetic invoices         |
| `make_golden.py`     | Draws synthetic receipts with their expected JSON               |
| `accuracy_report.py` | Reports a golden set's accuracy without exposing its invoices   |
| `laptop_agent.ps1`   | Sends flow B mail through Outlook classic on the owner's laptop |

The day-to-day procedures (re-authorizing, adding a portal, rotating secrets,
restoring) are in [the runbook](docs/runbook.md).

## Layout

| Path                 | Holds                                                                                |
| -------------------- | ------------------------------------------------------------------------------------ |
| `src/invoicebot/`    | The app; one package per job, drawn in [the architecture page](docs/architecture.md) |
| `tests/unit/`        | Tests without database or network                                                    |
| `tests/integration/` | Tests against a real Postgres                                                        |
| `migrations/`        | Alembic revisions, plain SQL, expand only                                            |
| `config/`            | `example.yaml`, parsing rules, portal and store files                                |
| `deploy/`            | Dockerfile, Compose files, deploy and rollback scripts                               |
| `scripts/`           | Operator scripts (table above)                                                       |
| `specs/`             | The specs: the source of truth                                                       |
| `docs/`              | Roadmap, open plans, architecture, runbook; finished plans in `archive/`             |

The packages under `src/invoicebot/`:

- `channels`: the channel port and the WhatsApp adapter; `api`: the webhook.
- `queue`: durable jobs, the worker and the outbox sender.
- `pipeline`: the job steps, chat replies and commands.
- `routing`, `rules`, `extract`: pick the flow, the business-rule texts,
  reading the invoice.
- `fetchers`, `sinks`, `sunat`, `github`, `auth`, `net`: the outside world.
- `store`: Postgres SQL functions; `config`: the typed config.
- `ops`, `gaps`: daily jobs and capability gaps; `events`: v1.1.

## Docs

The project uses spec-driven development: the specs are the source of truth,
and the code and tests cite their IDs (`R-1.8`, `BR-S01`).

| #   | Document                                                       | Purpose                                                                 |
| --- | -------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 0   | [specs/00-constitution.md](specs/00-constitution.md)           | Non-negotiable principles                                               |
| 1   | [specs/01-research.md](specs/01-research.md)                   | What exists, what to reuse, with sources                                |
| 2   | [specs/02-requirements.md](specs/02-requirements.md)           | User stories and testable acceptance criteria (EARS)                    |
| 3   | [specs/03-design.md](specs/03-design.md)                       | Architecture, data model, state machine, resource budget                |
| 4   | [specs/04-tasks.md](specs/04-tasks.md)                         | Backlog, traced to the requirements                                     |
| 5   | [specs/05-open-questions.md](specs/05-open-questions.md)       | Decisions taken and questions still open                                |
| 6   | [specs/06-business-rules.md](specs/06-business-rules.md)       | Business rules (BR-\*), user messages, capability gaps and owner alerts |
| 7   | [specs/07-foundation-review.md](specs/07-foundation-review.md) | Review of existing open-source agents; patterns reused (no code copied) |
| –   | [docs/architecture.md](docs/architecture.md)                   | Diagrams: components, state machine, flows, deploy                      |
| –   | [docs/runbook.md](docs/runbook.md)                             | Operating procedures                                                    |
| –   | [deploy/README.md](deploy/README.md)                           | Building, deploying and running the live host                           |
| –   | [docs/plan-mvp.md](docs/plan-mvp.md)                           | The roadmap; finished phase plans are in [docs/archive/](docs/archive/) |
