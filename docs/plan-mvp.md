# Roadmap: mk-invoice-bot MVP (v1 = M1–M7)

Source of truth: `specs/` (constitution, requirements, design, backlog in `specs/04-tasks.md`). Each phase becomes its own step plan, `docs/plan-mvp-<phase>.md`, written when the previous phase finishes. A phase is done when its milestone demo in spec 04 passes.

| #   | Phase (plan file)              | Backlog                                                                                                                             | Milestone  | Needs from the owner first                                                                                         |
| --- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------ |
| 1   | `foundation` (done 2026-11-11) | E1 (S1.1–S1.10, S5.1, S12.2)                                                                                                        | part of M1 | Nothing                                                                                                            |
| 2   | `whatsapp` (done 2026-11-11)   | E2 (incl. S2.7–S2.9 window and templates)                                                                                           | part of M1 | S0.7: Meta app, dedicated number, utility templates submitted                                                      |
| 3   | `read` (done 2026-11-13)       | S3.1, S3.2, S3.3, S3.11, S3.12; S7.3, S7.7; S4.1, S4.2; spike S0.2                                                                  | M1         | ≥ 10 receipt photos sent through WhatsApp (any receipt with a QR)                                                  |
| 3b  | `ocr` (done 2026-11-13)        | spike S0.3; S3.5, S3.6, S3.7 (D81)                                                                                                  | M1b        | Tesseract with Spanish on the host; the private samples folder                                                     |
| 4   | `fetch` (done 2026-11-13)      | S7.1, S7.6, S10.1, S10.2, S10.5, S10.8, S10.9, S0.9                                                                                 | M2         | La Leña's portal file completed (`config/portals/facturactiva-la-lena.yaml`)                                       |
| 5   | `file-b`                       | S11.1, S11.2, S11.3, S11.4, S11.5, S11.8 (`dry_run`, `graph` personal), S11.13, S11.16                                              | M3         | Entra app for the personal account; the Excel template on the host; a test inbox                                   |
| 6   | `widen-b`                      | rest of E4 and E5; S7.2, S7.4, S7.5; E8 (MVP part); S10.3, S10.4, S10.7, S10.10; S11.9, S11.11, S11.12, S11.14, S11.15, S11.17; E15 | M4         | S0.6 (remaining store pages), S0.8, S0.10, S0.11; Q5 (inbox for the email fallback); the bot's Outlook.com mailbox |
| 7   | `read-more`                    | S3.4, S3.8–S3.10; E6                                                                                                                | M5         | S0.1: private set growing to ≥ 30 pairs; S0.5                                                                      |
| 8   | `flow-a`                       | E9, S10.6                                                                                                                           | M6         | S0.4, Google OAuth app in production status                                                                        |
| 9   | `ops`                          | E12, then S12.7 (repo private), then S16.2                                                                                          | M7         | VPS chosen (D42)                                                                                                   |
| –   | v1.1 `contract`                | E13, E14                                                                                                                            | M8         | —                                                                                                                  |
| –   | optional `llm-fallback`        | E3b                                                                                                                                 | —          | DeepSeek key; flows opted in (never flow B, D43)                                                                   |

**Why this order (D64):** the first three phases are a thin slice of the work flow for one real store, so the riskiest pieces (the store's site, the Excel, the mail) are proven before anything is widened. PDF input, the padrón and the synthetic generator wait for `read-more`, because a readable QR plus the downloaded XML already give every field the work flow needs. OCR moved forward to phase 3b (D81), because the QR decodes on only half of real photos.

**Phase 3 (`read`) is done when** an integration test shows a photo with a QR going `RECEIVED` → `EXTRACTING` → `ROUTING` → summary sent with the routed flow's label, a photo without a readable QR ends in `EXTRACT_FAILED` with BR-E01, the QR spike is recorded in `specs/spikes/qr.md`, and the lint, type and import checks pass. Phase 3 adds `zxing-cpp`, OpenCV and `lxml` (more QR decoders only if the spike asks for them; Tesseract comes with `read-more`) and puts extraction code in `src/invoicebot/extract/` with an import-linter contract. "≥ 30 real pairs meet R-2.9" is checked before v1 goes live (D56).

Order changed on 2026-11-12 (D52, D64): flow B (work, Microsoft) comes before flow A, as a thin slice first. The flow B design follows the owner's real reimbursement folders (D47 to D75 in spec 05). What each store's site asks for is in `specs/spikes/portals.md`.

## Carried over from the `whatsapp` review

Each item goes into the named phase's plan.

- ~~**`routing` (before the first live trial):** keyed sender hash.~~ Done 2026-11-13 in `docs/plan-sender-hash.md`: HMAC-SHA256 keyed by the age-store secret `sender_hash_key`; an empty secret now counts as missing.
- **`routing`:** an outbox row that can never succeed (a text over Meta's limit, a recipient with no WhatsApp number) is retried 8 times. Mark it dead at once. Make the template language configurable instead of the `es_PE` default.
- **Telegram (`ops` or whenever a second channel lands):** `pipeline/handlers.py` and `pipeline/replies.py` import the WhatsApp adapter, as the plan asked. When the second channel arrives, put the download behind a per-channel media port and move the `<id>:<action>` button codec to a channel-neutral module. Check the media id against its pattern at the parse boundary, so a bad id cancels the invoice instead of being retried.
- **`ops`:** the Compose database password is the dev default and travels in `DATABASE_URL`. On the VPS, take it from the age store or an env file outside the repo.

Rules for every phase plan: steps of one concern in at most about 3 files, each with its own test command; tests cite requirement and rule IDs; no real RUCs, phone numbers or photos in git (D27).

## Carried over from the `read` review

Each item goes into the named phase's plan.

- **Next (owner, 2026-11-13): D81, OCR as the primary reader.** The QR spike reads 50 % of real WhatsApp photos (`specs/spikes/qr.md`), far below S3.3's 95 %. The owner chose OCR on every photo, with the QR only confirming or filling fields. D81 is recorded in spec 05, and phase 3b `ocr` (`docs/plan-mvp-ocr.md`) runs before `fetch`. It changes R-2.1, R-2.3, R-2.6, spec 03 §5 and the R-9.2 budget, and pulls S0.3, S3.5, S3.6 and S3.7 forward from `read-more`.
- **`ocr` (step 10):** the QR payload repairs the spike found (date with time, label before the RUC, word buyer type; the decimal comma is already read), and the XML fix for a `schemaLocation` namespace with a relative Windows path (2 of 10 real XMLs fail as `malformed`).
- **`ocr` (step 4):** an OCR field at confidence 0.9 would show as "Deducido" in the summary, because `route.py` detects inference by `confidence == INFERRED_CONFIDENCE`. Mark inference explicitly on `Field` instead.
- **`widen-b` (with S7.2 `qr_url`, moved from `fetch`):** `document_url` is any `http(s)` string from a QR. Allowlist the SUNAT and store hosts before anything fetches it (SSRF).
- **`ops` (moved from `fetch`, a host setting):** the image helper runs as the bot's user with network access; rlimits cap memory, CPU and writes. Run it as a separate user without network (systemd-run or a podman sandbox) before the host is shared. The browser helper (D82) needs network, so it gets its own user with internet-only egress instead.
- **`read-more`:** `scripts/check_fixtures.py` skips binary files, so a committed PDF or xlsx with a real RUC passes. Allow those types only under a fixture allowlist.

## Carried over from the `ocr` review

Each item goes into the named phase's plan.

- **`ops` (before the host is shared, moved from `fetch`):** put the image and browser helpers and Tesseract under a cgroup memory cap on the 2 GB host.
- **`fetch` step 18:** set `RLIMIT_FSIZE=0` for Tesseract and document it; cap the OCR text size and the matches per parsing rule.
- **`fetch` step 17:** catch OpenCV errors in the helper and report the photo as unreadable instead of exiting non-zero (today the queue retries them).
- **`fetch` step 17:** the helper's `variant` and `decoder` strings are logged unchecked. Move the QR variant and decoder names to a module without cv2 and make the result fields `Literal`s of them (R-9.16).
- **`widen-b`:** drop `printed_urls` until a step reads them, or put them to use.
- **`fetch` step 12 (moved from `read-more`: a fetched XML is the first caller):** `parse_invoice_xml` relies on the hardened parser plus the docinfo check, not on its byte scan for `<!doctype` (a UTF-16 document without a BOM passes the scan). Before a caller lands, refuse input that does not start with `<` after an optional UTF-8 BOM and whitespace.
- **`read-more`:** `tests/unit/test_extract_ocr.py` monkeypatches `subprocess.run`, which is not a port the project owns. Make the Tesseract command an argument that tests point at a fake script.
- **`read-more`:** `_fix_serie` reads O, Q and D as 0 and I and L as 1 in the serie's last three characters (S0.3), so a real serie such as `FD01` is stored as `F001` when there is no QR. Revisit once the private set holds such a serie.
- **Known, accepted:** the helper's process group is killed once more after the helper exits, by the old process ID; a reused ID could be hit in that window, which is very small.

## Carried over from the `fetch` review

Each item goes into the named phase's plan.

- **`ops` gate (before live fetching on a shared host):** Chromium runs with `--no-sandbox`, Playwright's default. Enable `chromium_sandbox=True` with user namespaces, or run the browser helper as its own uid or container.
- **`ops`:** a test that kills the browser helper mid-fetch and then finds no `chrome-headless-shell` process left. Playwright starts the browser detached, so the helper's process group may not hold it.
- **`ops`:** a hostile page can start many downloads into Playwright's artifacts folder before the page timeout. The folder now sits in the fetch's own `TMPDIR`, which the bot removes after every run, but nothing caps its size during the run. Cap the helper's disk.
- **`ops` (defense in depth):** run the browser helper in a network namespace or under nftables rules whose only allowed connection is its guard proxy, so a Chromium path that skips the proxy (WebRTC is closed by a launch flag) still reaches nothing.
- **`ops` (each Playwright upgrade):** loopback goes through the guard proxy only because Playwright adds `--proxy-bypass-list=<-loopback>`. Add a test that reads the launched Chromium's arguments, so an upgrade cannot drop it silently.
- **`widen-b` (with the next portal):** `check_config` renders each active portal's `pick` and `calendar` selectors from a sample, so a bad selector fails at startup instead of every fetch ending `technical`.
- **`widen-b` (with the next portal):** a startup check that `retrieval.portal_timeout_s` stays below the worker lease (300 s, `queue/worker.py`), so a fetch never outlives its lease; a second run is already harmless (R-5.1).
- **`widen-b` (with S10.3, the email fetcher):** `EmailInbox.retry_schedule` is an unvalidated `list[str]`; give it `Portal.retry_schedule`'s delay type.

## Moved by the `fetch` plan (D82)

- S7.6's `httpx` transport and the `document_url` allowlist go to `widen-b` with S7.2.
- S10.8's brand choice by serie prefix (and its one-tap question) goes to `file-b`; its `SIGHUP` reload goes to `ops`.
- The `BrowserPool` of spec 03 §8 is replaced by a browser helper process per fetch (D82).

## Known drift between code and specs

The specs run ahead of the code in these places; each has a named story that closes it.

- `scripts/check_config.py` and `config/models.py` still read a single `portals.yaml`: S10.8 moves them to `config/stores/` and `config/portals/`.
- `config/example.yaml` still shows flow B with `on_command`, `/enviar` and `can_send_batch`, and has no `extraction.igv_rates`, `accepted_tipos`, `reimbursement_cap` or mail modes: `read` adds `igv_rates` and `supported_tipos`, `file-b` adds the rest (target shape in spec 03 §7).
- The `batch_preview` template (S0.7) is only needed for the post-MVP `on_command` mode.
- `pipeline/intake.py` starts an invoice only from a photo: S3.10 adds PDFs in `read-more` (an XML on its own gets BR-E07).
- The `files.kind` check has no `eml` or `manifest`: the flow-b migration adds them.
- S1.2's LLM check names the `work_microsoft` flow, `example.yaml` has an example routing id `BR-R10` with no catalog entry, and `retrieval.email_inbox` is global. The flow config rework in `file-b` replaces them with per-flow settings (D53).

## Notes for later phase plans

- `routing`: S4.5 also owns the reply to `EXTRACT_FAILED` (a better photo or `campo=valor`); today a new photo creates a new invoice.
- `widen-b`: E8 owns BR-E06 and BR-R06.
- `file-b`: the R-8.2 failure message is specified in S9.4 (flow A); E11 must cover it for flow B.
