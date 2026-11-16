# Plan: MVP phase 6d · widen-b-deliver

Roadmap: `docs/plan-mvp.md` row 6d. The rest of flow B's delivery: the `_Sent` and `_PAID` folder names and the year move (S11.12), `/pagado` and payment matching (S11.11), the `inbox_rule` and `laptop_outlook` mail modes and how each one completes (S11.15), the laptop agent (S11.14), the go-live import (S11.17), plus the review leftovers carried to this phase.

**Goal:** a flow B invoice whose email went out gets a `_Sent` folder. `/pagado 15/12 144.60` finds the sent, unpaid invoices that add up to the amount and asks once. On [Sí] the folders become `…_Sent_PAID_15_12___144_6` inside the `2026/` year folder. The bot can send through the owner's Outlook rule (`inbox_rule`) or hand the email to the laptop agent (`laptop_outlook`), and the laptop agent's receipt completes the batch. Before go-live, the import script registers the owner's existing sent-but-unpaid claims.

**Needs from the owner first:** none of the prerequisites are done (owner, 2026-11-14), and the owner said to draft anyway. Nothing in this plan needs them for code or tests:

- S0.11 (the fallback tests): the laptop agent is tested with Pester against a fake Outlook object, and the Outlook rule is only documented. Results that contradict this plan (COM blocked, the rule's forward lands in Junk) are found at the live check and change the mode order in config, not the code.
- The bot's Outlook.com mailbox: needed only to send live through `inbox_rule`.
- The M3 run: still deferred. The live check at the end of this plan runs after it.
- S0.10 (the ops repo and GitHub App): goes with the gap service to 6e (D105).

## Design choices

Step 1 writes these into the specs (spec 05, D105 to D110).

- **D105 · 6d is split.** The roadmap row lists about 50 steps' worth of stories. 6d `widen-b-deliver` keeps S11.11, S11.12, S11.14, S11.15 and S11.17 plus the carried-over items. A new row 6e `widen-b-gaps-ops` takes E8 (S8.1 to S8.3) and E15 (S12.3, S15.1, S15.2), plus the gap leftovers: the `PORTAL_MISSING` and `TEMPLATE_INVALID` `shortcut:` logs, BR-E06, BR-R06 and BR-F03. Its owner prerequisites are S0.10, and an `age` recipient and the personal OneDrive target for backups. M4 is reached when 6e ends.
- **D106 · One job converges a folder's name.** A single `folder_status` job computes the name the folder should have from the database: the base name, plus `_Sent` when the batch is `sent`, plus `_PAID_…` and the year folder when the invoice has a payment. It moves the folder there from wherever it is now. It runs after every change (sent, paid), so a payment recorded before the `_Sent` rename has run still ends with one correct name. The folder's current path lives in `flow_data_json.folder_path`. The handler looks for the folder by that path and then by the target path, so a move that Graph applied but the bot never recorded is found again: a folder is never lost (R-7.11). `Drive` gets one method, `move(path, new_parent, new_name)`.
- **D107 · Payment matching.** The candidates are the sender's flow B invoices that are `DONE` with a `sent` batch and no `payment_id`, created in the last 180 days, at most 20, oldest first (R-7.12). An invoice's claimed amount is `min(total, reimbursement.cap)` (R-7.13), compared in cents. The matcher is a pure function. It enumerates subsets with pruning (amounts are positive and sorted), returns up to 3 matching sets, oldest invoices first, and stops after the third. The `#id` form only checks that the named invoices are candidates and add up. A proposal is stored in `payment_proposals` (profile, date, amount, its sets, state), and its buttons carry the proposal id as `p<proposal id>:pay:<set index>` and `p<proposal id>:pay_no`. Intake hands any press that starts with `p` to `pipeline/payments.py`, because a payment belongs to no single invoice, so `resolve_reply` does not apply to it. On confirm, the `payments` row, each invoice's `payment_id` and the `folder_status` jobs commit in one transaction.
- **D108 · How a mail mode completes.** `MailMode.automatic: bool` becomes `completion: Literal["accepted", "receipt", "tap"]` and `done_rule`. `graph` is `accepted` with BR-D10, `inbox_rule` is `accepted` with BR-D13, `laptop_outlook` is `receipt` with BR-D10, and `dry_run` is `tap` with BR-D08. `deliver.py` picks the path from a mapping keyed by `completion`, not an `if`. `inbox_rule` sends through Graph from account `bot` (the D63 mailbox, which now needs `Mail.Send` under D98's per-account rule) to `mail.owner_work_address` only, and refuses any other recipient. `laptop_outlook` writes an HMAC-signed job file to `_outbox/` in the personal OneDrive (HMAC-SHA256 over the canonical JSON without `hmac`, with the age-store secret `laptop_agent_key`) and records it in `mail_jobs`.
- **D109 · What [No llegó] and BR-D11 do.** Nothing is ever re-sent without the sender's choice. [No llegó] on BR-D13, an error receipt, or a `laptop_outlook` receipt overdue by `mail.confirm_timeout` (default 24 h) sends BR-D11 with three buttons. [Por la laptop] writes a new laptop job; the button is shown only when the flow lists `laptop_outlook`. [Lo envío yo] writes the `.eml` draft and sends BR-D07's text; it adds [Ya lo envié] only while the batch is `prepared`. [Ya llegó] completes a `prepared` batch with BR-D10, and on a `sent` batch it only answers BR-D10. An `inbox_rule` batch stays `sent` and its folder stays `_Sent` through all of this: the rule was trusted (D67), and the sender's choice only adds another way out. BR-D12 is sent once per job file still in `_outbox/` after `mail.agent_stale_after` (default 4 h).
- **D110 · The go-live import.** `scripts/import_claims.py --profile <id> <folder>…` reads each folder's name and its XML. It registers only folders named `_Sent` and not `_PAID` (sent, unpaid), as `DONE` flow B invoices with a `sent` batch and `flow_data_json` `{folder, folder_path, seq, year, month, comercio, imported: true}`. It skips a natural key that already exists, so a second run changes nothing. It never writes to the folders. `--dry-run` prints what it would register. The folders are a local copy (the synced OneDrive on the laptop, or a copy on the host), so the script needs no Graph sign-in.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1. No real RUCs, phone numbers or mail addresses in tests; use the synthetic values of `config/example.yaml`. The Pester tests run with `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"` (Pester 5 or later).

Out of scope here (6e, D105): E8, E15, the gap `shortcut:` logs. Also out: `on_command` and `scheduled` triggers (post-MVP), `forward_to_self` (no caller yet; `inbox_rule` covers the trusted path), the `onedrive_work` target.

## Step 1: Record D105–D110 and the roadmap split `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Spec 05: rows D105 to D110 with the text from "Design choices".
- Spec 06: under BR-D11, the button behavior of D109. Under BR-P02, the button label format `{fecha_min}…: {n} facturas, #12 #14`. The admin-free texts stay as they are.
- Spec 04: S11.15 cites D108 and D109; S11.11 cites D107; S11.12 cites D106; S11.17 cites D110.
- Spec 03 §5 (data model): the tables `payments`, `payment_proposals` and `mail_jobs`, the columns `invoices.payment_id` and `outbox.channel_message_id`, and the mail config keys `mail.owner_work_address`, `mail.confirm_timeout`, `mail.agent_stale_after`.
- Roadmap: row 6d lists only its S11 stories plus "review leftovers (`widen-b-fetch`)"; a new row 6e `widen-b-gaps-ops` takes E8 (S8.1–S8.3) and E15 (S12.3, S15.1, S15.2), milestone M4, prerequisites "S0.10; an `age` recipient and the backup target". The `shortcut:` gap line under "Moved by the `file-b` plan" and the "E8 owns BR-E06 and BR-R06" note name `widen-b-gaps-ops`. The five `widen-b-deliver` items under "Carried over from the `widen-b-fetch` review" are marked `(widen-b-deliver)`.

Test: none (spec text only); `uv run pytest -q tests/unit/test_rules_parity.py` still passes.

## Step 2: Per-fetcher retry schedules built in `app.py` `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_fetch.py`.

- `_schedule_entries` (`pipeline/fetch.py:371`) loses its `if fetcher == …` branches. `make_fetch_handler` takes `schedules: Mapping[str, Callable[[FlowConfig, Invoice], tuple[timedelta, ...]]]`, one entry per fetcher that has its own schedule. A fetcher without an entry uses `DEFAULT_RETRY_SCHEDULE`. Use the handler's real flow and invoice types; the names here are placeholders.
- `app.py` builds the mapping next to `_fetcher_builders`: `EMAIL_INBOX_FETCHER` → the flow's `email_inbox.retry_schedule`, `PORTAL_FETCHER` → the portal's `retry_schedule`, each falling back to the default.
- Tests (existing schedule tests keep passing; add): R-5.2 a fetcher with no entry in `schedules` retries on `DEFAULT_RETRY_SCHEDULE`; R-5.2 an entry for a made-up fetcher name is used for that fetcher, with no change to `fetch.py`.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py -k schedule`.

## Step 3: `ButtonPress` and `MENU_MAX` move to `channels/base.py` `[worker:low]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/channels/whatsapp/payloads.py`, plus every module that imports either name from `channels.whatsapp.payloads` (find them with `grep -rn "ButtonPress\|MENU_MAX" src tests`).

- Move the `ButtonPress` NamedTuple and `MENU_MAX` into `channels/base.py` unchanged. `payloads.py` imports them from there. Every other importer imports them from `invoicebot.channels.base`. No re-export from `payloads.py`.
- No behavior change.

Test: `uv run pytest -q tests/unit/test_whatsapp_payloads.py tests/integration/test_pipeline_actions.py` (use the real payloads test file name if it differs).

## Step 4: A channel-keyed media download port for uploads `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/pipeline/uploads.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_uploads.py`.

- `channels/base.py`: `class MediaSource(Protocol)` with one method, `async def download(self, media_ref: str, kind: str, out_dir: Path, limits: MediaLimits) -> Path`, matching what `download_media` takes today (`pipeline/uploads.py:213`; copy its real return type).
- `make_attach_handler` takes `media: Mapping[str, MediaSource]` (keyed by channel name) instead of `client: WhatsAppClient`. `_download` looks up the upload's channel and calls its source. An unknown channel raises `PermanentJobError` (or the project's equivalent permanent-failure path for jobs; check `queue/jobs.py`).
- `app.py` wraps the WhatsApp client in a small adapter class in `channels/whatsapp/media.py` that implements `MediaSource` with `download_media`, and passes `{WHATSAPP_ADAPTER: adapter}`. That class is the one addition outside the three named files.
- Tests use a fake `MediaSource` instead of the WhatsApp client: R-1.5 an upload is downloaded through the source of its channel; R-1.5 an upload from a channel with no source fails the job permanently.

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py`.

## Step 5: Import-linter contract: the pipeline never names the WhatsApp adapter `[worker:low]`

Files: `.importlinter`, `src/invoicebot/pipeline/*.py` (only if an import remains), `tests/unit/test_import_boundary.py`.

- Add a `forbidden` contract `pipeline-is-channel-neutral`: `invoicebot.pipeline` must not import `invoicebot.channels.whatsapp`. Run `uv run lint-imports`. If `pipeline/handlers.py`, `pipeline/replies.py` or another module still imports it, list those imports under `ignore_imports` with a comment `# Telegram (ops): media port and button codec move then` each, as the roadmap's "Carried over from the `whatsapp` review" item says. Do not refactor them here.
- `test_import_boundary.py`: R-1.7 the contract name appears in `.importlinter`.

Test: `uv run lint-imports && uv run pytest -q tests/unit/test_import_boundary.py`.

## Step 6: The outbox stores the channel message id `[worker]`

Files: `migrations/versions/0008_outbox_message_id.py`, `src/invoicebot/queue/outbox.py`, `tests/integration/test_outbox.py`.

- Migration (expand only): `ALTER TABLE outbox ADD COLUMN channel_message_id TEXT`, plus `CREATE INDEX ix_outbox_channel_message ON outbox (channel, channel_message_id) WHERE channel_message_id IS NOT NULL`.
- `Transport` becomes `Callable[[OutboxRow], Awaitable[str | None]]`. On success, `OutboxSender` stores the returned id: `UPDATE outbox SET state='sent', sent_at=now(), channel_message_id=%s`.
- Tests: D102 a transport returning `"wamid.X"` leaves that id on the sent row; D102 a transport returning `None` leaves it NULL; existing dead/retry tests keep passing.

Test: `uv run pytest -q tests/integration/test_outbox.py tests/integration/test_schema.py`.

## Step 7: The WhatsApp transport returns Meta's message id `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `src/invoicebot/channels/whatsapp/client.py`, `tests/unit/test_whatsapp_transport.py` (use the real file name).

- `_send` returns `messages[0].id` from Meta's response. A response without it returns `None` and logs a warning with the row id only. The transport returns that value, so every send path (free form and template) passes it on.
- Tests with `httpx.MockTransport`: D102 a 200 with `{"messages":[{"id":"wamid.A"}]}` returns `"wamid.A"`; D102 a 200 without `messages` returns `None`; D102 a template send returns its id too.

Test: `uv run pytest -q tests/unit/test_whatsapp_transport.py`.

## Step 8: A quote of a bot message links the upload to its invoice `[worker]`

Files: `src/invoicebot/store/messages.py` (where `find_open_invoice_for_attachment` lives; check), `tests/integration/test_pipeline_uploads.py`.

- `find_open_invoice_for_attachment(conn, profile_id, reply_to)`: when `reply_to` matches no inbound message, it also looks up `SELECT invoice_id FROM outbox WHERE channel=%s AND channel_message_id=%s`. The invoice must still be open for an upload and belong to the profile, under the same rule as the inbound match.
- Tests: D102 an upload quoting the BR-F07 message of invoice A, with A and B both stuck, links to A; D102 a quote of a bot message about another profile's invoice links nothing and answers BR-D14; D102 a quote of an unknown id with one stuck invoice links to it (unchanged).

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py -k quote`.

## Step 9: The inbox search covers the zero-padded number `[worker]`

Files: `src/invoicebot/fetchers/email_inbox.py`, `tests/unit/test_fetchers_email_inbox.py` (use the real file name).

- Search twice, for `f"{serie}-{numero}"` and for `f"{serie}-{numero:0>8}"`, then once more for the serie alone as today. Merge the results by message id and keep the order of the first time each message is seen. The name match (`email_inbox.py:174`) is unchanged. Skip a duplicate term (a numero that already has 8 digits).
- Tests with a fake mailbox: R-5.2 an invoice `F001`/`123` whose mail says `F001-00000123`, with 25 newer `F001` mails crowding the serie search, is found by the padded term; R-5.2 a message returned by two searches is read once; R-5.2 an 8-digit numero runs one exact search, not two.

Test: `uv run pytest -q tests/unit/test_fetchers_email_inbox.py`.

## Step 10: No cleanup of committed files after a late cancellation `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/uploads.py`, `tests/integration/test_pipeline_fetch.py`.

- In `_run` (`fetch.py:168-208`) and the attach handler (`uploads.py:152-161`), set a local `committed = True` as the last statement inside the `conn.transaction()` block. The `except BaseException` cleanup removes `out_dir` only when `committed` is false, then re-raises.
- Test: R-5.1 a fetch whose `pool.connection()` exit raises `asyncio.CancelledError` after the transaction commits keeps the files its committed rows point to. Use a pool wrapper whose connection context raises on exit. Add the same test for an upload in `test_pipeline_uploads.py`; that file is the step's one extra.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py tests/integration/test_pipeline_uploads.py -k cancel`.

## Step 11: `Drive.move` `[worker]`

Files: `src/invoicebot/sinks/base.py`, `src/invoicebot/sinks/onedrive.py`, `tests/unit/test_sinks_onedrive.py` (use the real file name).

- `Drive` gets `async def move(self, path: str, new_parent: str, new_name: str) -> None`. `GraphDrive.move` sends `PATCH /me/drive/root:/{path}` with `{"name": new_name, "parentReference": {"path": f"/drive/root:{new_parent}"}}`, path-encoded like `list_names`. A 404 raises `FolderMissing(GraphError)`, a 409 raises `FolderExists(GraphError)`, and other errors follow the existing `GraphError`/`GraphTransient` mapping.
- Tests with `httpx.MockTransport`: R-7.11 the PATCH body and URL for a move into `/Reembolsos/2026`; R-7.11 404 → `FolderMissing`; 409 → `FolderExists`; 503 → `GraphTransient`. Any fake `Drive` in the tests gets a `move` that records calls.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive.py`.

## Step 12: The folder name for a status (pure) `[worker]`

Files: `src/invoicebot/sinks/folders.py`, `tests/unit/test_sinks_folders.py`.

- `amount_label(amount: Decimal) -> str`: the payment total with trailing zeros dropped and `_` for the point: `144.60` → `144_6`, `42.00` → `42`, `143.90` → `143_9`, `0.05` → `0_05`.
- `status_name(base: str, sent: bool, paid_on: date | None, payment_total: Decimal | None) -> str`: `base`, plus `_Sent` if sent, plus `_PAID_{dd}_{mm}___{amount_label}` if paid. `folder_parent(root: str, year: int, paid: bool) -> str`: `root` or `f"{root}/{year}"`.
- Tests: R-7.11 the four amounts above; R-7.11 `2026-11-Support_2_LaLena` → `…_Sent` → `…_Sent_PAID_15_12___144_6` in `/Reembolsos/2026`; R-7.11 paid but not sent gives `…_PAID_…` without `_Sent`; `seq_pattern` still matches every one of these names (R-7.1).

Test: `uv run pytest -q tests/unit/test_sinks_folders.py`.

## Step 13: Schema for payments and mail jobs `[worker:low]`

Files: `migrations/versions/0009_payments.py`, `tests/integration/test_schema.py`.

- `payments(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, flow_id TEXT NOT NULL, profile_id TEXT NOT NULL, paid_on DATE NOT NULL, amount NUMERIC(12,2) NOT NULL CHECK (amount > 0), created_at TIMESTAMPTZ NOT NULL DEFAULT now())`.
- `ALTER TABLE invoices ADD COLUMN payment_id BIGINT REFERENCES payments(id)`.
- `payment_proposals(id BIGINT IDENTITY PK, profile_id TEXT NOT NULL, flow_id TEXT NOT NULL, paid_on DATE NOT NULL, amount NUMERIC(12,2) NOT NULL, sets_json JSONB NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','confirmed','rejected','expired')), created_at TIMESTAMPTZ NOT NULL DEFAULT now())`.
- `mail_jobs(job_id TEXT PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), receipt_at TIMESTAMPTZ, receipt_error TEXT, stale_alerted_at TIMESTAMPTZ, overdue_alerted_at TIMESTAMPTZ)`, with an index on `invoice_id`.
- Tests: R-7.12 the tables and columns exist after `upgrade head`; R-7.12 a payment with amount 0 is refused.

Test: `uv run pytest -q tests/integration/test_schema.py`.

## Step 14: The `folder_status` job `[worker:high: reasoning: the handler must converge from any partial state (moved but not recorded, paid before sent) without ever losing a folder; the spec lists the cases but the recovery order needs judgment]`

Files: `src/invoicebot/pipeline/folder_status.py` (new), `src/invoicebot/pipeline/steps.py`, `tests/integration/test_pipeline_folder_status.py` (new).

- `FOLDER_STATUS_STEP = "folder_status"` in `steps.py`. `make_folder_status_handler(pool, cfg, drive) -> Handler`.
- It reads the invoice's `flow_data_json` (`folder`, `folder_path`, `year`), its batch state and its payment, and computes the target from step 12 (D106). If `folder_path` is the target, it does nothing. Otherwise it ensures the year folder exists when paid (`list_names(root)`, then `create_folder`), then moves `folder_path` to the target. `FolderMissing` on the source: if the target exists (`list_names` of the target parent holds the name), record the target and finish; if neither exists, raise a permanent error and log the invoice id, never create a folder. `FolderExists` on the target: same check. It records `folder_path` with `merge_flow_data` after the move, in its own transaction. Transient Graph errors raise so the job retries.
- A `folder_path` missing from older rows defaults to `f"{root}/{folder}"`.
- Tests with a fake in-memory `Drive`: R-7.11 sent → `_Sent` under the root; R-7.11 paid → `_Sent_PAID_…` under `2026/`, with the year folder created once; R-7.11 a second run is a no-op; R-7.11 the move happened but `folder_path` was not recorded (the fake has only the target) → records the target, no move; R-7.11 paid before the `_Sent` rename ran → one move straight to the final name; R-7.11 source and target both missing → permanent failure, no folder created; R-7.11 a transient error on `move` → the job raises and `folder_path` is unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_folder_status.py`.

## Step 15: Enqueue `folder_status` when a batch becomes `sent` `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/app.py`.

- In `_sent` (`actions.py:238`) and in `_automatic` (`deliver.py:417`), `enqueue(conn, invoice_id, FOLDER_STATUS_STEP)` in the same transaction as `set_batch_state(..., SENT)`. `_prepare` also stores `folder_path` (`f"{sink.root}/{folder}"`) next to `folder` and `seq`.
- `app.py` registers `FOLDER_STATUS_STEP` in the handler dict when the deliver handler exists, with the same `drive`.
- Tests in `tests/integration/test_pipeline_actions.py` and `test_pipeline_deliver.py` (the step's test files): R-7.11 [Ya lo envié] leaves one `folder_status` job; R-7.11 a `graph` send that Graph accepts leaves one; R-7.11 a failed send leaves none.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_pipeline_deliver.py -k "sent or folder"`.

## Step 16: The payment matcher (pure) `[worker]`

Files: `src/invoicebot/pipeline/payment_match.py` (new), `tests/unit/test_payment_match.py` (new).

- `@dataclass(frozen=True) class Candidate: invoice_id: int; created_at: datetime; claimed_cents: int`.
- `match_sets(candidates: Sequence[Candidate], target_cents: int, limit: int = 3) -> list[tuple[int, ...]]`: the invoice-id sets whose `claimed_cents` add up to `target_cents`, at most `limit`, ordered by their oldest invoice, then by size. The search sorts by `created_at`, prunes a branch once its sum passes the target, and stops at `limit` sets (D107).
- `claimed_cents(total: Decimal, cap: Decimal | None) -> int`: `min(total, cap)` in cents.
- Tests: R-7.12 one exact set; R-7.12 two sets, returned oldest first; R-7.12 five possible sets, only 3 returned; R-7.12 no set → `[]`; R-7.13 a total above the cap counts as the cap; R-7.12 20 candidates with no match finish in under 1 s (`time.perf_counter`); an empty candidate list → `[]`.

Test: `uv run pytest -q tests/unit/test_payment_match.py`.

## Step 17: Payment store functions `[worker]`

Files: `src/invoicebot/store/payments.py` (new), `tests/integration/test_store_payments.py` (new).

- `candidates(conn, profile_id, flow_id, cap) -> list[Candidate]`: the profile's flow B invoices in `DONE` with a `sent` batch, `payment_id IS NULL`, `created_at > now() - interval '180 days'`, oldest first, `LIMIT 20` (R-7.12). It uses the database clock. `Candidate` lives in `pipeline/payment_match.py`, which `store` must not import (`store-is-low`), so move the dataclass to `store/payments.py` and import it from there in `payment_match.py`.
- `add_proposal(conn, profile_id, flow_id, paid_on, amount, sets) -> int`, `get_proposal(conn, proposal_id) -> Proposal | None` (frozen dataclass), `close_proposal(conn, proposal_id, state)`.
- `record_payment(conn, flow_id, profile_id, paid_on, amount, invoice_ids) -> int`: inserts the payment and sets `payment_id` on those invoices only `WHERE payment_id IS NULL`. If fewer rows change than ids given, it raises `AlreadyPaid`. The caller owns the transaction.
- Tests: R-7.12 an invoice older than 180 days, already paid, not `DONE` or with an unsent batch is not a candidate; R-7.12 21 eligible invoices → 20, oldest first; R-7.12 `record_payment` on an already-paid invoice raises `AlreadyPaid` and leaves no payment row once the transaction rolls back; R-7.12 a proposal round-trips its sets.

Test: `uv run pytest -q tests/integration/test_store_payments.py`.

## Step 18: Parse `/pagado` (pure) `[worker:low]`

Files: `src/invoicebot/pipeline/payment_match.py`, `tests/unit/test_payment_match.py`.

- `parse_pagado(arg: str, today: date) -> PagadoAmount | PagadoIds | None`: `dd/mm monto` (amount with `.` or `,`, an optional `S/`) or `dd/mm #12 #14 …`. The year is `today`'s, or the year before when `dd/mm` would fall after `today`. An impossible date, a zero or negative amount, more than 20 ids, or anything else → `None`.
- Tests: R-7.12 `15/12 144.60`, `15/12 144,6` and `15/12 S/ 144.60` give the same amount; R-7.12 `15/12 #12 #14` gives ids `(12, 14)`; R-7.12 a `dd/mm` after today falls in last year; R-7.12 `31/02 10`, `15/12 0`, `15/12`, `hola` → `None`.

Test: `uv run pytest -q tests/unit/test_payment_match.py -k parse`.

## Step 19: The `track_payments` flag `[worker]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- `Reimbursement` gets `track_payments: bool = False` (spec 03 §flow B). No profile-to-flow mapping exists, so `/pagado` uses the first configured flow whose `reimbursement.track_payments` is true.
- Tests: R-7.12 a flow with `reimbursement: {cap: 50, track_payments: true}` loads; the default is false; an unknown key is rejected.

Test: `uv run pytest -q tests/unit/test_config_models.py -k track_payments`.

## Step 20: Commands see the payment flow `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/api/webhook.py` (and where its deps are built in `src/invoicebot/app.py`). A pure pass-through, one concern.

- `CommandContext` gets `payment_flow: PaymentFlow | None = None`, where `PaymentFlow` is a frozen dataclass `(flow_id: str, cap: Decimal | None)` in `commands.py`. `handle_inbound` and `_store_with_overrides` take `payment_flow: PaymentFlow | None = None` and pass it into the context. The webhook deps carry it, built once at startup from the first flow whose `reimbursement.track_payments` is true (None without one).
- Tests: R-7.12 the webhook deps for a config with a tracking flow carry its id and cap; without one, None. Existing callers keep working with the default.

Test: `uv run pytest -q tests/unit/test_app.py tests/integration/test_webhook.py -k payment_flow` (use the real file names).

## Step 21: The `/pagado` command `[worker]`

Files: `src/invoicebot/pipeline/payments.py` (new), `src/invoicebot/pipeline/commands.py` (registration only), `src/invoicebot/rules/messages_es.py` (`HELP_PAGADO`, the D107 set label), `tests/integration/test_payments.py` (new).

- `async def pagado(conn, msg, arg, ctx)`, registered as `"pagado"` in `COMMANDS` with `HELP_PAGADO`. `payments.py` must not import `commands.py` at module level beyond the context types (no cycle: `commands.py` imports `pagado`); it answers through `pipeline/outgoing.queue_message`. A bad argument (`parse_pagado` is None) or `ctx.payment_flow is None` answers `HELP_PAGADO`. Candidates are always the sender's own (`profile_id`).
- Amount form: candidates (step 17) → `match_sets`. One set → BR-P01 with [Sí] [No]. Several → BR-P02 with one button per set (D107 label from spec 06) plus [No]. None → BR-P03 with `{invoice_lines}` of the candidates (`#id serie-numero S/ claimed`, the format of `/pendientes`). The `#id` form carries no amount (R-7.12, step 18): when every id is a candidate, the amount is the sum of their claimed amounts → BR-P01; when any id is not a candidate → BR-P03 with that sum as `{monto}`. BR-P01 and BR-P02 store a proposal first, and their buttons carry `p<proposal_id>:pay:<index>` and `p<proposal_id>:pay_no`.
- Tests with the fake channel: R-7.12 BR-P01 for one set, with the buttons' ids; R-7.12 BR-P02 lists 2 sets, oldest first; R-7.12 BR-P03 lists the pending invoices; R-7.12 the `#id` form naming an id that is not a candidate → BR-P03; R-7.12 the `#id` form naming candidates → BR-P01 with their summed amount; R-7.12 an invoice from another profile is never a candidate; R-7.12 with no payment flow `/pagado` answers its help line.

Test: `uv run pytest -q tests/integration/test_payments.py -k command`.

## Step 22: Confirming a payment `[worker]`

Files: `src/invoicebot/pipeline/payments.py`, `src/invoicebot/pipeline/intake.py`, `tests/integration/test_payments.py`.

- `handle_payment_press(conn, msg) -> bool`: parses `p<id>:pay:<n>` or `p<id>:pay_no` (a small parser in `payments.py`, with the same character caps as `parse_button_id`). The proposal must be `pending`, belong to the sender and be under 24 h old (database clock); otherwise BR-S02. `pay_no` closes it as `rejected` and answers nothing more. `pay:<n>` takes set `n`, calls `record_payment`, enqueues `FOLDER_STATUS_STEP` for each invoice, closes the proposal as `confirmed` and answers BR-P04, all in the caller's transaction. `AlreadyPaid` → BR-S02 and no change.
- `intake.py`: a press whose `button_id` starts with `p` goes to `handle_payment_press`; every other press goes to `handle_press`, as today. That one prefix check is the only change in intake.
- Tests: R-7.12 [Sí] stores one payment, sets `payment_id` on its invoices, leaves one `folder_status` job each and sends BR-P04 with `{n}`; R-7.12 [No] stores nothing; R-7.12 a second press of the same button → BR-S02; R-7.12 a press from another profile → BR-S02; R-7.12 a proposal whose invoice was paid in between → BR-S02 and nothing stored.

Test: `uv run pytest -q tests/integration/test_payments.py -k press`.

## Step 23: Mail config for the new modes `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py` (use the real file name).

- `type MailMode = Literal["dry_run", "graph", "inbox_rule", "laptop_outlook"]`.
- `Mail` gets `owner_work_address: EmailStr | None = None` (or the project's existing email type), `confirm_timeout: Delay = "24h"` and `agent_stale_after: Delay = "4h"`, where `Delay` is the existing `Annotated` delay type.
- `MailerSink` gets `inbox_rule_account: Literal["bot"] = "bot"`. A validator on `Config`: a flow that lists `inbox_rule` needs `mail.owner_work_address`; a flow that lists `laptop_outlook` needs an `onedrive_folder` sink on `onedrive_personal`.
- `example.yaml`: the new keys with synthetic values (`owner_work_address: "owner@example.com"`), and `modes: [graph]` unchanged.
- Tests: R-7.7 `inbox_rule` without `owner_work_address` is refused; R-7.14 `laptop_outlook` without a personal OneDrive sink is refused; R-7.8 the defaults are 24 h and 4 h; `example.yaml` still loads.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 24: `MailMode` says how it completes `[worker]`

Files: `src/invoicebot/sinks/mailer.py`, `src/invoicebot/pipeline/deliver.py`, `tests/unit/test_sinks_mailer.py`.

- `MailMode` replaces `automatic: bool` with `completion: Literal["accepted", "receipt", "tap"]` and `done_rule: str` (D108). `DryRun` is `tap`/`BR-D08`, `GraphSend` is `accepted`/`BR-D10`.
- `deliver.py` picks `_automatic` for `accepted` and `_assisted` for `tap` from a dict keyed by `completion`; `_automatic` uses the mode's `done_rule` for the invoice's rule and reply. `receipt` gets its handler in step 28; until then it is not in `mail_modes`, so no mode returns it.
- Tests: the existing deliver and mailer tests pass unchanged; R-7.8 each built mode reports the `completion` and `done_rule` above.

Test: `uv run pytest -q tests/unit/test_sinks_mailer.py tests/integration/test_pipeline_deliver.py`.

## Step 25: The `inbox_rule` mode `[worker]`

Files: `src/invoicebot/sinks/mailer.py`, `src/invoicebot/app.py`, `tests/unit/test_sinks_mailer.py`.

- `InboxRule(mailer: Mailer, owner_address: str)`: `completion="accepted"`, `done_rule="BR-D13"`. `deliver` refuses (`ValueError`) a mail whose `to` and `cc` are not exactly `[owner_address]` and `[]`, then sends it through the `bot` account's `GraphMailer`. It sends with the mail's `to` replaced by `owner_address`; the sink's `to` stays HR's address for the other modes.
- `mail_modes` adds `"inbox_rule"` when `mail.owner_work_address` is set; with `send_enabled` false it maps to the dry run, like `graph`. `app.py` builds the `bot` account's mailer with the token source of account `bot` (`Mail.Send` added to that account's scopes in `auth/providers.py`, D108; that file is the step's one extra).
- Tests with `httpx.MockTransport`: R-7.7 the Graph request goes to the owner's work address only; R-7.7 a mail with any other recipient raises and sends nothing; R-7.7 `send_enabled: false` maps `inbox_rule` to the dry run.

Test: `uv run pytest -q tests/unit/test_sinks_mailer.py -k inbox_rule`.

## Step 26: BR-D13 with [No llegó] `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_deliver.py`.

- When the mode's `done_rule` is BR-D13, `_notify` adds the button `button_id(invoice_id, NOT_ARRIVED_ACTION)` with title `BUTTON_NOT_ARRIVED = "No llegó"`. `NOT_ARRIVED_ACTION = "not_arrived"` lives in `pipeline/actions.py` (step 30 handles it).
- Tests: R-7.8 an `inbox_rule` send Graph accepts makes the batch `sent`, the invoice `DONE` with BR-D13, leaves one `folder_status` job and offers [No llegó]; R-7.8 a `graph` send still answers BR-D10 with no button.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py -k inbox_rule`.

## Step 27: The laptop job file and its HMAC (pure) `[worker]`

Files: `src/invoicebot/sinks/laptop_job.py` (new), `tests/unit/test_sinks_laptop_job.py` (new), `tests/laptop/vectors.json` (new).

- `job_document(job_id, invoice_id, to, subject, body, attachments: list[str], key: bytes) -> bytes`: the JSON of spec 03 (`job_id`, `invoice_id`, `to`, `subject`, `body`, `attachments`, `hmac`). The HMAC is HMAC-SHA256, hex, over the canonical JSON of every other field (`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`). `attachments` are paths relative to the invoice folder (`"Formato de reembolsos.xlsx"`, no `/` or `..`). A path that is not a plain file name raises `ValueError`.
- `parse_receipt(data: bytes) -> Receipt`: `{"job_id", "sent_at"}` or `{"job_id", "error"}`, with the job id matching `^[0-9a-f]{32}$`, an error at most 500 characters, and the whole file under 4 KiB; anything else raises `ValueError`.
- `vectors.json`: two synthetic jobs with key `"test-key"` and their expected HMACs, so the Pester tests in step 34 check the same bytes.
- Tests: R-7.14 the HMAC of each vector; R-7.14 an attachment `../x.pdf` or `a/b.pdf` raises; R-7.14 a receipt with a bad job id, an oversized error or extra size raises.

Test: `uv run pytest -q tests/unit/test_sinks_laptop_job.py`.

## Step 28: The `laptop_outlook` mode hands the email to the laptop `[worker]`

Run as four sub-steps, each reviewed before the next (D111: the job names its folder).

- **28a** (`src/invoicebot/sinks/laptop_job.py`, `tests/unit/test_sinks_laptop_job.py`, `tests/laptop/vectors.json`): `LaptopJob` gets `folder: str` after `invoice_id`, signed with the rest. Each `/`-separated segment is a plain name (the attachment check), with no leading `/` and no empty segment; else `ValueError`. Regenerate the vectors. Tests: R-7.14 `../x`, `a//b`, `/a`, `C:x` as a folder raise; the vectors verify.
- **28b** (`src/invoicebot/store/mail_jobs.py` new, `tests/integration/test_store_mail_jobs.py` new): `add_mail_job(conn, job_id, invoice_id)` inserts the row and does not commit. Test: R-7.14 the row is stored with `created_at` from the database clock.
- **28c** (`src/invoicebot/sinks/mailer.py`, `src/invoicebot/pipeline/deliver.py` call sites only, `tests/unit/test_sinks_mailer.py`, the fake modes in `tests/integration/test_pipeline_deliver.py`): `MailMode.deliver(invoice_id, mail, folder)`; every mode takes `folder`, only the new one uses it. `LaptopOutlook(drive, root, key)`: `completion="receipt"`, `done_rule="BR-D10"`; `deliver` writes `{root}/_outbox/<job_id>.json` (job id `uuid4().hex`, `job_document` with the folder and the attachments' file names) with `drive.upload` and returns the job id. `mail_modes` takes a `laptop: LaptopOutlook | None` and adds `laptop_outlook` when it is given (the dry run when `send_enabled` is false). Tests with a fake `Drive`: R-7.14 one job file whose HMAC verifies with the test key and whose folder is the invoice's.
- **28d** (`src/invoicebot/pipeline/deliver.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_deliver.py`): `completion="receipt"` maps to `_handed_off`: the batch becomes `prepared`, `add_mail_job` stores the returned job id, the invoice is `BATCHED`, and nothing is sent to the sender. `app.py` builds `LaptopOutlook` when a flow lists `laptop_outlook`, with the personal OneDrive drive, that flow's `onedrive_folder` root and the age-store secret `laptop_agent_key` through a scoped `Secrets` view; a missing secret is a startup error. Tests: R-7.14 a `laptop_outlook` delivery writes one job, leaves the batch `prepared` and one `mail_jobs` row, and sends no message; R-7.14 a missing `laptop_agent_key` with `laptop_outlook` listed fails at startup.

Test: `uv run pytest -q tests/unit/test_sinks_laptop_job.py tests/unit/test_sinks_mailer.py tests/integration/test_store_mail_jobs.py tests/integration/test_pipeline_deliver.py -k "laptop or mail_job"`.

## Step 29: The receipt poller `[worker]`

Files: `src/invoicebot/pipeline/laptop.py` (new), `src/invoicebot/sinks/onedrive.py`, `tests/integration/test_pipeline_laptop.py` (new).

- `Drive.download(path, max_bytes) -> bytes` if the port lacks it (check `sinks/base.py`): `GraphDrive` reads `/content`, refuses a body over `max_bytes` while streaming, and maps 404 to `FolderMissing`. `sinks/base.py` is the step's one extra file.
- `async def poll_receipts(pool, drive, root)` lists `{root}/_outbox/receipts/`, downloads each receipt with a 4 KiB cap and parses it with `parse_receipt`. A known job with `sent_at` and a `prepared` batch → batch `sent`, invoice `DONE` with BR-D10, a `folder_status` job, `mail_jobs.receipt_at` set. A receipt with `error` → `receipt_error` set and BR-D11 (step 30 adds its buttons; here, the text only). An unknown or already-handled job id changes nothing. Each receipt is handled in its own transaction.
- Tests with a fake `Drive`: R-7.8 a receipt with `sent_at` completes the batch with BR-D10 and one `folder_status` job; R-7.8 the same receipt seen again changes nothing; R-7.8 an error receipt sends BR-D11 and leaves the batch `prepared`; R-7.14 a receipt over 4 KiB is skipped and logged; R-7.14 a receipt for an unknown job id changes nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_laptop.py`.

## Step 30: [No llegó], [Ya llegó] and the BR-D11 menu `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/laptop.py`, `src/invoicebot/store/mail_jobs.py`, `tests/integration/test_pipeline_actions.py`.

- `async def send_not_confirmed(conn, invoice_id)` in `actions.py` is the one BR-D11 sender (steps 29 and 33): BR-D11's text with the buttons of D109: `via_laptop` ([Por la laptop], only when the invoice has a `mail_jobs` row, i.e. it went through `laptop_outlook`: actions see no config, so this is the database's answer; add the lookup to `store/mail_jobs.py`), `self_send` ([Lo envío yo]), `arrived` ([Ya llegó]). Button titles go in `messages_es.py` next to `BUTTON_NOT_ARRIVED`. `pipeline/laptop.py`'s error-receipt path calls it in place of its own BR-D11 text.
- `NOT_ARRIVED_ACTION` (valid in `DONE` and `BATCHED`) answers with `send_not_confirmed`. `ARRIVED_ACTION` (valid in `DONE` and `BATCHED`): a `prepared` batch becomes `sent`, the invoice `DONE` with BR-D10 and a `folder_status` job, in the press's transaction; a `sent` batch only gets BR-D10's text. Both press actions touch only the database. `VIA_LAPTOP_ACTION` and `SELF_SEND_ACTION` constants are defined here; step 31 handles them.
- Tests: R-7.8 [No llegó] on BR-D13 → BR-D11 with three buttons when the invoice has a `mail_jobs` row, two when not; R-7.8 [Ya llegó] on a `prepared` laptop batch completes it with BR-D10 and one `folder_status` job; R-7.8 [Ya llegó] on a `sent` batch changes no state and sends BR-D10's text.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py -k "not_arrived or arrived" && uv run pytest -q tests/integration/test_pipeline_laptop.py`.

## Step 31: [Por la laptop] and [Lo envío yo] enqueue a resend job `[worker]`

Files: `src/invoicebot/pipeline/steps.py`, `src/invoicebot/pipeline/actions.py`, `tests/integration/test_pipeline_actions.py`.

- `steps.py`: `RESEND_LAPTOP_STEP = "resend_laptop"` and `RESEND_EML_STEP = "resend_eml"`, each with a one-line comment.
- `VIA_LAPTOP_ACTION` and `SELF_SEND_ACTION` (valid in `DONE` and `BATCHED`) only `enqueue` their step in the press's transaction: no file, drive or mail call happens inside a press (the press runs in intake's transaction, so I/O there could outlive a rolled-back commit). No state changes and no reply; the job replies.
- Tests: R-7.14 [Por la laptop] enqueues one `resend_laptop` job and changes no invoice or batch state; R-7.8 [Lo envío yo] enqueues one `resend_eml` job and changes no state.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py -k "via_laptop or self_send"`.

## Step 32: The resend handler `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_deliver.py`.

- `make_resend_handler(...)` in `deliver.py`, beside `make_deliver_handler` and taking the same dependencies, returns one handler per resend step. It loads the invoice like `_load` but accepts `DONE` and `BATCHED` (not `DELIVERING`), rebuilds the delivery with `_prepare` (which reuses the saved folder and sequence, D88), and leaves the batch state alone.
- `resend_laptop`: if the invoice's flow data already holds `resending`, a previous attempt died mid-write: drop the marker, send BR-D03 and stop (never a second job, as D89). Otherwise commit `resending` into the flow data, call the `laptop_outlook` mode, then in one transaction store the `mail_jobs` row and drop the marker. No message: the receipt answers.
- `resend_eml`: call the `dry_run` mode and answer BR-D07's text as `_assisted` builds it, with [Ya lo envié] only when the batch is `prepared`. Rewriting the `.eml` is harmless, so no marker.
- `app.py`: register both steps next to `DELIVER_STEP`, only when the deliver handlers exist.
- Tests: R-7.14 `resend_laptop` on a `DONE` invoice writes one job and one `mail_jobs` row, batch unchanged; R-7.14 `resend_laptop` with `resending` already set writes no job and sends BR-D03; R-7.8 `resend_eml` on a `sent` batch writes the `.eml` and sends BR-D07's text without [Ya lo envié]; R-7.8 `resend_eml` on a `prepared` batch sends it with [Ya lo envié].

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py -k resend`.

## Step 33: Overdue receipts and a stale laptop `[worker]`

Files: `src/invoicebot/pipeline/laptop.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_laptop.py`.

- `async def check_overdue(pool, drive, root, mail)`. A `mail_jobs` row with no receipt, whose job file is still in `_outbox/` and is older than `agent_stale_after`, with `stale_alerted_at` NULL → BR-D12 with `{hours}`, then sets `stale_alerted_at`. A row with no receipt older than `confirm_timeout`, with `overdue_alerted_at` NULL → `send_not_confirmed` (BR-D11), then sets `overdue_alerted_at`. Ages come from the database clock. Nothing is re-sent.
- `app.py`: a background task `"laptop"` that runs `poll_receipts` then `check_overdue` every 5 minutes, only when a flow lists `laptop_outlook`, supervised like `park`.
- Tests: R-7.14 a job 5 h old still in `_outbox/` → one BR-D12 with `5`, and none on the next run; R-7.8 a job 25 h old with no receipt → one BR-D11; R-7.8 a job with a receipt → neither; R-7.14 a job the agent picked up (file gone) but with no receipt yet → no BR-D12.

Test: `uv run pytest -q tests/integration/test_pipeline_laptop.py -k "stale or overdue"`.

## Step 34: Laptop agent: job checks `[worker]`

Files: `scripts/laptop_agent.ps1` (new), `tests/laptop/laptop_agent.Tests.ps1` (new).

- The script takes `-OneDriveRoot`, `-WorkRoot`, `-KeyFile`, `-AllowlistFile` and `-DryRun`. Its functions live in the script and are dot-sourceable for tests; the main block runs only when the script is invoked, not dot-sourced.
- `Test-JobFile $path $key $allowlist $folder` returns `$true` only when: the file is under 64 KiB and valid JSON; the HMAC over the canonical JSON (the same rule as step 27: sorted keys, no spaces, ASCII escapes) matches, compared in constant time; every `to` is on the allowlist; every attachment is a plain file name that exists inside the invoice's folder (`Resolve-Path` stays under the folder). It logs the reason for a `$false` by job id only.
- Tests (Pester): R-7.14 both `tests/laptop/vectors.json` jobs verify with `test-key`; R-7.14 one changed byte → refused; R-7.14 a recipient not on the allowlist → refused; R-7.14 an attachment `..\x.pdf` or one missing from the folder → refused; R-7.14 a 70 KiB file → refused.

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"`.

## Step 35: Laptop agent: send once and write the receipt `[worker]`

Files: `scripts/laptop_agent.ps1`, `tests/laptop/laptop_agent.Tests.ps1`.

- `Send-Job $job $folder $outlook $stateFile`: if the job id is in the local state file (`sent-jobs.txt` beside the script), it only rewrites the receipt. Otherwise it creates a mail item through the injected `$outlook` object (`CreateItem(0)`, `To`, `Subject`, `Body`, `Attachments.Add`, `Send()`), appends the job id to the state file _before_ `Send()`, and writes `_outbox/receipts/<job id>.json` with `sent_at` (UTC ISO 8601). Then it deletes the job file. Any exception writes a receipt with `error` (the exception type and message, cut to 500 characters) and keeps the job id out of the state file. With `-DryRun` it logs what it would send and touches nothing.
- The real Outlook object (`New-Object -ComObject Outlook.Application`) is created only in the main block.
- Tests with a fake Outlook PSCustomObject that records calls: R-7.14 a valid job is sent once with three attachments, a receipt with `sent_at` is written and the job file is deleted; R-7.14 running twice sends once; R-7.14 a `Send()` that throws writes an `error` receipt; R-7.14 `-DryRun` sends nothing and writes nothing; R-7.14 the log file never contains the body text.

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"`.

## Step 36: Laptop agent: mirror folders by manifest id `[worker]`

Files: `scripts/laptop_agent.ps1`, `tests/laptop/laptop_agent.Tests.ps1`.

- `Sync-Folders $personalRoot $workRoot`: index both trees by the `invoice_id` in each folder's `.invoicebot.json` (the root and its year folders). A folder missing on the work side is copied there. A folder whose name or parent differs is renamed or moved to match the personal side (R-7.3). Files are copied only if missing or with a different hash. It never deletes anything on the work side, and a folder without a manifest is left alone. A manifest whose `invoice_id` is not an integer is logged and skipped.
- Tests with two temp trees: R-7.3 a new personal folder is copied; R-7.11 a personal rename to `_Sent` renames the work copy; R-7.11 a move into `2026/` moves the work copy; R-7.3 a hand-made work folder without a manifest is untouched; a second run changes nothing.

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"`.

## Step 37: Pester in CI, plus the runbook section `[worker:low]`

Files: `.github/workflows/ci.yml`, `deploy/README.md`.

- CI: a job `laptop-agent` on `ubuntu-latest` that runs `pwsh -NoProfile -c "Install-Module Pester -MinimumVersion 5.5.0 -Force -Scope CurrentUser; Invoke-Pester -CI tests/laptop"`.
- `deploy/README.md`, a section "Laptop agent": copy `scripts/laptop_agent.ps1`, the key file (from `secrets_edit.py`'s `laptop_agent_key`) and the allowlist file (HR's address) to the laptop; a Windows scheduled task every 5 minutes with `-OneDriveRoot`/`-WorkRoot`; first run with `-DryRun`; Outlook classic is required (R-7.14); where the log and `sent-jobs.txt` live. A section "Outlook rule for `inbox_rule`": _from_ the bot mailbox, _subject starts with_ `Support release` → _forward to_ HR (spec 03); and that it is checked by S0.11 (1) and (2).

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"` and `uv run pytest -q tests/unit/test_rules_parity.py` (no behavior change; the workflow is checked when the PR runs).

## Step 38: Go-live import: read the folders (pure) `[worker]`

Files: `src/invoicebot/importer.py` (new), `tests/unit/test_importer.py` (new).

- `parse_claim_folder(name: str) -> ClaimFolder | None`: `{YYYY}-{M or MM}-Support_{seq}_{comercio}` with optional `_Sent` and `_PAID_{dd}_{mm}___{amount}`, giving `year, month, seq, comercio, sent, paid`. `None` for any other name.
- `read_claim(folder: Path) -> Claim | None`: the folder's single `.xml`, parsed with the hardened `parse_invoice_xml` and checked under the existing size cap, giving `ruc, tipo, serie, numero, fecha, total, razon_social`. Only folders that are sent and unpaid count (D110). A folder with no XML, several XMLs, or an unreadable one → `None`, plus a reason the script prints.
- Tests with synthetic folders and XML fixtures from the existing test generator: R-7.16 `2026-7-Support_3_LaLena_Sent` and `2026-07-Support_3_LaLena_Sent` parse the same; R-7.16 a `_PAID_` folder and a folder without `_Sent` are skipped; R-7.16 a folder with two XMLs → `None` with a reason; R-7.16 `Fotos` → `None`.

Test: `uv run pytest -q tests/unit/test_importer.py`.

## Step 39: Go-live import: register the claims `[worker]`

Files: `scripts/import_claims.py` (new), `src/invoicebot/importer.py`, `tests/integration/test_importer.py` (new).

- `async def register_claims(conn, profile_id, flow_id, claims) -> ImportReport`: for each claim, skip it if its natural key (`<profile_id>|ruc|tipo|serie|numero`, the existing helper) exists. Otherwise insert a `DONE` invoice with its fields and `flow_data_json` of D110, plus a `sent` batch, in one transaction per claim.
- `scripts/import_claims.py --profile <id> [--dry-run] <folder>…`: loads the config and settings like `check_config.py`, walks each folder's subfolders (and their year folders), prints one line per folder (registered, skipped and why, or would register), and opens no Graph connection. It writes nothing under the folders it reads.
- Tests: R-7.16 two sent claims are registered as `DONE` with their `seq`; R-7.16 a second run registers none; R-7.16 `--dry-run` writes no row; R-7.1 `claim_seq` for that month returns the next number after an imported `seq` 3; R-7.12 an imported invoice is a `/pagado` candidate; R-7.16 the folder tree's files and mtimes are unchanged after a run.

Test: `uv run pytest -q tests/integration/test_importer.py`.

## Step 40: Spec, example config and the live check `[inline: spec text and the owner's live-check list]`

Files: `specs/02-requirements.md`, `specs/03-design.md`, `config/example.yaml`, `docs/plan-mvp-widen-b-deliver.md`.

- Spec 03 §6: the mail-mode table with each mode's completion (D108) and the job and receipt JSON as built.
- `example.yaml`: comments on `modes` naming the four modes and their completions.
- This plan, a section "Deferred: live check", run after M3 and S0.11: `inbox_rule` through the bot mailbox to the owner's work address, and the rule forwarding to the test inbox; the laptop agent with `-DryRun`, then one real job; `/pagado` on two real sent claims, with the folders renamed and moved in OneDrive and the laptop's work copy following; `import_claims.py --dry-run` on the owner's current folders.

Test: none (text only); `uv run pytest -q tests/unit/test_rules_parity.py` and `uv run python scripts/check_config.py config/example.yaml` (use the script's real arguments).
