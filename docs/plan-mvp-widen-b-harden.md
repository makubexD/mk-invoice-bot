# Plan: MVP phase 6a · widen-b-harden

Roadmap: `docs/plan-mvp.md` row 6, split here into four sub-phases (step 1 records the split). This first one is the code that needs nothing from the owner: the leftovers carried over from the `whatsapp`, `fetch` and `file-b` reviews, plus the owner's answer on the work account. Rules: BR-D02, BR-D03. Requirements: R-1.10, R-5.1, R-7.2, R-7.9, R-9.16.

**Goal:** a failed or crashed send alerts every admin with BR-D03's admin text. Graph upload bytes go only to Microsoft's upload hosts. A folder listing stops after a fixed number of pages. A control character in a reading cannot break the mail, the Excel or the drive path. An outbox row that can never be sent is marked dead at once. The template language is config. A portal fetch cannot outlive its job lease. The email inbox's retry schedule is typed. No owner item is needed.

**Needs from the owner first:** nothing. The M3 live run stays deferred and listed in the roadmap.

## Design choices

Step 1 writes these into the specs.

- **D91, flow B runs on the personal Microsoft account only** (owner, 2026-11-14). The work tenant's IT gives no consent to a third-party app, so S0.8 is answered: no work-account login. `onedrive_work` and `graph_work` stay unbuilt; delivery is the personal OneDrive plus the D49 and D61 fallbacks (laptop mirror, `inbox_rule`, `laptop_outlook`). The PKCE loopback login and the `auth-microsoft --account` choice of accounts are dropped from `widen-b`, because they existed only for the work account. The Entra app is registered by the owner with the personal account (supported account type "Personal Microsoft accounts only", public client flows allowed).
- **BR-D03 admin text**, in the style of BR-D01's: `📮 No se pudo enviar el correo del comprobante #{id} ({serie}-{numero}). Revisa la cuenta y ejecuta /reprocesar {id}.`
- **A permanent send failure** is a `PermanentSendError` raised by a transport. The outbox marks such a row `dead` on the first failure instead of retrying it 8 times.
- **The job lease is one constant**, `JOB_LEASE_S = 300` in `config/models.py`. `config` is a leaf and cannot import `queue`, so the worker imports the constant from there.
- **Control characters** are replaced by a space when a `Field` is built, so every reader, a `campo=valor` override and a reading loaded from `fields_json` all get the same clean value.

## Sub-phases of `widen-b` (step 1 writes them into the roadmap)

| #   | Sub-phase (plan file) | Backlog                                                                                                                 | Needs from the owner first                          |
| --- | --------------------- | ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 6a  | `widen-b-harden`      | review leftovers (`whatsapp`, `fetch`, `file-b`); D91                                                                   | Nothing                                             |
| 6b  | `widen-b-talk`        | rest of E4 (S4.3–S4.10, commands incl. `/pendientes`), E5 (S5.2, S5.3), BR-R09 brand question                           | Nothing                                             |
| 6c  | `widen-b-fetch`       | S7.2 (+ `document_url` allowlist, `printed_urls`), S7.4, S7.5, S10.3, S10.4, S10.7, S10.10, S11.9; portal config checks | S0.6 (remaining store pages); Q5                    |
| 6d  | `widen-b-deliver`     | S11.11, S11.12, S11.14, S11.15, S11.17; E8 (MVP part: S8.1–S8.3); E15 (S12.3, S15.1, S15.2)                             | S0.10, S0.11; the bot's Outlook.com mailbox; M3 run |

Conventions: everything in `docs/plan-mvp-read.md` "Conventions" and `docs/plan-mvp-file-b.md` "Conventions" still applies. Lint for every step: `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.

Out of scope here: everything in sub-phases 6b to 6d, the M3 live run.

## Step 1: Record D91, the BR-D03 admin text and the sub-phases `[inline: spec and roadmap text the owner approved]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `docs/plan-mvp.md`.

- Spec 05: add row D91 with the text from "Design choices" above; mark S0.8's question answered by D91 where Q2 is listed.
- Spec 06: in BR-D03's row, add the admin text after the user text, in the same `User: … Admin: …` form as BR-D01.
- Spec 04: S0.8 and S11.1 note "answered by D91: personal account only; no work-account login, no PKCE fallback".
- Roadmap: replace row 6 with the four sub-phase rows above (6a to 6d) and their owner needs. In "Moved by the `file-b` plan", strike the PKCE / work-account item with "dropped by D91". In "Carried over from the `file-b` review", strike the `--account` item ("dropped by D91") and mark the items this plan covers with `(widen-b-harden)`. Move the two `fetch` review items that wait for the next portal and the `printed_urls` item under `widen-b-fetch`.

Test: by reading the four files; `uv run pytest -q tests/unit/test_rules_parity.py` may fail on the missing `BR-D03_admin` text until step 2, which runs it, so commit steps 1 and 2 back to back.

## Step 2: BR-D03 alerts every admin `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `messages_es.py`: add `"BR-D03_admin"` with the exact text from step 1, next to `"BR-D03"`.
- `deliver.py`: add `RULE_SEND_FAILED_ADMIN = "BR-D03_admin"`. Give `_notify_admin` a `rule_id` parameter (the BR-D01 caller passes `RULE_AUTH_ADMIN`) and use it in the message and the warning's `extra`; its docstring stops naming BR-D01. Add `_notify_admins(conn, profiles, rule_id, text)` that loops over the admin profiles, and use it in `_pending_auth`, `_send_failed` and `_crashed_send`. Both send-failure paths format `BR-D03_admin` with `id`, `serie` and `numero` and queue it in the same transaction as the batch failure.

Test cases (integration, modelled on `test_auth_required_parks_the_invoice_and_alerts_every_admin`):

- `test_send_failure_alerts_every_admin`: a final Graph error on send → the batch is `failed`, the invoice `BATCHED`, the sender's outbox row has BR-D03, and each admin with a `preferred_channel` has one outbox row with rule `BR-D03_admin` whose text contains `#<id>` and `<serie>-<numero>`.
- `test_crashed_send_alerts_every_admin`: a batch left `sending` → the same rows as above.
- `test_admin_without_channel_is_skipped_on_send_failure`: an admin profile without `preferred_channel` gets no row, and the other admins and the sender still get theirs.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py tests/unit/test_rules_parity.py`

## Step 3: Upload sessions only go to Microsoft's upload hosts `[worker]`

Files: `src/invoicebot/sinks/onedrive.py`, `tests/unit/test_sinks_onedrive.py`, `tests/unit/test_sinks_mailer_graph.py`.

- `onedrive.py`: add `UPLOAD_HOSTS = ("graph.microsoft.com",)` and `UPLOAD_HOST_SUFFIXES = (".sharepoint.com", ".files.1drv.com", ".office.com")`. In `upload_chunks`, replace the `startswith("https://")` check with `_check_upload_url(url)`: parse with `urllib.parse.urlsplit`, require scheme `https`, a lower-cased hostname equal to one of `UPLOAD_HOSTS` or ending with one of `UPLOAD_HOST_SUFFIXES`, and no userinfo; otherwise raise `GraphError(NOT_SENT_STATUS)` before any request. `mailer_graph.py` already goes through `upload_chunks`, so it needs no change.

Test cases (each through `httpx.MockTransport`; "no request" means the transport saw no PUT):

- `https://tenant-my.sharepoint.com/upload?x=1` → uploaded.
- `https://api.files.1drv.com/up/abc` → uploaded.
- `https://graph.microsoft.com/v1.0/upload/abc` → uploaded.
- `https://evil.example/upload` → `GraphError`, no request.
- `https://sharepoint.com.evil.example/u` → `GraphError`, no request.
- `http://tenant.sharepoint.com/u` → `GraphError`, no request.
- `https://user@tenant.sharepoint.com/u` → `GraphError`, no request.
- mailer: an attachment upload session answering `https://evil.example/u` → `GraphError`, no PUT, no send.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive.py tests/unit/test_sinks_mailer_graph.py`

## Step 4: Cap the pages a folder listing follows `[worker]`

Files: `src/invoicebot/sinks/onedrive.py`, `tests/unit/test_sinks_onedrive.py`.

- Add `MAX_LIST_PAGES = 50`. `Drive.list_names` counts the pages it fetched; when a page past the 50th would be requested (a 50th page still carries `@odata.nextLink`), raise `GraphError(NOT_SENT_STATUS)` without requesting it. Note the cap in the docstring.

Test cases:

- 3 pages, the last without a next link → all names in order, 3 requests.
- exactly 50 pages, the 50th without a next link → all names, 50 requests.
- 50 pages, the 50th with a next link → `GraphError`, exactly 50 requests.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive.py`

## Step 5: A field value never carries a control character `[worker]`

Files: `src/invoicebot/extract/fields.py`, `tests/unit/test_extract_fields.py`.

- `Field.__post_init__`: replace each character whose `unicodedata.category` is `Cc` with a space, then strip leading and trailing whitespace; store the result with `object.__setattr__` (the dataclass is frozen). Add a module constant `_CONTROL_CATEGORY = "Cc"`. The docstring of `Field` says why (BR-D02: lxml, the `.eml` builder and the drive path reject control characters).

Test cases:

- `"ACME\x00 SAC"` → `"ACME  SAC"`.
- `"F001\n"` → `"F001"`.
- `"Razón\tSocial"` → `"Razón Social"`.
- `"\x1b[31mX"` → `"[31mX"`.
- `"Pollería Ñandú"` (no control character) → unchanged.
- `reading_from_json` with a `razon_social` value holding `\x07` → the loaded field has a space there.

Test: `uv run pytest -q tests/unit/test_extract_fields.py`

## Step 6: The outbox marks a permanent failure dead at once `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `tests/integration/test_outbox.py`.

- `outbox.py`: add `class PermanentSendError(Exception)` ("the row can never be sent; retrying is pointless"). `_deliver` returns the error text and whether the exception was a `PermanentSendError`. `_record_failure` takes that flag; when it is set, the row goes `dead` with `attempts + 1` and the dead-row log line, whatever the attempt count. The module docstring names the new case.

Test cases:

- a transport raising `PermanentSendError("no number")` on the first attempt → the row is `dead`, `attempts` is 1, `last_error` holds the text.
- a transport raising `RuntimeError` → the row stays pending, `attempts` 1, rescheduled (unchanged behavior).
- a transport that raises `RuntimeError` once, then `PermanentSendError` → `dead` with `attempts` 2.

Test: `uv run pytest -q tests/integration/test_outbox.py`

## Step 7: WhatsApp raises permanent failures; the template language is config `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `src/invoicebot/config/models.py`, `src/invoicebot/app.py`.

- `transport.py`: add `PERMANENT_META_CODES = frozenset({100, 131009, 131026, 131051})` (invalid parameter, parameter value invalid, message undeliverable, unsupported message type). In the transport, raise `PermanentSendError` (with `from`) when `row.to` has no number in `numbers`, when `_template_name` raises `TemplateMissing`, and when the client raises a `WhatsAppError` whose `code` is in that set. `language` becomes a required keyword argument (no default).
- `models.py`: `WhatsAppChannel.template_language: str = Field(default="es_PE", pattern=r"^[a-z]{2}(_[A-Z]{2})?$")`.
- `app.py`: pass `language=channel.template_language` to `make_transport`.

Test cases (in `tests/integration/test_whatsapp_transport.py`, with its existing fakes; config cases in `tests/unit/test_config_models.py`):

- recipient missing from `numbers` → `PermanentSendError`, no request.
- window closed and no template for the rule → `PermanentSendError`, no request.
- Meta answers 400 with code 131026 → `PermanentSendError`.
- Meta answers 500 with code 1 → `WhatsAppError`, not permanent.
- a template send with `template_language: en_US` → the payload's `language.code` is `en_US`.
- config: `template_language: "es-PE"` → validation error; absent → `es_PE`.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py tests/unit/test_config_models.py -k "template_language or transport"`

## Step 8: A portal fetch cannot outlive its job lease `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/queue/worker.py`, `tests/unit/test_config_models.py`.

- `models.py`: add `JOB_LEASE_S = 300` with a comment ("the worker's job lease; a portal fetch must end before it, R-5.1"). `Retrieval` gets a `model_validator(mode="after")` that rejects `portal_timeout_s >= JOB_LEASE_S` with a message naming both values.
- `worker.py`: `lease_s: int = JOB_LEASE_S`, imported from `invoicebot.config.models`.

Test cases:

- `portal_timeout_s: 120` → valid.
- `portal_timeout_s: 299.5` → valid.
- `portal_timeout_s: 300` → validation error mentioning 300.
- `portal_timeout_s: 900` → validation error.

Test: `uv run pytest -q tests/unit/test_config_models.py -k portal_timeout`

## Step 9: The email inbox's retry schedule is typed `[worker]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- `EmailInbox.retry_schedule` becomes `tuple[timedelta, ...]` with a `field_validator` (mode `before`) and a `field_serializer` like `Portal.retry_schedule`'s: one-liners over the existing `parse_delay` and `format_delay`, which stay the one home of the D65 delay format. `config/example.yaml` must still load unchanged.

Test cases:

- `["5m", "1h", "6h", "24h"]` → four `timedelta`s, and the dump gives the same four strings back.
- `["5 minutes"]` → validation error.
- `[]` → an empty tuple (as for a portal).
- `config/example.yaml` loads.

Test: `uv run pytest -q tests/unit/test_config_models.py -k retry_schedule`
