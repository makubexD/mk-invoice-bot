# Plan: MVP phase 9i · docs

Roadmap: `docs/plan-mvp.md` row 9i. It runs while Meta verification is pending, before 9d2 `live-chat`. No code behavior changes.

**Goal:**

- The README says what the bot is today, how far the roadmap got, how to run it locally, the CLI commands and the source layout.
- `docs/architecture.md` holds four Mermaid diagrams drawn from `specs/03-design.md` and the code: components, the invoice state machine, flows A and B, and the local deploy topology.
- A unit test keeps the diagrams, the README's command list and the doc links from drifting away from the code.
- `CLAUDE.md` and `.claude/` say nothing stale or contradictory.
- Finished plan files leave `docs/` for `docs/archive/`, so `docs/` lists only the roadmap, the open plans and the runbook.

**Proposed decisions (owner review):**

- **D161:** diagrams are Mermaid blocks in `docs/architecture.md`. GitHub renders them, they diff as text, and they need no image tooling. `specs/03-design.md` stays the source of truth; the page is a view of it and links to its sections. `tests/unit/test_docs.py` checks the diagrams name every state and package.
- **D162:** a finished phase plan moves to `docs/archive/` with `git mv` (history kept). `plan-next` only reads `docs/plan-*.md` and treats a missing plan file as finished, so moving one changes nothing for `/next`. Links to a moved plan are updated, except inside `docs/archive/` itself (those files are history and stay as written).

**Out of scope:** rewriting `specs/` (the design spec keeps its ASCII diagrams), `docs/runbook.md` content, the Windows notes (9f).

**Needs from the owner:** nothing.

## Step 1: Archive the finished plans and record D161–D162 `[worker:low]`

Files: `docs/plan-*.md` (moved), `docs/archive/` (new), `docs/plan-mvp.md`, `specs/05-open-questions.md`, `specs/spikes/qr.md`, `docs/plan-mvp-live-chat.md`.

1. `git mv` every `docs/plan-*.md` into `docs/archive/` except `docs/plan-mvp.md`, `docs/plan-mvp-live-chat.md` and `docs/plan-mvp-docs.md`. That is 23 files, `docs/plan-sender-hash.md` included.
2. In every tracked file outside `docs/archive/`, replace each path `docs/plan-<name>.md` of a moved file with `docs/archive/plan-<name>.md`. Relative markdown links (`](plan-x.md)` inside `docs/`) become `](archive/plan-x.md)`. Do not touch the three plans that stay, `git` history, or any file inside `docs/archive/`.
3. Append two rows after D160 in `specs/05-open-questions.md`, in the same table format:
   - `| D161 | **Architecture diagrams are Mermaid in docs/architecture.md.** (9i plan, proposed). A view of spec 03 and the code, not a second source of truth: spec 03 still decides. GitHub renders Mermaid and it diffs as text. tests/unit/test_docs.py fails when a state in states.py or a package under src/invoicebot is missing from the diagrams. |`
   - `| D162 | **Finished phase plans move to docs/archive/.** (9i plan, proposed). docs/ keeps the roadmap, the open plans and the runbook. plan-next reads only docs/plan-*.md and counts a missing plan as finished. Files inside docs/archive/ are history and are not edited. |`

   Wrap the path names in backticks as the other rows do.

Test: `git grep -nE 'docs/plan-[a-z0-9-]+\.md' -- ':!docs/archive' | grep -vE 'docs/plan-(mvp|mvp-live-chat|mvp-docs)\.md'` prints nothing; `ls docs/plan-*.md` lists exactly the three kept plans; `plan-next --body` still runs (it reports this plan).

## Step 2: Fix stale package docstrings and the duplicate uptime section `[worker:low]`

Files: `src/invoicebot/pipeline/__init__.py`, `src/invoicebot/store/__init__.py`, `deploy/README.md`.

1. `src/invoicebot/pipeline/__init__.py`: the docstring becomes `"""Pipeline: the job steps, chat replies and commands that move an invoice through its states (spec 03 §4–§7)."""`. It is no longer a placeholder.
2. `src/invoicebot/store/__init__.py`: the docstring becomes `"""Postgres store: the connection pool and one module of SQL functions per table group; the caller owns the transaction."""`.
3. `deploy/README.md` has two uptime sections. Delete the subsection `### 6. Uptime check (R-9.14)` under "Local MVP host" (the heading and its one paragraph), renumber `### 7. Dev and live side by side` to `### 6.`, and append this sentence to the paragraph of the remaining `## Uptime check (R-9.14)` that ends "alert on any non-200.": `On the local MVP host that URL is https://<machine>.<tailnet>.ts.net/health (D146).` (with the URL in backticks). Fix any link or text in the file that points at the old `### 6.` or `### 7.` numbers.

Test: `uv run ruff check src/invoicebot/pipeline src/invoicebot/store && uv run ruff format --check src/invoicebot/pipeline src/invoicebot/store && uv run mypy src`; `grep -c "Uptime check" deploy/README.md` prints `1`.

## Step 3: Drift tests for the docs `[worker]`

Files: `tests/unit/test_docs.py` (new).

Read the files relative to the repo root (`Path(__file__).parents[2]`). A helper `_mermaid_blocks(text: str) -> list[str]` returns the bodies of the ` ```mermaid ` fenced blocks. Every test docstring starts with `D161`. Cases:

- `test_state_diagram_names_every_state`: the first mermaid block in `docs/architecture.md` whose body starts with `stateDiagram-v2` contains, as a whole word, every module-level `str` constant of `invoicebot.states` whose value equals its name (`RECEIVED` … `FAILED`, 17 today). Collect them with `vars(states)`, not a hand-written list. Expected now: fails with `FileNotFoundError` (no `docs/architecture.md`); passes after step 5.
- `test_component_diagram_names_every_package`: the first mermaid block whose body starts with `flowchart` contains, as a whole word, the name of every directory under `src/invoicebot/` that holds an `__init__.py`, except `api` and `events` (v1.1 placeholders). Expected now: fails (file missing); passes after step 4.
- `test_readme_lists_every_cli_command`: for every subcommand name of `invoicebot.cli._parser()` (read from its `_SubParsersAction.choices`), `README.md` contains `invoicebot <name>`. Expected now: fails (the README names none); passes after step 6.
- `test_doc_links_resolve`, parametrized over `README.md`, `docs/architecture.md`, `docs/runbook.md`, `deploy/README.md`: every markdown link target `](target)` that has no URL scheme and does not start with `#` exists on disk, resolved against the linking file's directory after dropping any `#anchor`. Expected now: fails for `docs/architecture.md` (missing file); the other three pass after step 1, or name the broken link.

Test: `uv run pytest -q tests/unit/test_docs.py` fails only in the expected ways above; `uv run ruff check tests/unit/test_docs.py && uv run ruff format --check tests/unit/test_docs.py`.

## Step 4: Components and deploy topology diagrams `[inline: the diagrams must match the code, which is judgment, not a spec the worker can hold]`

Files: `docs/architecture.md` (new).

1. A short intro: what the page is (a view of spec 03 and the code, D161) and links to `specs/03-design.md`, `specs/06-business-rules.md` and `deploy/README.md`.
2. **Components:** a `flowchart LR` that names every package under `src/invoicebot/` except `api` and `events`, grouped as edge (channels, webhook in `app.py`), core (pipeline, routing, rules, extract, queue, store), adapters (fetchers, sinks, sunat, github, auth, net), and runtime (config, ops, gaps), with the arrows the import contracts in `.importlinter` allow. Helper processes (`helper.py`, `sandbox.py`) and the outbox are shown as nodes. Under the diagram, one line per package taken from its `__init__.py` docstring.
3. **Deploy topology (local MVP host, D141, D146–D148):** a `flowchart` showing Meta → Tailscale Funnel → the `invoicebot-live` Compose project (app container with the sandboxed helpers, Postgres), and the outbound calls: Meta Graph, Microsoft Graph (OneDrive, Excel, mail), Google (Drive, Sheets, Gmail), store portals through `GuardProxy`, SUNAT, GitHub App, nightly `age` backups to OneDrive. Facts come from `deploy/README.md`, `deploy/compose.live.yml` and `deploy/deploy-live.sh`.

Test: `uv run pytest -q tests/unit/test_docs.py -k "component or links"` passes; both blocks render in the GitHub preview (checked in the PR).

## Step 5: State machine and flow diagrams `[inline: transitions are read from the pipeline code, which is judgment]`

Files: `docs/architecture.md`.

1. **Invoice state machine:** a `stateDiagram-v2` with all 17 states of `invoicebot/states.py` and the transitions the pipeline actually makes (read from `pipeline/` and `store/invoices.py`, checked against spec 03 §4). Mark the terminal states and note under the diagram what `WAITING_FOR_USER`, `CANCELLABLE` and `EDITABLE` hold. Where the code and spec 03 §4 disagree, stop and report it (repo rule) rather than drawing either.
2. **Flow A and flow B:** one `sequenceDiagram` each, from the photo to the reply: flow A (RK EXPORT, PDF only, Drive + Sheets, D142), flow B (CIGNIUM, portal or inbox fetch, OneDrive folder + Excel + mail, laptop agent). One sentence names the `personal_dni` flow (Sheets row only, D143) without a diagram.

Test: `uv run pytest -q tests/unit/test_docs.py -k "state or links"` passes; the blocks render in the GitHub preview.

## Step 6: README refresh `[inline: user-facing copy]`

Files: `README.md`.

Rewrite it with these sections, each short:

1. **What it is:** WhatsApp bot (Telegram is planned for v1.1, not built), the five steps it does today, the standalone/contract sentence about Jarvis.
2. **Status:** phases done up to 9i, live on the local host (M7), 9d2 `live-chat` waits for Meta business verification; link to `docs/plan-mvp.md`.
3. **Quickstart (dev):** `uv sync`, Postgres with `podman compose -f deploy/docker-compose.yml up -d postgres`, a config copied from `config/example.yaml`, the secrets store via `scripts/secrets_edit.py`, running the app, and the test, lint and type commands from `CLAUDE.md`. Name environment variables, never values. Every command checked by running it, or by `--help` where running needs real accounts.
4. **CLI:** every `invoicebot` subcommand from `cli._parser()` with one line each, plus the scripts in `scripts/` the runbook uses.
5. **Layout:** the top-level folders and one line per `src/invoicebot` package; link `docs/architecture.md`.
6. **Docs map:** the specs table (kept), `docs/architecture.md`, `docs/runbook.md`, `deploy/README.md`, `docs/plan-mvp.md`, `docs/archive/`.

Test: `uv run pytest -q tests/unit/test_docs.py` passes in full.

## Step 7: `CLAUDE.md` and `.claude/` audit `[inline: judging which guidance is stale is judgment]`

Files: `CLAUDE.md`, files under `.claude/` that a check below finds stale.

Checks, each fixed in place or, when it is the owner's call, listed in the commit body:

- `CLAUDE.md` first line: "WhatsApp and Telegram" becomes WhatsApp, with Telegram in v1.1. The Roadmap line says finished phase plans are in `docs/archive/` (D162).
- Every command in the Commands block runs as written (the full-suite line excepted; it runs in the finish).
- Every path, file and agent `CLAUDE.md` names exists.
- Each `.claude/rules/*.md` rule that conflicts with `CLAUDE.md` is named in an override line in `CLAUDE.md` (known: layout, tests mirror, secrets from env). Rules that name a tool or agent the repo lacks (for example `py-performance-profiler` in `python-py-code-quality.md`) are reported; the rules are generated files, so the fix goes in the commit body for the owner rather than an edit, unless the file says it is hand-maintained.
- `.claude/settings.json`: every hook command it references exists and is executable.
- `.claude/agents/*.md`: the paths and rule files they cite exist.

Test: the Commands block's commands pass; `uv run pytest -q tests/unit/test_docs.py` still passes.
