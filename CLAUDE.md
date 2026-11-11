# mk-invoice-bot

A WhatsApp and Telegram bot that reads Peruvian invoices from photos, validates them and files them. One Python
process (FastAPI + asyncio) and Postgres, built for a 2 GB host.

- **Source of truth:** `specs/` (requirements `02`, design `03`, backlog `04`, business rules `06`). Code and tests
  cite requirement and rule IDs (`R-1.8`, `BR-S01`). If the code and a spec disagree, stop and say so.
- **Roadmap:** `docs/plan-mvp.md`; each phase has its own `docs/plan-mvp-<phase>.md`.
- **Out of scope:** Jarvis. Nothing here may import it or depend on it; integration is the REST/MCP/events contract.

## Commands

Everything runs through `uv`. Never call `pytest`, `ruff` or `mypy` bare, and never use `pip`.

```bash
podman compose -f deploy/docker-compose.yml up -d postgres   # integration tests need it
uv run pytest -q tests/unit/test_x.py                        # one file while working
uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations
uv run mypy src && uv run lint-imports
uv run pytest -q --cov                                       # full suite with branch coverage (floor 90%): once, before a PR
```

Dependencies are managed with `uv` and `uv.lock` (build backend `hatchling`); this project does not use Poetry. Add
one with the `py-add-package` skill, which vets it first, and only when the plan step says so.

## Review

- A phase's finish review runs the `py-code-reviewer` agent on the whole diff, next to the usual review.
- A phase that touches a webhook, secrets, file parsing or an outbound request also runs `py-security-auditor`.
- Both only report. The main session decides what to fix.

## Design rules

The goal is code that is easy to read and easy to extend. When two rules pull apart, the simpler code wins.

- **Build only what the step asks (YAGNI).** No option, parameter, hook or config key without a caller today. No
  abstraction before its second real use; the third copy is when to extract.
- **Simplest thing that works (KISS).** A function before a class, the standard library before a dependency, plain
  SQL before a query builder. If a reviewer needs the author to explain it, rewrite it.
- **One home for each piece of knowledge (DRY).** A business rule, limit, message text or schema fact lives in one
  place and is imported: rule texts in `rules/`, limits in config, retry policy in `queue/retry.py`. Search before
  adding a helper. Similar-looking code with different reasons to change is not duplication.
- **One reason to change (SRP).** A module does one job and its docstring says which. Split a function that needs
  "and" to describe it. Lint caps complexity at 10 and arguments at 6: restructure, never silence.
- **Extend by adding, not editing (open/closed).** A new channel, fetcher, sink or job step is a new module that
  implements an existing `Protocol` and is registered in one place (a mapping such as `handlers` or `transports`).
  Adding one must not touch the pipeline or another adapter. No `if channel == ...` chains outside an adapter.
- **Depend on ports, pass dependencies in (DIP).** Core code names a `Protocol` (`Channel`, `Transport`,
  `Handler`), never a concrete adapter. Pools, HTTP clients, clocks, config and secrets arrive as arguments. No
  module-level client, no global lookup, nothing created inside the function that uses it.
- **Small interfaces.** A port has the few methods its callers use. Do not add a method "for later".
- **Classes only where they earn it.** Use a class for long-lived state or a lifecycle (`Worker`, `OutboxSender`) or
  to implement a port. Everything else is a module of functions. Composition, never inheritance for reuse. Data is
  a frozen dataclass inside the core and a frozen pydantic model at a boundary (config, channel messages, HTTP).

## Architecture

- `channels/`, `pipeline/`, `queue/`, `store/` and `rules/` never import `api/` or `events/`. `.importlinter`
  enforces it; add a contract there when a new boundary appears. This replaces the generic `domain/`, `core/`
  layering in the `py-project-layout` rule.
- Dependencies point inward: `api` → `pipeline` → `store`, `queue`, `rules`. Channel adapters sit at the edge and
  the pipeline sees only canonical messages (`channels/base.py`).
- **Database:** store functions take the caller's connection and never commit; the caller owns the transaction. A
  state change, its next job and its outbox message go in one transaction. Time comes from the database clock
  (`now()`), not from Python.
- **Migrations:** plain SQL through `op.execute`, expand only (add, never rename or drop in the same release). A
  merged revision is never edited. CI runs the previous image against the new schema.
- **Outbound messages** always go through the outbox. Nothing sends to a channel directly.

## Security

- **Secrets come from the age-encrypted store** through a scoped `Secrets` view, never from the environment. This
  overrides the `py-security` rule: startup rejects secret-looking environment variables (R-9.9).
- No real RUCs, phone numbers, names or invoice photos in the repo; use the synthetic values in
  `config/example.yaml`. Sender ids are logged only as a hash.
- Everything from a channel, a portal or a file is untrusted: validate it at the boundary and cap its size before
  reading it.

## Tests

- `tests/unit/` has no database and no network. `tests/integration/` uses real Postgres and fails, not skips, when
  it is unreachable. Both are flat, one `test_<module>.py` per module; this overrides the mirrored tree in the
  `py-testing` rule.
- Every test docstring starts with the requirement or rule ID it proves.
- Fake only at a port you own (`httpx.MockTransport`, the fake channel). Never mock the database.
- New behavior comes with its test in the same change. Never delete, skip or weaken a test to get to green.

## Done means

The step's own test passes, the lint, type and import commands above pass, only the files the step names changed,
and no new `# noqa`, `# type: ignore` or lint ignore was added without a reason on the same line.
