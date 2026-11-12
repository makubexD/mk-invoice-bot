# Roadmap: mk-invoice-bot MVP (v1 = M1–M7)

Source of truth: `specs/` (constitution, requirements, design, backlog in `specs/04-tasks.md`). Each phase becomes its own step plan, `docs/plan-mvp-<phase>.md`, written when the previous phase finishes. A phase is done when its milestone demo in spec 04 passes.

| #   | Phase (plan file)              | Backlog                                             | Milestone  | Needs from the owner first                                                               |
| --- | ------------------------------ | --------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------- |
| 1   | `foundation` (done 2026-11-11) | E1 (S1.1–S1.10, S5.1, S12.2)                        | part of M1 | Nothing                                                                                  |
| 2   | `whatsapp` (done 2026-11-11)   | E2 (incl. S2.7–S2.9 window and templates)           | part of M1 | S0.7: Meta app, dedicated number, utility templates submitted                            |
| 3   | `read`                         | S3.1, S3.2, S3.3, S3.11, S3.12; S7.3, S7.7; S4.1, S4.2; spike S0.2 | M1 | ≥ 10 receipt photos sent through WhatsApp (any receipt with a QR) |
| 4   | `fetch`                        | S7.1, S7.6, S10.1, S10.2, S10.5, S10.8, S10.9, S0.9 | M2 | La Leña's portal file completed (`config/portals/facturactiva-la-lena.yaml`) |
| 5   | `file-b`                       | S11.1, S11.2, S11.3, S11.4, S11.5, S11.8 (`dry_run`, `graph` personal), S11.13, S11.16 | M3 | Entra app for the personal account; the Excel template on the host; a test inbox |
| 6   | `widen-b`                      | rest of E4 and E5; S7.2, S7.4, S7.5; E8 (MVP part); S10.3, S10.4, S10.7, S10.10; S11.9, S11.11, S11.12, S11.14, S11.15, S11.17; E15 | M4 | S0.6 (remaining store pages), S0.8, S0.10, S0.11; Q5 (inbox for the email fallback); the bot's Outlook.com mailbox |
| 7   | `read-more`                    | spike S0.3; S3.4–S3.10; E6                          | M5 | S0.1: private set growing to ≥ 30 pairs; S0.5 |
| 8   | `flow-a`                       | E9, S10.6                                           | M6 | S0.4, Google OAuth app in production status |
| 9   | `ops`                          | E12, then S12.7 (repo private), then S16.2          | M7 | VPS chosen (D42) |
| –   | v1.1 `contract`                | E13, E14                                            | M8 | — |
| –   | optional `llm-fallback`        | E3b                                                 | —          | DeepSeek key; flows opted in (never flow B, D43)                                         |

**Why this order (D64):** the first three phases are a thin slice of the work flow for one real store, so the riskiest pieces (the store's site, the Excel, the mail) are proven before anything is widened. OCR, PDF input, the padrón and the synthetic generator wait for `read-more`, because a readable QR plus the downloaded XML already give every field the work flow needs.

**Phase 3 (`read`) is done when** an integration test shows a photo with a QR going `RECEIVED` → `EXTRACTING` → `ROUTING` → summary sent with the routed flow's label, a photo without a readable QR ends in `EXTRACT_FAILED` with BR-E01, the QR spike is recorded in `specs/spikes/qr.md`, and the lint, type and import checks pass. Phase 3 adds `zxing-cpp`, OpenCV and `lxml` (more QR decoders only if the spike asks for them; Tesseract comes with `read-more`) and puts extraction code in `src/invoicebot/extract/` with an import-linter contract. "≥ 30 real pairs meet R-2.9" is checked before v1 goes live (D56).

Order changed on 2026-11-12 (D52, D64): flow B (work, Microsoft) comes before flow A, as a thin slice first. The flow B design follows the owner's real reimbursement folders (D47 to D75 in spec 05). What each store's site asks for is in `specs/spikes/portals.md`.

## Carried over from the `whatsapp` review

Each item goes into the named phase's plan.

- **`routing` (before the first live trial):** the logged sender hash is an unkeyed, truncated SHA-256 of the phone number. With only about 10^8–10^9 Peruvian mobile numbers, anyone can brute-force it back to the number. Make it an HMAC keyed with a new secret from the age store (R-1.2, R-9.12).
- **`routing`:** an outbox row that can never succeed (a text over Meta's limit, a recipient with no WhatsApp number) is retried 8 times. Mark it dead at once. Make the template language configurable instead of the `es_PE` default.
- **Telegram (`ops` or whenever a second channel lands):** `pipeline/handlers.py` and `pipeline/replies.py` import the WhatsApp adapter, as the plan asked. When the second channel arrives, put the download behind a per-channel media port and move the `<id>:<action>` button codec to a channel-neutral module. Check the media id against its pattern at the parse boundary, so a bad id cancels the invoice instead of being retried.
- **`ops`:** the Compose database password is the dev default and travels in `DATABASE_URL`. On the VPS, take it from the age store or an env file outside the repo.

Rules for every phase plan: steps of one concern in at most about 3 files, each with its own test command; tests cite requirement and rule IDs; no real RUCs, phone numbers or photos in git (D27).

## Known drift between code and specs

The specs run ahead of the code in these places; each has a named story that closes it.

- `scripts/check_config.py` and `config/models.py` still read a single `portals.yaml`: S10.8 moves them to `config/stores/` and `config/portals/`.
- `config/example.yaml` still shows flow B with `on_command`, `/enviar` and `can_send_batch`, and has no `extraction.igv_rates`, `accepted_tipos`, `reimbursement_cap` or mail modes: `read` adds `igv_rates` and `supported_tipos`, `file-b` adds the rest (target shape in spec 03 §7).
- The `batch_preview` template (S0.7) is only needed for the post-MVP `on_command` mode.
- `pipeline/intake.py` starts an invoice only from a photo: S3.10 adds PDFs in `read-more` (an XML on its own gets BR-E07).
- `scripts/check_fixtures.py` scans only `tests/` and `config/`; S3.12 makes it scan every tracked file (R-9.17).
- The `files.kind` check has no `eml` or `manifest`: the flow-b migration adds them.
- S1.2's LLM check names the `work_microsoft` flow, `example.yaml` has an example routing id `BR-R10` with no catalog entry, and `retrieval.email_inbox` is global. The flow config rework in `file-b` replaces them with per-flow settings (D53).

## Notes for later phase plans

- `routing`: S4.5 also owns the reply to `EXTRACT_FAILED` (a better photo or `campo=valor`); today a new photo creates a new invoice.
- `widen-b`: E8 owns BR-E06 and BR-R06.
- `file-b`: the R-8.2 failure message is specified in S9.4 (flow A); E11 must cover it for flow B.
