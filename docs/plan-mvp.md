# Roadmap: mk-invoice-bot MVP (v1 = M1–M7)

Source of truth: `specs/` (constitution, requirements, design, backlog in `specs/04-tasks.md`). Each phase becomes its own step plan, `docs/plan-mvp-<phase>.md`, written when the previous phase finishes. A phase is done when its milestone demo in spec 04 passes.

| #   | Phase (plan file)                                                                  | Backlog                                                                                                                                                                      | Milestone  | Needs from the owner first                                                       |
| --- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | -------------------------------------------------------------------------------- |
| 1   | `foundation` (done 2026-11-11)                                                     | E1 (S1.1–S1.10, S5.1, S12.2)                                                                                                                                                 | part of M1 | Nothing                                                                          |
| 2   | `whatsapp` (done 2026-11-11)                                                       | E2 (incl. S2.7–S2.9 window and templates)                                                                                                                                    | part of M1 | S0.7: Meta app, dedicated number, utility templates submitted                    |
| 3   | `read` (done 2026-11-13)                                                           | S3.1, S3.2, S3.3, S3.11, S3.12; S7.3, S7.7; S4.1, S4.2; spike S0.2                                                                                                           | M1         | ≥ 10 receipt photos sent through WhatsApp (any receipt with a QR)                |
| 3b  | `ocr` (done 2026-11-13)                                                            | spike S0.3; S3.5, S3.6, S3.7 (D81)                                                                                                                                           | M1b        | Tesseract with Spanish on the host; the private samples folder                   |
| 4   | `fetch` (done 2026-11-13)                                                          | S7.1, S7.6, S10.1, S10.2, S10.5, S10.8, S10.9, S0.9                                                                                                                          | M2         | La Leña's portal file completed (`config/portals/facturactiva-la-lena.yaml`)     |
| 5   | `file-b` (done 2026-11-14)                                                         | S11.1, S11.2, S11.3, S11.4, S11.5, S11.8 (`dry_run`, `graph` personal), S11.13, S11.16                                                                                       | M3         | Entra app for the personal account; the Excel template on the host; a test inbox |
| 6a  | `widen-b-harden` (done 2026-11-14)                                                 | review leftovers (`whatsapp`, `fetch`, `file-b`); D91                                                                                                                        | part of M4 | Nothing                                                                          |
| 6b  | `widen-b-talk` (done 2026-11-14)                                                   | rest of E4 (S4.3–S4.10, commands incl. `/pendientes`), E5 (S5.2, S5.3), BR-R09 brand question                                                                                | part of M4 | Nothing                                                                          |
| 6c  | `widen-b-fetch` (done 2026-11-14)                                                  | S7.2 (+ `document_url` allowlist, `printed_urls`), S7.4, S7.5, S10.10, S11.9; portal config checks; S10.3, S10.4, S10.7 wait for a portal that needs them (D99)              | part of M4 | S0.6 (remaining store pages); Q5                                                 |
| 6d  | `widen-b-deliver` (done 2026-11-16)                                                | S11.11, S11.12, S11.14, S11.15, S11.17; review leftovers (`widen-b-fetch`) (D105)                                                                                            | part of M4 | S0.11; the bot's Outlook.com mailbox; M3 run (all for the live check only)       |
| 6e  | `widen-b-gaps-ops` (done 2026-11-16)                                               | E8 (S8.1–S8.3, S8.5, S8.8, S8.10, S8.11; D112); E15 (S12.3, S15.1, S15.2); gap leftovers (`shortcut:` logs, BR-E06, BR-R06, BR-F03) (D105)                                   | M4         | S0.10; an `age` recipient and the backup target                                  |
| 7   | `read-more` (done 2026-11-16)                                                      | S3.4, S3.8–S3.10; E6 (D121–D127)                                                                                                                                             | M5         | S0.1: private set growing to ≥ 30 pairs; S0.5                                    |
| 8   | `flow-a` (done 2026-11-17)                                                         | E9, S10.6 (D128–D135); landed: Google auth and `google-setup`, Drive and Sheets sinks, Gmail `email_inbox`, `sunat_sol` fetcher (`todo` until S0.4)                          | M6         | S0.4, Google OAuth app in production status (both for the live check only)       |
| 9a  | `ops-harden` (done 2026-11-17)                                                     | S8.9 (D139); code leftovers marked `ops-harden` below (D136–D140); landed: `/gaps` and `/gap ignorar`, daily GitHub App check, `auth-google` pinned mailboxes, all leftovers | part of M7 | Nothing                                                                          |
| 9b  | `flows-config` (done 2026-11-17; plan: `docs/plan-mvp-flows-config.md`, D142–D145) | flow A requires only the PDF; a third flow for the owner's boletas against their DNI (BR-R row, Sheets sink); checked against the real config (D141)                         | part of M7 | the DNI flow's sheet                                                             |
| 9c  | `ops-host` (done 2026-11-18; plan: `docs/plan-mvp-ops-host.md`, D146–D149)         | host leftovers marked `ops-host` below, applied to the local Ubuntu machine (D141); first deploy (D136)                                                                      | M7         | Tailscale signed in, Funnel allowed (first deploy only; D146)                    |
| 9d  | `ops-live` (done 2026-11-18; plan: `docs/plan-mvp-ops-live.md`, D150–D152)         | the 9a and 9c review findings; chat-free live checks; backup drill (D150–D154)                                                                                               | M7         | Meta verification; the Google and Entra apps                                     |
| 9d2 | `live-chat` (plan: `docs/plan-mvp-live-chat.md`, D154)                             | live runs that need a chat reply: flow A, then flow B; gaps live; read checks (M3–M7); when Meta verification is approved                                                    |
| 9e  | `ops-reload` (plan: `docs/plan-mvp-ops-reload.md`, D155–D160)                      | S8.7, S10.8 `SIGHUP`, S12.1, S12.4, S12.5 (D136); before 9d2, while Meta is pending (D154)                                                                                   | part of M7 | Nothing                                                                          |
| 9f  | `windows`                                                                          | run through the container image or WSL2 and document it; no native port (D141)                                                                                               | —          | a Windows machine to test on                                                     |
| 9g  | `private`                                                                          | S12.7 (repo private), then S16.2 (D52)                                                                                                                                       | M7         | the owner's word for S12.7 (not yet on 2026-11-18)                               |
| –   | v1.1 `contract`                                                                    | E13, E14                                                                                                                                                                     | M8         | —                                                                                |
| –   | optional `llm-fallback`                                                            | E3b                                                                                                                                                                          | —          | DeepSeek key; flows opted in (never flow B, D43)                                 |

**Why this order (D64):** the first three phases are a thin slice of the work flow for one real store, so the riskiest pieces (the store's site, the Excel, the mail) are proven before anything is widened. PDF input, the padrón and the synthetic generator wait for `read-more`, because a readable QR plus the downloaded XML already give every field the work flow needs. OCR moved forward to phase 3b (D81), because the QR decodes on only half of real photos.

**Phase 3 (`read`) is done when** an integration test shows a photo with a QR going `RECEIVED` → `EXTRACTING` → `ROUTING` → summary sent with the routed flow's label, a photo without a readable QR ends in `EXTRACT_FAILED` with BR-E01, the QR spike is recorded in `specs/spikes/qr.md`, and the lint, type and import checks pass. Phase 3 adds `zxing-cpp`, OpenCV and `lxml` (more QR decoders only if the spike asks for them; Tesseract comes with `read-more`) and puts extraction code in `src/invoicebot/extract/` with an import-linter contract. "≥ 30 real pairs meet R-2.9" is checked before v1 goes live (D56).

Order changed on 2026-11-12 (D52, D64): flow B (work, Microsoft) comes before flow A, as a thin slice first. The flow B design follows the owner's real reimbursement folders (D47 to D75 in spec 05). What each store's site asks for is in `specs/spikes/portals.md`.

## Carried over from the `whatsapp` review

Each item goes into the named phase's plan.

- ~~**`routing` (before the first live trial):** keyed sender hash.~~ Done 2026-11-13 in `docs/plan-sender-hash.md`: HMAC-SHA256 keyed by the age-store secret `sender_hash_key`; an empty secret now counts as missing.
- ~~**`routing` (widen-b-harden):** an outbox row that can never succeed (a text over Meta's limit, a recipient with no WhatsApp number) is retried 8 times. Mark it dead at once. Make the template language configurable instead of the `es_PE` default.~~ Done in `widen-b-harden`.
- **Telegram (`ops` or whenever a second channel lands):** `pipeline/handlers.py` and `pipeline/replies.py` import the WhatsApp adapter, as the plan asked. When the second channel arrives, put the download behind a per-channel media port and move the `<id>:<action>` button codec to a channel-neutral module. Check the media id against its pattern at the parse boundary, so a bad id cancels the invoice instead of being retried.
- **`ops-host` (moved to `docs/plan-mvp-ops-host.md`, D148):** the Compose database password is the dev default and travels in `DATABASE_URL`. On the VPS, take it from the age store or an env file outside the repo.

Rules for every phase plan: steps of one concern in at most about 3 files, each with its own test command; tests cite requirement and rule IDs; no real RUCs, phone numbers or photos in git (D27).

## Carried over from the `widen-b-talk` review

Each item goes into the `widen-b-fetch` plan unless named otherwise.

- (widen-b-fetch) A press on an older menu indexes the newest `choices`: store a menu id in the button.
- (widen-b-fetch) `/pendientes` re-sends parked questions without a rate limit.
- (widen-b-fetch) A resumed press whose action rejects its choice leaves the invoice unparked.
- (widen-b-fetch) `/pendientes` shows the prepared date in UTC (`shortcut:` in `commands.py`).
- (widen-b-fetch) A NUL in an inbound text fails the JSONB insert, so the webhook answers 503 and Meta redelivers it for days. Strip or reject control characters in `parse_webhook`, and answer 200 for a message that can never succeed.
- (widen-b-fetch) Profile ids have no pattern; the per-profile natural key (D93) assumes they never contain `|`. Give them one (`^[a-z0-9_-]+$`) in the config model.

## Carried over from the `read` review

Each item goes into the named phase's plan.

- **Next (owner, 2026-11-13): D81, OCR as the primary reader.** The QR spike reads 50 % of real WhatsApp photos (`specs/spikes/qr.md`), far below S3.3's 95 %. The owner chose OCR on every photo, with the QR only confirming or filling fields. D81 is recorded in spec 05, and phase 3b `ocr` (`docs/plan-mvp-ocr.md`) runs before `fetch`. It changes R-2.1, R-2.3, R-2.6, spec 03 §5 and the R-9.2 budget, and pulls S0.3, S3.5, S3.6 and S3.7 forward from `read-more`.
- **`ocr` (step 10):** the QR payload repairs the spike found (date with time, label before the RUC, word buyer type; the decimal comma is already read), and the XML fix for a `schemaLocation` namespace with a relative Windows path (2 of 10 real XMLs fail as `malformed`).
- **`ocr` (step 4):** an OCR field at confidence 0.9 would show as "Deducido" in the summary, because `route.py` detects inference by `confidence == INFERRED_CONFIDENCE`. Mark inference explicitly on `Field` instead.
- **`widen-b` (with S7.2 `qr_url`, moved from `fetch`; widen-b-fetch):** `document_url` is any `http(s)` string from a QR. Allowlist the SUNAT and store hosts before anything fetches it (SSRF).
- **`ops-host` (moved from `fetch`, a host setting; moved to `docs/plan-mvp-ops-host.md`, namespaces instead of users, D147):** the image helper runs as the bot's user with network access; rlimits cap memory, CPU and writes. Run it as a separate user without network (systemd-run or a podman sandbox) before the host is shared. The browser helper (D82) needs network, so it gets its own user with internet-only egress instead.
- ~~**`read-more`:** `scripts/check_fixtures.py` skips binary files, so a committed PDF or xlsx with a real RUC passes. Allow those types only under a fixture allowlist.~~ Done in `read-more`.

## Carried over from the `ocr` review

Each item goes into the named phase's plan.

- **`ops-host` (before the host is shared, moved from `fetch`; moved to `docs/plan-mvp-ops-host.md`, `oom_score_adj` and the container `mem_limit` instead of a cgroup, D147):** put the image and browser helpers and Tesseract under a cgroup memory cap on the 2 GB host.
- **`fetch` step 18:** set `RLIMIT_FSIZE=0` for Tesseract and document it; cap the OCR text size and the matches per parsing rule.
- **`fetch` step 17:** catch OpenCV errors in the helper and report the photo as unreadable instead of exiting non-zero (today the queue retries them).
- **`fetch` step 17:** the helper's `variant` and `decoder` strings are logged unchecked. Move the QR variant and decoder names to a module without cv2 and make the result fields `Literal`s of them (R-9.16).
- **`widen-b-fetch` (widen-b-fetch, D101):** drop `printed_urls` until a step reads them, or put them to use.
- **`fetch` step 12 (moved from `read-more`: a fetched XML is the first caller):** `parse_invoice_xml` relies on the hardened parser plus the docinfo check, not on its byte scan for `<!doctype` (a UTF-16 document without a BOM passes the scan). Before a caller lands, refuse input that does not start with `<` after an optional UTF-8 BOM and whitespace.
- ~~**`read-more`:** `tests/unit/test_extract_ocr.py` monkeypatches `subprocess.run`, which is not a port the project owns. Make the Tesseract command an argument that tests point at a fake script.~~ Done in `read-more`.
- **`read-more` live check (`docs/plan-mvp-read-more.md` § "Deferred: live check", item 2):** `_fix_serie` reads O, Q and D as 0 and I and L as 1 in the serie's last three characters (S0.3), so a real serie such as `FD01` is stored as `F001` when there is no QR. Revisit once the private set holds such a serie.
- **Known, accepted:** the helper's process group is killed once more after the helper exits, by the old process ID; a reused ID could be hit in that window, which is very small.

## Carried over from the `fetch` review

Each item goes into the named phase's plan.

- **`ops-host` gate (before live fetching on a shared host; moved to `docs/plan-mvp-ops-host.md`, D147):** Chromium runs with `--no-sandbox`, Playwright's default. Enable `chromium_sandbox=True` with user namespaces, or run the browser helper as its own uid or container.
- **`ops-host` (moved to `docs/plan-mvp-ops-host.md`, D147):** a test that kills the browser helper mid-fetch and then finds no `chrome-headless-shell` process left. Playwright starts the browser detached, so the helper's process group may not hold it.
- **`ops-host` (moved to `docs/plan-mvp-ops-host.md`, tmpfs cap, D147):** a hostile page can start many downloads into Playwright's artifacts folder before the page timeout. The folder now sits in the fetch's own `TMPDIR`, which the bot removes after every run, but nothing caps its size during the run. Cap the helper's disk.
- **VPS (defense in depth; deferred by D149):** run the browser helper in a network namespace or under nftables rules whose only allowed connection is its guard proxy, so a Chromium path that skips the proxy (WebRTC is closed by a launch flag) still reaches nothing.
- ~~**`ops-harden` (each Playwright upgrade):** loopback goes through the guard proxy only because Playwright adds `--proxy-bypass-list=<-loopback>`. Add a test that reads the launched Chromium's arguments, so an upgrade cannot drop it silently.~~ Done in `ops-harden`.
- **`widen-b-fetch` (with the next portal):** `check_config` renders each active portal's `pick` and `calendar` selectors from a sample, so a bad selector fails at startup instead of every fetch ending `technical`.
- ~~**`widen-b` (widen-b-harden):** a startup check that `retrieval.portal_timeout_s` stays below the worker lease (300 s, `queue/worker.py`), so a fetch never outlives its lease; a second run is already harmless (R-5.1).~~ Done in `widen-b-harden`.
- ~~**`widen-b` (widen-b-harden):** `EmailInbox.retry_schedule` is an unvalidated `list[str]`; give it `Portal.retry_schedule`'s delay type.~~ Done in `widen-b-harden`.

## Moved by the `fetch` plan (D82)

- S7.6's `httpx` transport and the `document_url` allowlist go to `widen-b` with S7.2.
- S10.8's brand choice by serie prefix (and its one-tap question) goes to `file-b`; its `SIGHUP` reload goes to `ops-reload`.
- The `BrowserPool` of spec 03 §8 is replaced by a browser helper process per fetch (D82).

## Moved by the `file-b` plan (D84–D90)

- BR-R09's one-tap brand question goes to `widen-b` with reply handling (D80): no store in `config/stores/` has two brands yet. `file-b` picks the brand by serie prefix, or the only brand.
- ~~The PKCE loopback login and the work-account attempt (S11.1, S0.8) go to `widen-b` with `inbox_rule`: the personal account allows device code.~~ Dropped by D91.
- The `_Sent` rename goes to `widen-b` with S11.12; `payments` and `payment_id` go there with S11.11.
- `/pendientes` listing a batch `prepared` for 2 days (S11.4) goes with the commands of `widen-b`.
- The `PORTAL_MISSING` and `TEMPLATE_INVALID` gaps are logged with a `shortcut:` until the gap service (E8, `widen-b-gaps-ops`, D105).
- **M3 live run (deferred by the owner on 2026-11-14):** the code is merged, the demo is not. Run `docs/plan-mvp-file-b.md` § "Deferred: live run (M3)" once the Entra app, the Excel template on the host and a test inbox are ready, before `widen-b` starts.

## Carried over from the `file-b` review

Each item goes into the named phase's plan.

- ~~**`widen-b` (widen-b-harden; the admin text is now in spec 06):** BR-D03 says "admin alert", but spec 06 has no admin text for it, so a failed or crashed send only tells the sender. Add a `BR-D03_admin` text and send it through the same admin loop as BR-D01.~~ Done in `widen-b-harden`.
- ~~**`widen-b` (widen-b-harden):** check the host of Graph upload-session URLs (`sinks/onedrive.py`, `sinks/mailer_graph.py`) against an allow-list (`*.sharepoint.com`, `*.files.1drv.com`, `graph.microsoft.com`, `*.office.com`), so the PDF, XML and Excel bytes are never PUT elsewhere.~~ Done in `widen-b-harden`.
- ~~**`widen-b` (widen-b-harden):** cap the pages `Drive.list_names` follows through `@odata.nextLink` (e.g. 50) and raise `GraphError` past it.~~ Done in `widen-b-harden`.
- ~~**`widen-b` (with the work account, S11.1):** `auth-microsoft --account` becomes a choice of the configured accounts, and a sign-in resumes only that account's `PENDING_AUTH` invoices.~~ Dropped by D91.
- ~~**`widen-b` (widen-b-harden):** a control character in a reading's text (`razon_social`, `serie`) makes lxml, the `.eml` builder or the drive path raise `ValueError`; map it to BR-D02 or strip it when the reading is stored.~~ Done in `widen-b-harden`.
- ~~**`read-more` or later:** `sinks/xlsx_formula.py` recurses without a depth limit (Excel caps nesting at 64) and refuses a leading `+` (`=+A1+B1`). The template is hash-pinned, so both only show up when a template is set up.~~ Done in `read-more`.
- **Known, accepted (D89):** a transient Graph error during an automatic send leaves the batch `sending`, so the retry ends with BR-D03 instead of resending, even for a 429 that sent nothing.

## Carried over from the `widen-b-harden` review

Each item goes into the named phase's plan.

- **M3 live run:** the upload-session allow-list (`UPLOAD_HOSTS`, `UPLOAD_HOST_SUFFIXES` in `sinks/onedrive.py`) comes from Microsoft's docs (`*.up.1drv.com`, `api.onedrive.com`, `outlook.office.com`). Record a real personal-account `uploadUrl` for a drive upload and a mail attachment, and pin the list to it.
- **`widen-b-fetch` (widen-b-fetch):** a value made only of unsafe characters is now `""` after cleaning, but `missing_required` still counts the field as present. Treat an empty field as absent.
- **`widen-b-fetch` (with the next portal):** `portal_timeout_s` only has to stay below the 300 s lease (`JOB_LEASE_S`); leave a margin for the helper start and the handler's database work (e.g. 30 s).
- **`widen-b-talk` (widen-b-talk):** a non-string `retry_schedule` entry (`[5]`) makes `parse_delay` raise `TypeError`, not a `ValidationError`, for a portal and the email inbox. Give both one `Annotated` delay type.
- ~~**`ops-harden`:** move the remaining state string constants (`actions.py`, `route.py`, `extract.py`, `fetch.py`, `deliver.py`, `handlers.py`) to `invoicebot/states.py` (added by `widen-b-talk`).~~ Done in `ops-harden`.
- ~~**`ops-harden`:** for a permanent Meta error code, store only the status and code in `last_error`, in case Meta's message echoes a parameter. The dead-row log line could also say the failure was permanent.~~ Done in `ops-harden`.

## Carried over from the `widen-b-fetch` review

Each item goes into the named phase's plan.

- **`widen-b-deliver` (widen-b-deliver):** per-fetcher retry schedules built in `app.py` instead of `if fetcher == …` in `pipeline/fetch.py` (open/closed).
- **`widen-b-deliver` (widen-b-deliver):** a channel-keyed media download port instead of `WhatsAppClient` in `pipeline/uploads.py`, plus `ButtonPress` and `MENU_MAX` moved to `channels/base.py` and an import-linter contract `pipeline` → `channels.whatsapp` (DIP).
- **`widen-b-deliver` (widen-b-deliver):** the outbox stores the channel message id, so a quote of BR-F07 matches its invoice (D102).
- **`widen-b-deliver` (widen-b-deliver; from the review-fix round):** the inbox `$search` stays on the serie (spec 03), because the stored numero has no leading zeros and Graph splits words at the hyphen, so `F001-123` misses `F001-00000123`. A store's other mail with the same serie can still push the invoice out of the 25 newest; search for both the plain and the zero-padded `{serie}-{numero}` and merge by message id, with a zero-padded test mail.
- **`widen-b-deliver` (widen-b-deliver):** a cancellation while the connection goes back to the pool, after the commit, makes `pipeline/fetch.py` and `pipeline/uploads.py` delete files whose rows are committed. Mark the commit done before the pool exit and clean up only when it isn't.
- ~~**`ops-harden`:** `SystemResolver`'s deadline abandons the `getaddrinfo` thread without stopping it, so a hung resolver fills the default executor that `asyncio.to_thread` shares. Give DNS its own small executor. Reachable today only through allowlisted hosts.~~ Done in `ops-harden`.
- ~~**`ops-harden`:** pin the Docker base image by digest and set `UV_PYTHON_DOWNLOADS=never`; cap the BR-D14 list (e.g. 10 items and "…"); add a webhook test where a real NUL byte makes Postgres refuse the middle message.~~ Done in `ops-harden`.

## Carried over from the `widen-b-deliver` review

Each item goes into the `widen-b-gaps-ops` plan unless named otherwise.

- ~~**D107 amended (owner, 2026-11-16):** payment candidates are ordered by the invoice date (`fecha`), then id, and BR-P02/BR-P03 show invoice dates; the 180-day window stays on `created_at`, so imported old claims stay matchable. Today the code orders and dates by `created_at` (`store/payments.py` `candidates`, `Candidate.created_at`, `pipeline/payments.py` `_set_line`/`_pending_line`).~~ Done in `widen-b-gaps-ops`.
- ~~`laptop_agent.ps1` `Test-JobFile` does not check the job's exact key set; jobs and receipts share the key and the signing. Refuse any other key set, or sign a `type` field on both sides.~~ Done in `widen-b-gaps-ops`.
- ~~`laptop_agent.ps1` forgets a failed job id, and jobs carry no expiry: a job restored from the OneDrive recycle bin is sent again. Keep the ids of final error receipts, or sign a `created_at` with a maximum age.~~ Done in `widen-b-gaps-ops`.
- ~~`payment_match` runs the subset search on the event loop (up to about 2^20 nodes with 20 candidates). Cap the nodes visited or run it in `asyncio.to_thread`.~~ Done in `widen-b-gaps-ops`.
- ~~With `mail.send_enabled: false`, `laptop_outlook` maps to `DryRun`, and `_resend_laptop` stores the `.eml` path as a `mail_jobs.job_id`. Refuse a mode whose `completion` is not `receipt` there.~~ Done in `widen-b-gaps-ops`.
- ~~BR-P03 for the `#id` form, when some ids are not candidates, prints the sum of only the valid ids as the amount. Show the pending list without it.~~ Done in `widen-b-gaps-ops`.

## Carried over from the `widen-b-gaps-ops` review

Each item goes into the named phase's plan.

- ~~**`ops-harden`:** `laptop_agent.ps1` removes a job id from `sent-jobs.txt` when the send fails, so a captured job dropped back into `_outbox/` within its 7 days is sent again (recipients stay allowlisted). Keep failed ids in a separate state so a replay is refused without writing a false `sent` receipt.~~ Done in `ops-harden`.
- ~~**`ops-harden`:** the BR-S06 [Aprobar] id carries only the RUC (D118), so a stale button from a rejected proposal approves the next one. Put a proposal version in the id and match it in `taught_links.decide` (a D118 change).~~ Done in `ops-harden`.
- ~~**`ops-harden`:** `scripts/restore_drill.sh` passes the database URL, password included, on the `psql`/`pg_restore` command line; use `PG*` variables or a `.pgpass`. `ops/backup.py` `_pg_environment` drops URL query keys such as `sslmode`; pass them through or refuse them.~~ Done in `ops-harden`.
- ~~**`ops-harden`:** an approval during the GitHub issue creation can lose the hint (`set_pending_issue_hint` updates 0 rows once the row is sent); queue a `comment` row when it does. `upsert_gap`'s `raised_now` can be true twice under concurrent raises; lock the row first if workers run in parallel.~~ Done in `ops-harden`.

## Carried over from the `read-more` review

Each item goes into the named phase's plan.

- **`read-more` live check (`docs/plan-mvp-read-more.md` § "Deferred: live check"):** S0.5, S0.1, the padrón RSS and disk measurement and a WhatsApp PDF; it records M5.
- ~~**`ops-harden`:** a 401 from validez keeps the cached SUNAT token until its TTL (at most a day); delete `kv['sunat_token']` on a 401.~~ Done in `ops-harden`.
- ~~**`ops-harden`:** a recheck that gets no answer overwrites the stored `estado_cp` with null; keep the last answer and bump only the count.~~ Done in `ops-harden`.
- ~~**`ops-harden`:** the lone-XML refusal (BR-E07) quotes the image byte cap as "the photo and PDF limit"; pick the cap per kind, as `handlers._max_mb` does.~~ Done in `ops-harden`.
- **Known, accepted:** `scripts/import_padron.py` reads a padrón line with no length cap; the ZIP is downloaded by the owner by hand, and `check_zip` caps entries and ratio.

## Carried over from `flow-a`

- **`flow-a` live check (`docs/plan-mvp-flow-a.md` § "Deferred: live check"):** Google OAuth client in production, `auth-google` and `google-setup`, an M6 run through Drive and Sheets, a Gmail `email_inbox` fetch, S0.4 (`config/sunat_sol.yaml` stays `todo` and out of the default chain if SOL gives no XML) and BR-D01; it records M6.
- **`flows-config` real-config check (2026-11-17):** the owner's `config/config.yaml` was rebuilt on the current `example.yaml` shape with the real RUCs, DNI, Google account mailboxes, WhatsApp phone number id and Entra client id; `scripts/check_config.py` exits 0 (only `PORTAL_NOT_ACTIVE` warnings for `todo` portals). Both Sheets sinks and the Drive sink are bot-created (`existing: false`). The live run must still fill in: the Google OAuth client id (`google.client_id`) and its secret in the age store, the Drive `root_folder_id` and both `spreadsheet_id`s from `google-setup --flow personal_google` and `--flow personal_dni`, the profiles' real WhatsApp numbers and legal names, `mail.owner_work_address`, and SUNAT `validez_api.enabled` (now `false`, the example's default, until its credentials exist). No sink is `existing`, so no extra `auth-google` sign-in is needed.
- ~~**`ops-harden`:** `auth-google` saves the token of whichever Google user signed in; request `openid email`, compare the address with one set per account in config and refuse a mismatch (py-security-auditor, 2026-11-17).~~ Done in `ops-harden`.
- **`flow-a` live check, SOL egress:** the sign-in's SUNAT-only `page.route` misses websockets and redirect hops and starts after the login page's first load, so a compromised SUNAT script could send the typed credentials off SUNAT. Record the hosts the live SOL login and query use, then give `GuardProxy` a SUNAT host allowlist for the SOL session (`CONNECT` and `GET`) and install the route before `goto` (py-security-auditor, 2026-11-17).
- ~~**`ops-harden`:** `GoogleTokens` detects a new token file by `st_mtime_ns` only; compare `(st_ino, st_mtime_ns, st_size)` for filesystems with coarse timestamps. The `auth-google` loopback listener ends on the first request carrying any `state`; accept only the matching one (fails safe today; py-security-auditor, 2026-11-17).~~ Done in `ops-harden`.

## Known drift between code and specs

The specs run ahead of the code in these places; each has a named story that closes it.

- `scripts/check_config.py` and `config/models.py` still read a single `portals.yaml`: S10.8 moves them to `config/stores/` and `config/portals/`.
- The `batch_preview` template (S0.7) is only needed for the post-MVP `on_command` mode.
- S1.2's LLM check names the `work_microsoft` flow, `example.yaml` has an example routing id `BR-R10` with no catalog entry, and `retrieval.email_inbox` is global (widen-b-fetch, step 20: per-flow, D98). The flow config rework in `file-b` replaces them with per-flow settings (D53).

## Last (owner, D153)

Not a priority; after everything else.

- S0.4: does SOL "Consulta de comprobantes" give a received invoice's XML or PDF? If it does, fill `config/sunat_sol.yaml`, build D152's host allowlist (the 9d plan's old step 13), and add `sunat_sol` back to flow A's chain.
- Nubefact's public "Busca tu comprobante" page as a `portal_registry` entry for invoices issued through Nubefact.

## Notes for later phase plans

- `routing`: S4.5 also owns the reply to `EXTRACT_FAILED` (a better photo or `campo=valor`); today a new photo creates a new invoice.
- `widen-b-gaps-ops`: E8 owns BR-E06 and BR-R06 (D105).
- `file-b`: the R-8.2 failure message is specified in S9.4 (flow A); E11 must cover it for flow B.

## Carried over from the `ops-host` review

- **`ops-live`:** the browser helper keeps the network and can read all of `/data`, filed photos included; its downloads land there, so a mask would break fetching. Give each fetch its own download dir and mask the rest of `/data`, or record the exposure under D147 (py-security-auditor, 2026-11-18).
- **Later:** the browser helper sees the container's real `/proc` (other processes' command lines; `environ` and `root` are denied). A fresh procfs is refused under rootless Podman (py-security-auditor, 2026-11-18).

## Carried over from the `flows-config` review

- **`ops-live`, before any sink is `existing: true`:** the `spreadsheets` scope reaches every sheet in that Google account, not only the configured one. Prefer a dedicated bot account with only the target sheet shared to it, or record the blast radius under D145 (py-security-auditor, 2026-11-17).
- **Later:** an `existing` sheet's header is checked only by `google-setup`; a later column edit by the owner, or a changed `tab`, makes the bot write misaligned rows. Check the header once per process for `existing` sinks, or document that a config edit means running `google-setup` again (py-security-auditor, 2026-11-17).
- **Later (optional):** the sink kind literals (`"google_drive"`, `"google_sheets"`) live in `config/models.py` and `app.py`; one home. A header cell starting `=`, `+` or `-` is escaped on write, so `ensure_tab` would then always report a mismatch; note it or compare escaped (py-code-reviewer, 2026-11-17).

## Found by the `ops-live` backup (2026-11-18)

- **`ops-reload`:** the JSON log formatter drops `extra=` fields, so `backup failed` and `channel token rejected` reach the log without their `reason` or `channel`. Render `extra` in the JSON line (never secrets: the keys in use are reasons, channels, task keys and counts) and test it.
- **`ops-reload`:** a successful backup logs nothing; log `backup uploaded` with the two sizes, so the owner can see the nightly run.

## Carried over from the `ops-harden` review

Found by `py-code-reviewer` and `py-security-auditor` on 2026-11-17; by the owner's decision (D141) they wait for the live runs and go into the `ops-live` plan unless named otherwise.

- **`ops-live`:** `/gap ignorar` is undone by an issue already queued: `mark_reported` sets `reported` unconditionally and `issue_opened` alerts. Keep `ignored` in `mark_reported` and skip the alert for an ignored gap (D139).
- **`ops-live`:** `teach._record_hint` queues a comment with no `issue_url` even when no `create_issue` row is `sending` (GitHub enabled later, or the create row went dead), and `pipeline/gaps._issue_queued` has no `op` filter, so that comment waits forever and blocks a new `create_issue`. Queue it only while a create row is `sending`; filter `_issue_queued` on `op = 'create_issue'`.
- **`ops-live`:** the UBL path stores `num_doc_adq` unchecked (`extract/ubl.py`), while the QR path applies `NUM_DOC_ADQ_PATTERN`; a crafted XML can put lines or links into `/gaps` and the BR-R06 gap, or push `/gaps` past the 4096-character text cap. Apply the pattern at the UBL boundary.
- **`ops-live`:** `scripts/restore_drill.sh` drops the URL query (`sslmode` falls back to `prefer`) and lowercases the host; map the same keys as `PG_ENVIRONMENT_BY_QUERY_KEY` and refuse others.
- **VPS (deferred by D149, the local host is `x86_64`):** `deploy/Dockerfile` may pin the python base to its amd64 image digest, not the multi-arch index; pin the index digest if the host is not amd64.
- **Later (optional):** one home for the Google account derivation (`config/models._google_accounts_in` copies `auth/providers`); revoke the grant of a refused wrong-mailbox sign-in; compare-and-delete the SUNAT token on a 401; one shared DNS executor instead of one per browser session; a wiring test for the `github_health` choice; `/gaps` fetches 21 to show `…` only when cut, with its text in `messages_es.py`; one email normalization (`lower` vs `casefold`).

## Carried over from the `ops-live` review

Found by `py-code-reviewer` and `py-security-auditor` on 2026-11-18. The two Required findings, the `_record_hint` lock order and a sign-in that left two accounts in the cache, were fixed on the branch.

- **`ops-reload`:** the image helper still sees all of `/data` (`IMAGE_SCRIPT`); give it the browser helper's data-root mask, since it reads its input from stdin, or record why it is exempt under D150 (py-security-auditor).
- **`ops-reload`:** the sandbox checks `HELPER_OUT_DIR` against the data root as text, so `$data/../x` passes; reject `..` components in the shell and in `browser_prefix`, with a test (both reviewers). Today both paths come from the app and the operator.
- **Later:** `TokenProvider` uses the cache's one account without checking it is the configured mailbox; a wrong-mailbox device-code sign-in goes unnoticed. Store and compare the signed-in account, or match a configured mailbox (py-security-auditor).
- **Later (optional):** `scripts/restore_drill.sh` repeats the allowed `PG*` keys of `PG_ENVIRONMENT_BY_QUERY_KEY`; let the Python side print only allowed keys (py-code-reviewer).
