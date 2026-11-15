# Plan: MVP phase 6c · widen-b-fetch review fixes

Roadmap: `docs/plan-mvp.md` row 6c. The finish review of `docs/plan-mvp-widen-b-fetch.md` (py-code-reviewer, py-security-auditor and the five-axis review, 2026-11-14) found four major and several minor issues. This plan is that review's one round of fixes, on the same branch (`docs/plan-mvp-widen-b-fetch`). Its finish also closes `docs/plan-mvp-widen-b-fetch.md`: the finish commit carries a `Plan-Done:` line for both plans.

**Goal:** an upload can never resume delivery without storing a file, a rejected upload is answered once, a PDF upload is a PDF, an ambiguous upload asks instead of guessing, one bad webhook message no longer drops the rest of its batch, and the guarded HTTP client checks every host's certificate.

## Design choices

The owner chose these on 2026-11-14. Step 1 writes them into the specs.

- **D102 · An upload that cannot be matched asks.** A pdf/xml upload whose quote does not resolve, from a profile with more than one `MANUAL_REQUIRED` invoice, is not linked. BR-D14 lists those invoices and asks the sender to send the file again as a reply to the invoice's photo, which the quote lookup already matches. With exactly one open invoice the upload links to it, as before. Quotes of the bot's own messages need the sent message id on outbox rows; that is carried to a later phase.
- **D103 · A rejected upload is recorded.** A new table `upload_rejections (invoice_id, media_ref, rule_id, created_at)`, expand only, keyed on `(invoice_id, media_ref)`. A rejected XML (BR-F06, BR-F09) or a non-PDF (BR-D15) gets a row in the same transaction as its reply, and `list_unattached_uploads` skips refs with a row. A redelivered job or a later upload therefore neither downloads it again nor repeats the reply.
- **D104 · The inbox trusts its mail (accepted risk).** `email_inbox` accepts any mail whose XML matches the invoice (R-5.6). It does not check the sender or the UBL signature. The mailbox is the flow's own dedicated account (D98), and an attacker would need the serie, numero, RUC and amounts. Sender allowlists or signature checks wait until a real abuse case or an employer requirement appears.
- **Carried to the next phase:** per-fetcher retry schedules built in `app.py` instead of `if fetcher == …` in `pipeline/fetch.py` (open/closed). A channel-keyed media download port instead of `WhatsAppClient` in `pipeline/uploads.py`, plus `ButtonPress` and `MENU_MAX` moved to `channels/base.py` and an import-linter contract `pipeline` → `channels.whatsapp` (DIP). The outbox stores the channel message id so a quote of BR-F07 matches (D102).

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres. Every new user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1. Synthetic values only.

## Step 1: Record D102–D104, BR-D14, BR-D15 and the carried items `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `docs/plan-mvp.md`.

- Spec 05: rows D102 to D104 with the text from "Design choices".
- Spec 06: BR-D14, trigger "a pdf/xml upload whose quote does not resolve while the profile has more than one stuck invoice (D102)", outcome "not linked", text `Tienes varios comprobantes esperando archivos: {invoices}. Vuelve a enviar el archivo respondiendo a la foto del comprobante.` (`{invoices}` is `#{id} {serie}-{numero}` items joined by `, `). BR-D15, trigger "an uploaded pdf does not start with `%PDF-` (D103)", outcome "not stored; recorded as rejected", text `El archivo que enviaste no es un PDF válido; no lo guardé.`
- Spec 02: R-1.5 cites D102 (an ambiguous upload asks) and D103 (a rejected upload is answered once).
- Roadmap: under "Carried over" for the next phase, the three items from "Design choices".

Also `rules/messages_es.py` and `rules/catalog.py`: BR-D14 and BR-D15 entries, since `test_rules_parity` keeps them in step with spec 06.

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 2: One refused webhook message does not drop its batch `[worker]`

Files: `src/invoicebot/api/webhook.py`, `tests/integration/test_webhook.py`.

- Move the `psycopg.errors.DataError` handling from `_intake` into `_persist`, around each message's `handle_inbound`. A refused message is logged and the loop goes on to the next one. `StateMoved` and the other database errors still end the request as they do now (503).
- The log line names only `type(exc).__name__` and `exc.sqlstate` in `extra=`. No message text and no traceback, because psycopg's message can echo the refused value.
- Test (R-1.8): a three-message body whose middle message makes Postgres raise `DataError` (the existing single-message test shows how, e.g. a NUL byte). The answer is 200, and the first and third messages are stored.

Test: `uv run pytest -q tests/integration/test_webhook.py`.

## Step 3: The `upload_rejections` table and its store functions `[worker]`

Files: `migrations/versions/0007_upload_rejections.py`, `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/uploads.py` (the one call site only), `tests/integration/test_invoices_store.py`.

- Migration 0007 (down revision 0006, plain SQL through `op.execute`): `CREATE TABLE upload_rejections (invoice_id bigint NOT NULL REFERENCES invoices(id), media_ref text NOT NULL, rule_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (invoice_id, media_ref))`. Use the same id type as `invoices.id` in 0001. Downgrade drops the table.
- `store/invoices.py`: `record_upload_rejection(conn, invoice_id, media_ref, rule_id)` does `INSERT … ON CONFLICT DO NOTHING` and does not commit. `list_unattached_uploads` also skips a media ref with an `upload_rejections` row for the invoice. It takes the upload kinds as a parameter (`kinds: Sequence[str]`, passed as `= ANY(%s)`) instead of the `IN ('pdf', 'xml')` literal. Its one caller in `pipeline/uploads.py` passes `UPLOAD_KINDS` imported from `pipeline/intake.py`.
- Tests (D103):
  - A recorded rejection hides that upload from `list_unattached_uploads`, and the invoice's other uploads are still listed.
  - Recording the same rejection twice raises nothing and leaves one row.

Test: `uv run pytest -q tests/integration/test_invoices_store.py -k "upload"`.

## Step 4: An attach run that stores nothing changes nothing; failures are per upload `[worker]`

Files: `src/invoicebot/pipeline/uploads.py`, `tests/integration/test_pipeline_uploads.py`.

- In `_commit`, when `outcome.kept` is empty, queue the rejection replies and return. The missing-docs check and the move to `DELIVERING` run only when this run stored a file. This matches the module docstring.
- Each rejection reply is queued together with `record_upload_rejection(conn, invoice_id, upload.media_ref, rule_id)` in the same transaction. `_Rejection` carries the media ref. Remove the `shortcut:` comment.
- `_download` also catches `WhatsAppError` (`channels/whatsapp/client.py`) and `MediaChecksumMismatch` (`channels/whatsapp/media.py`). It logs them like the other skips (`extra=` with invoice id and the exception type name), so one broken media ref no longer fails the whole job.
- `MANUAL_STATE` and `DELIVERING_STATE` are replaced by `MANUAL_REQUIRED` and `DELIVERING` imported from `invoicebot/states.py`.
- Tests (R-1.5, D101, D103):
  - An invoice in `MANUAL_REQUIRED` that already has every required doc stays `MANUAL_REQUIRED` after a rejected XML upload: one BR-F09 reply, no deliver job.
  - The same holds when the upload's kind is not in the flow's `manual_uploads`: no reply, no deliver job.
  - A rejected XML followed by a second attach job for the same invoice sends BR-F09 once and downloads the XML once (count the mock transport's requests).
  - An upload whose media info call answers 404 is skipped, and a valid PDF in the same run is still stored.

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py`.

## Step 5: One PDF header check for the QR link and the inbox `[worker:low]`

Files: `src/invoicebot/fetchers/base.py`, `src/invoicebot/fetchers/qr_url.py`, `src/invoicebot/fetchers/email_inbox.py`, `tests/unit/test_fetchers_email_inbox.py`.

- `fetchers/base.py`: `is_pdf(data: bytes) -> bool`. It is true when the body, after an optional UTF-8 BOM and leading ASCII whitespace, starts with `%PDF-`. Move the BOM and whitespace constants from `qr_url.py`. `qr_url._classify` uses it for its pdf branch.
- `email_inbox.py`: a PDF whose bytes fail `is_pdf` is dropped. The XML is still returned, so the result is the same as a mail with no PDF.
- Test (R-5.6): a mail with a matching XML and an HTML file named `<serie>-<numero>.pdf` returns only the XML.

Test: `uv run pytest -q tests/unit/test_fetchers_email_inbox.py tests/unit/test_fetchers_qr_url.py`.

## Step 6: A PDF upload must be a PDF (BR-D15) `[worker]`

Files: `src/invoicebot/pipeline/uploads.py`, `tests/integration/test_pipeline_uploads.py`.

- `MESSAGES["BR-D15"]` and its `rules/catalog.py` entry already exist (step 1, kept in parity with spec 06); use them, don't add them again.
- `uploads.py`: a `pdf` upload whose bytes fail `fetchers.base.is_pdf` is discarded like a rejected XML. Its rejection is `BR-D15`, recorded (step 4) and answered once.
- Test (D103): an HTML body uploaded as a pdf leaves no files row, sends BR-D15 once, and the invoice stays `MANUAL_REQUIRED`.

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py tests/unit/test_rules_parity.py`.

## Step 7: An ambiguous upload asks which invoice (BR-D14) `[worker]`

Files: `src/invoicebot/store/messages.py`, `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`.

- `store/messages.py`: `find_open_invoice_for_attachment` keeps its quote branch. Without a resolved quote it returns the profile's open invoice only when there is exactly one. Add `list_open_invoices(conn, profile_id) -> list[tuple[int, str | None, str | None]]`: id, serie and numero of the profile's open invoices, newest first. The serie and numero come from the stored reading, as `load_reading` does. Update the docstring.
- `intake._link_upload`: when nothing matched and `list_open_invoices` returns more than one invoice, queue BR-D14 to the sender in the caller's transaction (`queue_message`, as other intake replies do). The `{invoices}` items are `#{id} {serie}-{numero}`, with `#{id}` alone when the reading lacks a field. Nothing is linked.
- `MESSAGES["BR-D14"]` and its `rules/catalog.py` entry already exist (step 1, kept in parity with spec 06); use them, don't add them again.
- Tests (R-1.5, D102):
  - Two stuck invoices and a PDF with no quote: BR-D14 names both, and no `attach_upload` job is queued.
  - The same PDF quoting the older invoice's photo links to the older invoice.
  - One stuck invoice and no quote: it links, as before.

Test: `uv run pytest -q tests/integration/test_intake.py tests/integration/test_messages_store.py tests/unit/test_rules_parity.py`.

## Step 8: Every host's certificate is checked; DNS has a deadline `[worker:low]`

Files: `src/invoicebot/app.py`, `src/invoicebot/net/guard.py`, `tests/unit/test_net_guard.py`.

- `app.py`: the guarded client's inner transport is `httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))`. A pinned request reaches httpcore by IP, so a pooled connection would carry an earlier host's TLS check to another host on the same IP (R-5.10). A comment on that line says so.
- `net/guard.py`: `SystemResolver.__init__(self, timeout_s: float = DNS_TIMEOUT_S)` with `DNS_TIMEOUT_S = 5.0`. `resolve` wraps `getaddrinfo` in `asyncio.timeout(self._timeout_s)` and returns `[]` on `TimeoutError`, as it does on `gaierror`.
- Test (R-5.10): a `SystemResolver` with a tiny timeout, whose loop's `getaddrinfo` is patched with `monkeypatch` to sleep longer, returns `[]`.

Test: `uv run pytest -q tests/unit/test_net_guard.py tests/unit/test_health.py`.

## Step 9: A QR fetch has an overall deadline `[worker:low]`

Files: `src/invoicebot/fetchers/qr_url.py`, `tests/unit/test_fetchers_qr_url.py`.

- The fetcher already receives the retrieval timeout or can be given it at construction in `app.py`; if a constructor argument is needed, `app.py` is allowed too and passes `cfg.retrieval.portal_timeout_s`. Wrap the redirect-following download in `asyncio.timeout(timeout_s)`. On expiry, return the same reason a read timeout returns now.
- Test (R-5.1): a `MockTransport` handler that sleeps past a tiny deadline yields that reason.

Test: `uv run pytest -q tests/unit/test_fetchers_qr_url.py`.

## Step 10: A failed fetch commit leaves no files `[worker:low]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- Around the store transaction after a fetch (the `async with inputs.pool.connection() as conn, conn.transaction():` block), add `except BaseException: shutil.rmtree(out_dir, ignore_errors=True); raise`, as `pipeline/uploads.py` does (R-9.2).
- Test (R-9.2): a fetcher that returns `ok` with files, and an invoice moved out of `FETCHING_DOCS` between fetch and commit so that `set_state` raises `StateMoved` (or the nearest seam the existing tests use). After the handler raises, `out_dir` is gone. If that seam does not exist without changing src beyond this step, stop and report.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`.

## Step 11: The mailbox search names the invoice `[worker:low]`

Files: `src/invoicebot/fetchers/email_inbox.py`, `tests/unit/test_fetchers_email_inbox.py`.

- The Graph `$search` term becomes `"{serie}-{numero}"` instead of the serie alone, so a store's other mail with the same serie no longer pushes the invoice out of the top 25.
- Test (R-5.5): the fake mailbox records the term it was asked for, and it is `F001-123` for that invoice.

Test: `uv run pytest -q tests/unit/test_fetchers_email_inbox.py`.

## Step 12: One home for the inbox name and the store wording `[worker:low]`

Files: `src/invoicebot/fetchers/email_inbox.py`, `src/invoicebot/pipeline/manual_message.py`, `src/invoicebot/rules/messages_es.py`.

- `fetchers/email_inbox.py` imports `EMAIL_INBOX_FETCHER` from `config/models.py` instead of defining it.
- `manual_message.py`'s `"el comercio"` and `"del comercio"` become constants in `messages_es.py` (`STORE_FALLBACK`, `STORE_FALLBACK_OF`), imported here.
- `SOL_ADAPTER` and `SUNAT_FETCHER` stay separate: a secrets adapter and a fetcher name change for different reasons.

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/unit/test_fetchers_email_inbox.py`.

## Step 13: Python 3.12.4 at least `[worker:low]`

Files: `pyproject.toml`, `uv.lock`, `deploy/Dockerfile`.

- `requires-python = ">=3.12.4"`. The address guard relies on the `ipaddress.is_private` fixes in 3.12.4 (CVE-2024-4032). Run `uv lock` so the lock file agrees.
- Both `FROM` lines pin `python:3.12.8-slim` (or the newest 3.12 patch tag the registry has, at least 3.12.4).

Test: `uv lock --check && uv run pytest -q tests/unit/test_net_guard.py`.
