# Plan: MVP phase 9z3 · forget

Roadmap: `docs/plan-mvp.md`, row 9z3. The owner asked on 2026-11-26 for an emergency way to forget a filed invoice, so
the same photo can be sent again from a clean slate, and to use it right away on live invoice #34 before the
`review-ui` live check.

What the code does today: nothing removes an invoice. A second copy of a filed invoice is refused by its natural key
(BR-E04, `store/invoices.claim_natural_key`); only a `FAILED` or `CANCELLED` holder gives the key up. The flow B folder
stays on OneDrive (its current path in `flow_data.folder_path`, `_Sent` after the send), and the next filing of the
same month takes the next sequence number, because `claim_seq` counts both the invoices' claimed numbers and the
folders listed on the drive (D88). Thirteen tables reference `invoices(id)`; only `portal_candidates` cascades and
`portal_probes` sets null. On 2026-11-26 #34's key was released by hand (`UPDATE invoices SET natural_key = NULL`).

Out of scope: a chat command (a stray message must never delete anything); flow A's Sheet row and Drive folder (the
command refuses `--drive` for a flow without a OneDrive folder sink and says so); recalling a mail already sent.

Needs from the owner: review of D196; the live run in step 6.

Decision this plan proposes (step 1), **D196 forget an invoice**:

- A host command, `invoicebot invoice forget <id> [--drive] [--purge] [--yes]`, run in the app container like the
  other host commands. Without `--yes` it only prints what it would do and changes nothing.
- Only an invoice in a terminal state (`DONE`, `CANCELLED`, `FAILED`) can be forgotten; any other state exits 2 and
  names `/cancelar` first, so the command never races a running job.
- Always: the invoice's natural key is released, so the same invoice can be sent again.
- `--drive`: the invoice's folder (`flow_data.folder_path`) is deleted through the bot's own Graph drive; a folder
  already gone is fine. A flow without a OneDrive folder sink refuses `--drive` with exit 2 before any change.
- `--purge`: the invoice's host files (`tmp/<id>/`, `mail/<id>/`, `mail/<id>.eml`, each `files.local_path` under the
  data directory) are removed and its database rows are deleted: every table that references it, then the invoice,
  then its batch when no other invoice uses it. `llm_usage` rows are deleted too: nothing writes them
  yet (2026-11-26), and their `invoice_id` is `NOT NULL`.
- Order: the drive first, then the host files, then the database in one transaction with the invoice row locked. A
  drive failure stops before anything else changes; rerunning is safe because every part accepts what is already gone.
- With `--drive --purge` the month's next filing reuses the sequence number, since neither the folder nor the
  invoice's claim remains (D88). The command prints one line per part and what it removed.

## Step 1: D196, the roadmap row and the runbook `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`, `docs/runbook.md`.

- `specs/05-open-questions.md`: add row D196 after D195 with the decision above, marked "(9z3 plan, proposed
  2026-11-26, the owner asked for it)".
- `docs/plan-mvp.md`: add row 9z3 `forget` after 9z2 (plan `docs/plan-mvp-forget.md`, D196).
- `docs/runbook.md`: a new section "13. Forget an invoice" with the dry run, `--yes`, `--drive`, `--purge`, the
  terminal-state rule and the sequence-number effect.

Test: `npx prettier --check docs/runbook.md docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: the database part `[worker]`

Files: `src/invoicebot/store/forget.py` (new), `tests/integration/test_store_forget.py` (new).

Module docstring "Forget an invoice in the database: release its key or delete its rows (D196).". Functions take the
caller's connection and never commit.

- `@dataclass(frozen=True) class ForgetTarget`: `invoice_id: int`, `state: str`, `flow_id: str | None`,
  `serie: str | None`, `numero: str | None`, `folder_path: str | None`, `local_paths: tuple[str, ...]` (the non-null
  `files.local_path` values).
- `async def lock_target(conn, invoice_id) -> ForgetTarget | None`: `SELECT ... FOR UPDATE` on the invoice, plus its
  files' local paths; `None` when there is no such invoice.
- `async def release_key(conn, invoice_id) -> None`: `UPDATE invoices SET natural_key = NULL, updated_at = now()`.
- `async def purge(conn, invoice_id) -> dict[str, int]`: deletes the invoice's rows from `llm_usage`, `files`, `jobs`, `fetch_attempts`, `invoice_gaps`, `upload_rejections`,
  `pending_actions`, `events`, `messages`, `outbox`, `mail_jobs`, `portal_candidates`, then the invoice, then its
  batch when no other invoice has that `batch_id`. Returns the row count per table. One SQL constant per statement,
  each parameterized; the table list lives in one tuple so a new referencing table is one line. Before writing it,
  check the current schema for every foreign key to `invoices(id)` (`pg_constraint`) and cover all of them.

Cases (docstrings start with `D196:`; synthetic values only):

1. `lock_target` of a missing id is `None`.
2. `lock_target` returns the state, flow, serie, numero, `folder_path` and the files' local paths.
3. `release_key` leaves the row and clears `natural_key`; a new invoice can then claim the same key
   (`claim_natural_key` returns `None`).
4. `purge` of an invoice with a row in each referencing table deletes them all and the invoice; the counts match.
5. `purge` deletes the invoice's `llm_usage` row; a `portal_probes` row is kept with `invoice_id` null (its
   `ON DELETE SET NULL`).
6. `purge` deletes the invoice's batch when it is the batch's only invoice, and keeps a batch another invoice uses.
7. A guard test: every foreign key to `invoices(id)` in the live schema is covered by `purge` (reads `pg_constraint`),
   so a new table fails this test until it is added.

Test: `uv run pytest -q tests/integration/test_store_forget.py`

## Step 3: the forget operation `[worker]`

Files: `src/invoicebot/ops/forget.py` (new), `tests/integration/test_ops_forget.py` (new).

Module docstring "Forget a terminal invoice: its drive folder, host files and database rows (D196).".

- `@dataclass(frozen=True) class ForgetRequest`: `invoice_id: int`, `drive: bool`, `purge: bool`, `apply: bool`.
- `class ForgetRefused(Exception)`: carries the reason text (unknown invoice, not terminal, `--drive` without a
  folder or without a drive).
- `async def forget(pool, data_dir: Path, drive: Drive | None, request: ForgetRequest) -> list[str]`: returns the
  report lines.
  1. In a transaction, `lock_target`; refuse a missing invoice, a non-terminal state (the reason names `/cancelar`),
     and `--drive` when `drive` is `None` or `folder_path` is empty. Without `apply`, return the lines of what would
     happen and change nothing.
  2. With `apply`: `--drive` first, `await drive.delete(folder_path)` (a folder already gone is accepted by the
     port). Then with `--purge`, remove `tmp/<id>/`, `mail/<id>/`, `mail/<id>.eml` and each local path, each only
     when it resolves under `data_dir` (`is_relative_to`), via `asyncio.to_thread`, missing ones accepted. Then one
     transaction: lock again, refuse if it left its terminal state meanwhile, then `purge` or `release_key`.
- Report lines, Spanish-free (host output): `invoice 34 (F014-15828, DONE, flow work_microsoft)`,
  `drive: deleted /Reembolsos/…` or `drive: kept`, `files: removed 3` or `files: kept`,
  `database: purged (outbox 5, events 12, …)` or `database: key released`, and `dry run: nothing changed` without
  `apply`.

Cases (docstrings start with `D196:`; a fake `Drive` recording `delete`, real Postgres, `tmp_path` data dir):

1. A dry run on a `DONE` invoice returns the plan, calls no `delete`, and leaves the row, its key and the files.
2. `apply` without flags releases the key and keeps the row, the folder and the files.
3. `apply, drive` deletes `folder_path` once and releases the key.
4. `apply, drive, purge` deletes the folder, removes the host files and deletes the rows.
5. A `BATCHED` invoice raises `ForgetRefused` naming `/cancelar`, and nothing changes.
6. A missing id raises `ForgetRefused`.
7. `--drive` with `drive=None`, or with no `folder_path`, raises `ForgetRefused` before any change.
8. A drive `delete` that raises `GraphError` propagates, and the row, key and files are unchanged.
9. A `files.local_path` outside the data directory is not removed and the report says so.

Test: `uv run pytest -q tests/integration/test_ops_forget.py`

## Step 4: the host command `[worker]`

Files: `src/invoicebot/cli.py`, `tests/unit/test_cli.py`.

- `invoicebot invoice forget <id> [--drive] [--purge] [--yes]` (`invoice` subparser with a `forget` subcommand). It
  needs `DATABASE_URL` (parser error otherwise, as `_auth_microsoft` does), builds the pool, and with `--drive` the
  personal Graph drive the same way `app._personal_drive` does (reuse it; move it to a shared place only if the
  import would cross a contract). It calls `ops.forget.forget` with `apply=args.yes`, prints each line, and exits 0;
  `ForgetRefused` prints its reason to stderr and exits 2; `AuthRequired` says to run `auth-microsoft` and exits 1.
- `--help` text says a run without `--yes` changes nothing.

Cases (docstrings start with `D196:`; patch `ops.forget.forget` at the module the CLI imports, as `test_cli.py` does
for other commands):

1. `invoice forget 34` calls `forget` with `apply=False, drive=False, purge=False` and prints its lines.
2. `invoice forget 34 --drive --purge --yes` passes all three as true.
3. `ForgetRefused("not terminal")` exits 2 with the reason on stderr.
4. No `DATABASE_URL` is a parser error (exit 2).

Test: `uv run pytest -q tests/unit/test_cli.py`

## Step 5: wire-up check and deploy `[inline: live host deploy]`

Files: none.

Build the image from the branch, `./deploy/deploy-live.sh <tag>`, then in the app container
`invoicebot invoice forget 34` (dry run) and check that it names #34, its folder and its files.

Test: the dry run prints the plan and the live row is unchanged.

## Step 6: live run on #34 `[inline: needs the owner and the live data]`

Files: `specs/spikes/forget.md` (new).

`invoicebot invoice forget 34 --drive --purge --yes`; check with the read-only `rclone` remote that the folder is
gone, and in the database that #34 and its rows are gone. Record the output (no RUCs, no addresses) and what the
owner sees when the same photo is sent again (it files as `…_Support_2_…`, no duplicate answer).

Test: the spike file records each check with its result.
