# Plan: MVP phase 6c · widen-b-fetch

Roadmap: `docs/plan-mvp.md` row 6c. More ways for flow B to get an invoice's PDF and XML before it asks the sender: the QR link (S7.2), the bot's Outlook.com mailbox (S11.9), an honest BR-F07 list (S7.4), files the sender sends for a stuck invoice (S7.5), stores without a website (S10.10), plus the review leftovers carried to this phase.

**Goal:** an invoice whose portal has nothing ends with a BR-F07 message that says, one line per fetcher, what was tried and why it failed. Before that, the QR's link (when it points at SUNAT or a known store host) and the bot's mailbox are searched; the mailbox is retried on its 5m/1h/6h/24h schedule with "esperando correo". A sender who then sends the PDF and XML gets the invoice delivered; an XML that is not this invoice is refused.

**Needs from the owner first:** Q5 is answered (2026-11-14): each flow has its own dedicated mailbox; flow B's is an Outlook.com account, flow A's a Gmail account. This plan takes flow B's to be the bot's own Outlook.com mailbox of D63 (the one `widen-b-deliver` sends from). The S0.6 store pages (PanYFuego, Intiwarma, Umari) are still missing; nothing here needs them. The mailbox itself and its sign-in are needed only for the live check, not for the code.

## Design choices

Step 1 writes these into the specs (spec 05, D98 to D101).

- **D98 · Each flow reads its own mailbox (Q5).** `retrieval.email_inbox` moves into the flow: `flows.<name>.email_inbox: {provider, account, retry_schedule}` (D53, closes the drift line about a global inbox). Flow B: `provider: graph`, `account: bot`, the bot's Outlook.com mailbox, signed in with `offline_access` and `Mail.Read` only (R-6.5, least privilege). Flow A: `provider: gmail`, built in `flow-a`. Scopes are per account: `Files.ReadWrite` only for an account a `onedrive_folder` sink writes to (`onedrive_<account>`), `Mail.Send` only for a Graph mailer's account, `Mail.Read` only for a Graph inbox's account. `auth-microsoft --account` accepts only an account the config uses, and a sign-in resumes `PENDING_AUTH` invoices only for an account a sink uses. BR-D01's admin text names the account. This narrows D91: still no work account; the personal account and the bot mailbox are both personal Microsoft accounts.
- **D99 · Captchas and custom adapters wait for a portal that needs them.** S10.3 and S10.4 (solve an image captcha, then ask the sender) and S10.7 (custom adapters) move out of this phase. The only image-captcha site (`lalucha-ddns`, 1 of 35 invoices) is `todo` with no fields, and asking the sender means holding a browser session across a 5-minute wait that one helper per fetch (D82) and the 300 s job lease do not allow; that design is made when the owner completes that portal file. Until then the `portal_registry` fetcher returns `captcha_unsolvable` for any portal whose `captcha` is not `none`, shown as BR-F04 in the BR-F07 list.
- **D100 · The BR-F07 list (S7.4).** One bullet per fetcher that ran, in chain order, from its last reason (table in step 16); a `qr_url` with no link in the QR adds no bullet; BR-F01's bullet is added when the flow does not list `sunat_sol`. BR-F03 ("ya avisé para arreglarlo") is not used until the gap service exists (`widen-b-deliver`); a technical failure gets a plain bullet instead. A mailbox that needs a sign-in waits on the inbox's retry schedule like "not found yet", and its first such attempt alerts the admins with BR-D01's admin text.
- **D101 · Sender uploads and the QR link.** R-1.5 is built for `MANUAL_REQUIRED` only; `CONFIRMING` waits for `read-more`, where `CONFIRMING` asks about fields. An upload is checked like a fetched file (size cap, hardened XML parse, R-5.6 match) and the newest file of a kind wins at delivery. The `qr_url` fetcher requests only an `https` link whose host is SUNAT's (`sunat.gob.pe` or a subdomain) or a host of a file in `config/portals/`, through the request guard, and keeps the answer only when it is a PDF or an XML. `printed_urls` is dropped: R-2.10 makes them gap hints, and the gap service in `widen-b-deliver` adds them back if it reads them.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py` and is quoted in spec 06 by step 1. No real RUCs, phone numbers or mail addresses in tests; use the synthetic values of `config/example.yaml`.

Out of scope here: S10.3, S10.4, S10.7 (D99); S10.10's store contact (BR-F11's text does not use it; it lands with the first text that does); BR-F02, BR-F03 and the gap service (`widen-b-deliver`); Gmail (`flow-a`); `check_config` rendering `pick`/`calendar` selectors (with the next portal).

## Step 1: Record D98–D101, Q5 and the new texts `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/02-requirements.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Spec 05: rows D98 to D101 with the text from "Design choices"; Q5 marked answered (2026-11-14, D98).
- Spec 06: BR-D01's admin text becomes `🔑 Reautoriza {provider} ({account}): ejecuta scripts/auth_{provider}.py --account {account}`; under BR-F06 and BR-F09 the upload replies of step 30; under BR-F07 the extra bullets of step 16; the "esperando correo" text of step 26.
- Spec 04: S10.3, S10.4, S10.7 cite D99 and name `lalucha-ddns` as their trigger; S7.5 cites D101 (`MANUAL_REQUIRED` only).
- Spec 02: R-1.5 notes `CONFIRMING` comes with `read-more` (D101); R-2.10 notes `printed_urls` is dropped until the gap service reads it (D101).
- Spec 03 §6: the `email_inbox` bullet names the per-flow setting and the Graph query of step 23; the `qr_url` host allowlist.
- Roadmap: row 6c lists D99's moves; under "Known drift" the `retrieval.email_inbox is global` part is marked `(widen-b-fetch, step 20)`; under the "Carried over" sections the items this plan builds are marked `(widen-b-fetch)`.

The code texts change in the steps that use them (BR-D01's admin text in step 2), so this step touches no source file.

Test: none (spec text only); `uv run pytest -q tests/unit/test_rules_parity.py` still passes.

## Step 2: BR-D01's admin text names the account `[worker:low]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `MESSAGES["BR-D01_admin"]` becomes spec 06's new text (step 1). Where deliver formats it, pass `account=MICROSOFT_ACCOUNT` (`personal`) next to `provider`.
- Test: the existing BR-D01 test asserts the admin text contains `--account personal`.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py -k auth`.

## Step 3: Profile ids get a pattern `[worker:low]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- `Config.profiles` keys must match `^[a-z0-9_-]+$` (D93's per-profile natural key assumes no `|`). Use `dict[Annotated[str, StringConstraints(pattern=...)], Profile]`; name the pattern `PROFILE_ID_PATTERN`.
- Tests (cite D93): `owner` and `maku_2` load; `a|b`, `Ana` and `""` raise `ValidationError`.

Test: `uv run pytest -q tests/unit/test_config_models.py -k profile`.

## Step 4: The portal timeout leaves a margin below the lease `[worker:low]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- Add `JOB_LEASE_MARGIN_S = 30` next to `JOB_LEASE_S`. `Retrieval`'s check becomes `portal_timeout_s + JOB_LEASE_MARGIN_S <= JOB_LEASE_S`, and its message names the margin.
- Tests (cite R-5.1): 270 passes; 271 and 300 raise.

Test: `uv run pytest -q tests/unit/test_config_models.py -k timeout`.

## Step 5: An empty field counts as absent `[worker:low]`

Files: `src/invoicebot/extract/fields.py`, `tests/unit/test_extract_fields.py`.

- `Reading.value(name)` returns `None` when the field's value is `""`; `missing_required` lists a required field whose value is `""`.
- Tests (cite R-2.13, R-12.8): a reading with `serie=""` lists `serie` as missing and `value("serie") is None`; `serie="F001"` is present.

Test: `uv run pytest -q tests/unit/test_extract_fields.py`.

## Step 6: Inbound text drops unsafe characters `[worker:low]`

Files: `src/invoicebot/channels/whatsapp/parse.py`, `tests/unit/test_whatsapp_parse.py`.

- `parse_webhook` passes the text body, a caption and a button title through `text.replace_unsafe_chars` before building the `InboundMessage` (a NUL fails the JSONB insert).
- Tests (cite R-12.8): a text `"ho\x00la"` parses to `"ho la"`; a caption with `‮` loses it; plain text is unchanged.

Test: `uv run pytest -q tests/unit/test_whatsapp_parse.py`.

## Step 7: A message that can never be stored is answered 200 `[worker]`

Files: `src/invoicebot/api/webhook.py`, `tests/integration/test_webhook.py`.

- In `_persist`'s caller, catch `psycopg.errors.DataError` (bad encoding, a value Postgres refuses) before the generic database branch: `logger.exception("webhook message refused by the database")` and answer 200, so Meta stops redelivering. Other `psycopg.Error`s stay 503.
- Tests (cite R-1.8): a handler double that raises `DataError` gets 200 and the log line; one that raises `OperationalError` still gets 503.

Test: `uv run pytest -q tests/integration/test_webhook.py`.

## Step 8: Button ids carry a menu number `[worker]`

Files: `src/invoicebot/channels/whatsapp/payloads.py`, `tests/unit/test_whatsapp_payloads.py`.

- `button_id(invoice_id, action, choice=None, menu=None)`: with a choice, `menu` is required and the id is `<id>:<action>:<choice>:<menu>`, `menu` an int 0–9999; without a choice, `menu` must be `None` (`ValueError` otherwise). `ButtonPress` gains `menu: int | None`; `parse_button_id` reads the 4-part form and refuses a 3-part id with a choice (an old menu sent before this change resolves to `None`, i.e. stale).
- Tests (cite R-3.5): round trip with choice 3 and menu 12; `menu=10000` raises; `"5:pick:3"` parses to `None`; `"5:cancel"` parses with `menu=None`.

Test: `uv run pytest -q tests/unit/test_whatsapp_payloads.py`.

## Step 9: A press on an older menu is stale `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/questions.py`, `src/invoicebot/pipeline/actions.py`.

- `route.py`, where it stores `choices`: also store `menu` = the previous `menu` in flow data plus 1 (0 when absent), and pass it to the question builder.
- `questions.flow_question` and `brand_question` take `menu` and pass it to `button_id` for every choice button.
- `actions._choice_at(conn, invoice_id, choice, menu)` returns `None` unless `menu` equals the stored `menu`; callers pass `press.menu`.
- Tests in `tests/integration/test_pipeline_actions.py` (cite R-3.5, BR-S02): ask flow (menu 0), then brand (menu 1); a pick press with menu 0 gets BR-S02 and changes nothing; a brand press with menu 1 is taken.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_pipeline_route.py`.

## Step 10: A bad choice does not unpark the invoice `[worker]`

Files: `src/invoicebot/pipeline/replies.py`, `src/invoicebot/pipeline/actions.py`, `tests/integration/test_park.py`.

- Make `_choice_at` public as `choice_at` in `actions.py`. In `resolve_reply`, before `_resumes_parked`, a press with a `choice` must have `await choice_at(conn, invoice_id, choice, menu)` not `None`; otherwise BR-S02 (`queue_stale`) and `None`, with the invoice still `PENDING_USER`.
- Tests (cite D94, BR-S02): a parked flow question; a press with choice 7 (no such choice) leaves `PENDING_USER` and `waiting_state` set, and queues BR-S02; a press with choice 0 and the right menu unparks and picks.

Test: `uv run pytest -q tests/integration/test_park.py`.

## Step 11: `/pendientes` re-sends a question at most every 10 minutes `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

- `requeue_last(conn, invoice_id, rule_id, min_interval: timedelta)` inserts only when the newest matching row's `created_at <= now() - min_interval` (database clock). `commands.py` adds `PENDING_REQUEUE_INTERVAL = timedelta(minutes=10)` next to `PENDING_REQUEUES` and passes it; a skipped row still counts toward `PENDING_REQUEUES`.
- Tests (cite S4.8, D94): `/pendientes` twice in a row queues the question once; after moving the outbox row's `created_at` 11 minutes back, a third `/pendientes` queues it again.

Test: `uv run pytest -q tests/integration/test_commands.py -k pendientes`.

## Step 12: `/pendientes` shows the prepared date in the owner's timezone `[worker]`

Files: `src/invoicebot/store/invoices.py`, `tests/integration/test_invoices_store.py`.

- `list_pending(..., timezone: str)` selects `(b.prepared_at AT TIME ZONE %(timezone)s)::date` (as `route.py` does); `PendingRow.batch_prepared_at` becomes `date | None`.
- Tests (cite S11.4): a batch prepared at `2026-11-14 03:30 UTC` lists `2026-11-13` with `America/Lima` and `2026-11-14` with `UTC`.

Test: `uv run pytest -q tests/integration/test_invoices_store.py -k pending`.

## Step 13: Commands get the owner's timezone `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/api/webhook.py`.

- Replace the commands' `admins` parameter with a frozen `CommandContext(admins: frozenset[str], timezone: str)`; `handle_command` and every command take it. `handle_inbound` takes `timezone: str` (keyword) and builds it; `WebhookDeps` gains `timezone`, filled in `app.py`'s `_webhook_deps` from `cfg.owner.timezone` (`app.py` is the one extra file). `_pendientes` passes it to `list_pending` and drops its `shortcut:`.
- Tests: existing `tests/integration/test_commands.py` and `test_intake.py` updated to the new signatures; the step 12 case through `/pendientes` (cite S11.4).

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_intake.py tests/integration/test_webhook.py`.

## Step 14: Drop `printed_urls` `[worker:low]`

Files: `src/invoicebot/extract/ocr_parse.py`, `config/parsing_rules.yaml`, `tests/unit/test_extract_ocr_parse.py`.

- Remove `OcrReading.printed_urls`, `_read_printed_urls` and the parsing rule that feeds them (and its model field in `extract/parsing_rules.py` if it has one: then that file instead of the YAML's loader test). Callers of `OcrReading` only read `.reading`.
- Tests: delete only the cases that assert `printed_urls` (D101 is the reason; say so in the commit); the rest pass unchanged.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_extract_ocr.py`.

## Step 15: New fetch reasons; the portal fetcher reports stores without a site and captchas `[worker]`

Files: `src/invoicebot/fetchers/base.py`, `src/invoicebot/fetchers/portal.py`, `tests/integration/test_fetchers_portal.py`.

- `FetchReason` gains `NOT_FOUND = "not_found"` (the source answered and holds no document; final), `NO_URL = "no_url"`, `NO_WEBSITE = "no_website"`, `CAPTCHA_UNSOLVABLE = "captcha_unsolvable"`, `AUTH_REQUIRED = "auth_required"`.
- `PortalFetcher._portal_for`: a store with `portal: manual` returns `NO_WEBSITE` (S10.10); a portal whose `captcha` is not `none` returns `CAPTCHA_UNSOLVABLE` with its key, checked before `status` (D99), with a `shortcut:` naming S10.3/S10.4 for `image`.
- Tests (cite S10.10, R-5.4): manual store → `no_website`, no helper run; `captcha: recaptcha` (status `todo`) → `captcha_unsolvable`; `captcha: image` → `captcha_unsolvable`; the existing cases still pass.

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py`.

## Step 16: The BR-F07 list (S7.4) `[worker]`

Files: `src/invoicebot/pipeline/manual_message.py` (new), `src/invoicebot/rules/messages_es.py`, `tests/unit/test_manual_message.py` (new).

- Move `_manual_message`, `_fetcher_lines`, `_reason_line` and `_REASON_RULES` out of `pipeline/fetch.py` into `manual_message.py` as `manual_message(reading, flow, attempts, directory) -> str`; `fetch.py` calls it (that edit is part of this step). Module docstring: builds BR-F07's text.
- Bullets, one per fetcher in first-attempt order, from its last reason:

  | last reason                                    | bullet                                                                                        |
  | ---------------------------------------------- | --------------------------------------------------------------------------------------------- |
  | `not_found_yet`, `not_found`, `missing_docs`   | `FETCH_NOT_FOUND_LINE` (exists)                                                               |
  | `no_url`                                       | none                                                                                          |
  | `no_portal`                                    | new `FETCH_NO_PORTAL_LINE = "• Portal del comercio: no lo conozco"`                           |
  | `no_website`                                   | BR-F11 with `comercio` = the store's first brand's `comercio`                                 |
  | `portal_inactive`                              | new `FETCH_PORTAL_TODO_LINE = "• Portal {portal}: todavía no sé consultarlo"`                 |
  | `captcha_unsolvable`                           | BR-F04                                                                                        |
  | `technical`, `timeout`                         | new `FETCH_TECHNICAL_LINE = "• {fetcher}: falló por un error técnico"`                        |
  | `guard_blocked`, `xml_invalid`, `xml_mismatch` | BR-F08, BR-F09, BR-F06 (as today)                                                             |
  | `auth_required`                                | new `FETCH_AUTH_LINE = "• Correo: el buzón necesita reconectarse; ya avisé al administrador"` |

  `{portal}` is the host of the store's portal `url`; `{fetcher}` is `fetcher_desc`. After the fetcher bullets, BR-F01's bullet when `"sunat_sol"` is not in `flow.fetchers`. `gap_line` stays `""` with its `shortcut:` pointing at `widen-b-deliver`.

- Tests (cite BR-F07, S7.4): one case per table row; BR-F01 present for a flow without `sunat_sol` and absent with it; two attempts of one fetcher give one bullet from the last; bullet order follows first attempt.

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/integration/test_pipeline_fetch.py`.

## Step 17: Guarded httpx transport `[worker:high: reasoning: pinning the resolved IP while TLS still checks the hostname needs httpcore's sni_hostname extension, a Host header and IPv6 brackets, and every redirect hop must be re-checked]`

Files: `src/invoicebot/net/http.py` (new), `src/invoicebot/net/guard.py`, `tests/unit/test_net_http.py` (new).

- Move `SystemResolver` from `fetchers/browser/routing.py` to `net/guard.py` (re-import it there; that file is the one extra edit).
- `GuardedTransport(httpx.AsyncBaseTransport)`, built with `inner: httpx.AsyncBaseTransport`, `resolver: Resolver`, `check: CheckAddresses = check_addresses`. `handle_async_request`: `check_url(url, frozenset())` (https only); `resolve_and_check(host, resolver, check)`; on a reason raise `GuardBlocked(reason)` (new exception carrying the `GuardReason`); else send to `inner` a copy of the request whose URL host is the pinned IP (bracketed for IPv6), with `Host` set to the original host and `extensions["sni_hostname"] = host`. The client never follows redirects itself (`follow_redirects=False`); callers re-send each hop, so every hop passes the transport.
- Tests with a `MockTransport` as `inner` and a fake resolver (cite R-5.10): https to a public IP goes out with the IP in the URL, the `Host` header and `sni_hostname`; `http:` → `http_not_allowed`; `file:` → `scheme_blocked`; a host resolving to `10.0.0.1` → `private_address`; one public and one private answer → blocked (rebinding); no answer → `unresolvable`; an IPv6 answer is bracketed.

Test: `uv run pytest -q tests/unit/test_net_http.py tests/unit/test_net_guard.py`.

## Step 18: The `document_url` allowlist `[worker:low]`

Files: `src/invoicebot/fetchers/qr_url.py` (new), `tests/unit/test_fetchers_qr_url.py` (new).

- `SUNAT_DOMAIN = "sunat.gob.pe"`. `allowed_hosts(directory: StoreDirectory) -> frozenset[str]`: the lowercased hosts of every portal's `url`. `is_allowed(url, hosts) -> bool`: scheme `https`, host lowercased equal to `SUNAT_DOMAIN` or ending in `.sunat.gob.pe`, or in `hosts`; anything unparseable is `False`.
- Tests (cite R-5.2, R-5.10): `https://e-factura.sunat.gob.pe/x` yes; `https://sunat.gob.pe.evil.test/` no; `https://evilsunat.gob.pe/` no; a portal host yes; the same host over `http` no; `https://user@host/` no.

Test: `uv run pytest -q tests/unit/test_fetchers_qr_url.py`.

## Step 19: The `qr_url` fetcher `[worker]`

Files: `src/invoicebot/fetchers/qr_url.py`, `src/invoicebot/fetchers/base.py`, `tests/unit/test_fetchers_qr_url.py`.

- `FetchInvoice` gains `document_url: str | None = None`.
- `QrUrlFetcher(http: httpx.AsyncClient, hosts: frozenset[str], max_bytes: MediaMaxBytes)`, `QR_URL_FETCHER = "qr_url"`, `MAX_REDIRECTS = 3`. `fetch`: no `document_url` → `NO_URL`; not `is_allowed` → `GUARD_BLOCKED`. GET with streaming; on a 3xx re-check the `Location` (resolved against the URL) with `is_allowed` and re-send, up to `MAX_REDIRECTS` (more → `GUARD_BLOCKED`). `GuardBlocked` → `GUARD_BLOCKED`; `httpx.TimeoutException` → `TIMEOUT`; other `httpx.HTTPError` or a non-200 → `TECHNICAL`. Read the body in chunks, stopping past the larger of the PDF and XML caps (`TECHNICAL`). Kind by content: `%PDF-` → pdf; `<` after an optional UTF-8 BOM and whitespace → xml; else `NOT_FOUND`. Over its kind's cap → `TECHNICAL`. An XML goes through `parse_invoice_xml` and `matches_invoice` (as `portal.py` does: `XmlRejected` → `XML_INVALID`, no match → `XML_MISMATCH`). Write the file to `out_dir/<kind>.<kind>`, hash it, return `OK` with that one file and `portal_key=None`.
- Tests with `MockTransport` (cite R-5.2, R-5.6): no url; a disallowed host (no request made); a PDF; a matching XML; a mismatching XML; an XML with a DOCTYPE; an HTML page → `not_found`; a body over the cap; a redirect to an allowed host followed; a redirect to a disallowed host blocked; 4 redirects blocked; a 500 → `technical`.

Test: `uv run pytest -q tests/unit/test_fetchers_qr_url.py`.

## Step 20: Per-flow inbox settings `[worker]`

Files: `src/invoicebot/config/models.py`, `config/config.yaml`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- Rename `EmailInbox` to `FlowInbox` with `provider: Literal["gmail", "graph"]`, `account: str` (pattern `^[a-z0-9_]+$`), `retry_schedule: tuple[Delay, ...]`. `Flow.email_inbox: FlowInbox | None = None`; `Retrieval.email_inbox` removed. A model validator on `Flow`: listing `email_inbox` in `fetchers` requires `email_inbox` settings.
- YAML: the `retrieval.email_inbox` block moves under each flow that lists the fetcher: flow A `provider: gmail, account: invoices_a`; flow B `provider: graph, account: bot`; both `retry_schedule: [5m, 1h, 6h, 24h]`.
- Tests (cite D98, R-5.2): both YAML files load; a flow listing `email_inbox` without settings raises; `retrieval.email_inbox` is refused (`extra="forbid"`); a bad account name raises.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_config_checks.py`.

## Step 21: Scopes per account; `auth-microsoft` signs in only a used account `[worker]`

Files: `src/invoicebot/auth/providers.py`, `src/invoicebot/cli.py`, `tests/integration/test_cli_auth.py`.

- `providers.py`: `BASE_SCOPES = ("offline_access",)`; `FILES_SCOPE = "Files.ReadWrite"`, `MAIL_READ_SCOPE = "Mail.Read"`. `scopes_for(cfg, account)` adds `Files.ReadWrite` when a flow has an `onedrive_folder` sink whose `targets` include `f"onedrive_{account}"`, `Mail.Send` as today, `Mail.Read` when a flow's `email_inbox` is `graph` with that account. `microsoft_accounts(cfg) -> frozenset[str]`: every account with a scope beyond `offline_access`. `delivery_accounts(cfg)`: those with `Files.ReadWrite` or `Mail.Send`.
- `cli.py`: an `--account` not in `microsoft_accounts(cfg)` exits 2 with a message naming the configured ones; after a sign-in, `PENDING_AUTH` invoices are resumed only when the account is in `delivery_accounts(cfg)`.
- Tests (cite R-6.5, R-7.9, D98): with `example.yaml`, `personal` gets `offline_access, Files.ReadWrite, Mail.Send` (as today) and `bot` gets `offline_access, Mail.Read`; `--account nobody` exits 2; signing in `bot` with a fake provider resumes no `PENDING_AUTH` invoice; signing in `personal` still resumes them.

Test: `uv run pytest -q tests/integration/test_cli_auth.py tests/unit/test_cli.py`.

## Step 22: Graph mail reader `[worker]`

Files: `src/invoicebot/fetchers/graph_mail.py` (new), `tests/unit/test_fetchers_graph_mail.py` (new).

- `GraphMailbox(http: httpx.AsyncClient, tokens: TokenProvider, account: str, timeout_s: float)`, Graph base `https://graph.microsoft.com/v1.0`, reusing `auth_headers` and `raise_graph_error` from `sinks/onedrive.py`. Methods:
  - `search(term: str, top: int) -> list[MailMessage]`: `GET /me/messages` with `$search="<term>"` (the term has `"` and `\` removed), `$top`, `$select=id,receivedDateTime,hasAttachments`; only messages with attachments. `MailMessage(id: str, received_at: datetime)`.
  - `attachments(message_id) -> list[MailAttachment]`: `GET /me/messages/{id}/attachments?$select=id,name,size,contentType`; `MailAttachment(id, name, size)`; non-file attachments (an `@odata.type` other than `#microsoft.graph.fileAttachment`) skipped.
  - `download(message_id, attachment_id, max_bytes) -> bytes`: `GET .../$value`, streamed, `ValueError` past `max_bytes`.
  - Ids from Graph are checked against `^[A-Za-z0-9_=+/-]+$` before they go into a URL path (`GraphError` otherwise) and URL-encoded.
- Tests with `MockTransport` (cite R-5.2, R-6.5): the search request's query string; a message without attachments dropped; the attachment listing; a download within and over the cap; a 401/403 raising as `raise_graph_error` does; a bad id refused without a request.

Test: `uv run pytest -q tests/unit/test_fetchers_graph_mail.py`.

## Step 23: The `email_inbox` fetcher `[worker]`

Files: `src/invoicebot/fetchers/email_inbox.py` (new), `tests/unit/test_fetchers_email_inbox.py` (new).

- A `Mailbox` `Protocol` with the three methods of step 22. `EmailInboxFetcher(mailbox: Mailbox, max_bytes: MediaMaxBytes)`, `EMAIL_INBOX_FETCHER = "email_inbox"`, `SEARCH_TOP = 25`, `MAX_XML_DOWNLOADS = 5`.
- `fetch`: `search(invoice.serie, SEARCH_TOP)`; keep messages received on or after `invoice.fecha` minus 1 day, newest first. A file name matches when, uppercased, it contains `{serie}-` followed by `0*{int(numero)}` and then a non-digit or the end. Download matching `.xml` attachments (at most `MAX_XML_DOWNLOADS`, each capped by the XML limit), parse with `parse_invoice_xml`, keep the first that `matches_invoice`. Then the PDF from the same message: a matching `.pdf` name, else the message's only `.pdf`, capped by the PDF limit. Write both to `out_dir`, hash, return `OK` (`portal_key=None`). No matching XML → `NOT_FOUND_YET` (the chain retries on the inbox schedule); `AuthRequired` → `AUTH_REQUIRED`; `GraphTransient` or `httpx.HTTPError` → `TECHNICAL`; `GraphError` → `TECHNICAL`; a rejected XML is skipped (another may match), and if every candidate was rejected or mismatched the result is `XML_INVALID` or `XML_MISMATCH` (the last seen).
- Tests with a fake `Mailbox` (cite R-5.2, R-5.6, S11.9): XML and PDF found; padded `F001-00000123` matches numero `123`, `F001-1234` does not; a message older than the invoice skipped; no match → `not_found_yet`; a mismatching XML → `xml_mismatch`; an over-cap XML skipped; `AuthRequired` → `auth_required`; only the XML present → `OK` with one file (the chain's `required_docs` check decides).

Test: `uv run pytest -q tests/unit/test_fetchers_email_inbox.py`.

## Step 24: A mailbox that needs a sign-in waits `[worker:low]`

Files: `src/invoicebot/fetchers/chain.py`, `tests/unit/test_fetchers_chain.py`.

- `WAIT_REASONS = frozenset({NOT_FOUND_YET, AUTH_REQUIRED})`. `_decide_one` waits on any reason in it; `retry_delay` counts attempts with a reason in it.
- Tests (cite D65, D100): `auth_required` then `not_found_yet` use schedule entries 0 and 1; after the schedule the chain moves on; existing cases pass.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py`.

## Step 25: Admin alerts move to `outgoing.py` `[worker:low]`

Files: `src/invoicebot/pipeline/outgoing.py`, `src/invoicebot/pipeline/deliver.py`.

- Move `_notify_admins` and `_notify_admin` from `deliver.py` to `outgoing.py` as public `queue_admin_alert(conn, profiles, rule_id, text)` (the single-admin helper stays private). `deliver.py` imports it. No behavior change.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`.

## Step 26: Fetch runs per flow: registries, schedules, waiting texts, sign-in alert `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_fetch.py`.

- `make_fetch_handler(pool, cfg, directory, registries: Mapping[str, Mapping[str, Fetcher]], data_dir)`: the flow's registry is `registries.get(flow_name, {})`.
- Schedules: `email_inbox` uses `flow.email_inbox.retry_schedule`; `portal_registry` its portal's; others `DEFAULT_RETRY_SCHEDULE`.
- `_fetch_invoice` fills `document_url` from `reading.document_url`.
- Waiting text: sent on a fetcher's first wait (its first attempt with a reason in `WAIT_REASONS` that has a delay), per fetcher: `FETCH_WAITING` for the portal, new `FETCH_WAITING_MAIL = "⏳ Comprobante #{id}: esperando correo del comercio. Lo vuelvo a buscar más tarde."` for `email_inbox`, `FETCH_WAITING` otherwise.
- On a fetcher's first `auth_required` attempt, in the same transaction, `queue_admin_alert` with BR-D01's admin text for the inbox's account (`provider="microsoft"`).
- Tests (cite S7.1, S11.9, D100): a flow with no registry entry for a fetcher skips it; the inbox's second `not_found_yet` is scheduled 1 h out (database clock); "esperando correo" is sent once even after a portal already sent its own waiting text; an `auth_required` attempt alerts each admin once and schedules a retry; a QR `document_url` reaches the fetcher.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`.

## Step 27: Register `qr_url` and `email_inbox` in the app `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_health.py`.

- A second `httpx.AsyncClient(transport=GuardedTransport(httpx.AsyncHTTPTransport(), SystemResolver()), follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS)` for `qr_url`, opened in the lifespan next to the existing one.
- Per flow: `qr_url` → `QrUrlFetcher` (hosts from `allowed_hosts(directory)`), `portal_registry` → the `PortalFetcher` (one instance shared), `email_inbox` → `EmailInboxFetcher(GraphMailbox(...))` only when the flow's inbox is `graph` and `AGE_IDENTITY_PATH` is set (the provider from `build_token_provider(settings, cfg, account, scopes_for(cfg, account))`); otherwise log a warning naming the flow and leave it out.
- Test: the app starts with `example.yaml` and a fake secrets store (the existing startup test) and logs no error; add an assertion that a flow without `AGE_IDENTITY_PATH` logs the inbox warning.

Test: `uv run pytest -q tests/unit/test_health.py`.

## Step 28: Flows declare what a sender may upload `[worker:low]`

Files: `src/invoicebot/config/models.py`, `config/config.yaml`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Flow.manual_uploads: list[Literal["pdf", "xml"]] = []`. Flow B `[pdf, xml]`, flow A `[xml, pdf]` (R-1.5, D66).
- Tests (cite R-1.5): both YAMLs load with those lists; `xlsx` is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 29: Intake links an upload to the stuck invoice `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `src/invoicebot/store/messages.py`, `tests/integration/test_intake.py`.

- `OPEN_STATES = ("MANUAL_REQUIRED",)` with a `shortcut:` naming D101; both SQL queries of `find_open_invoice_for_attachment` use it as a parameter instead of the literal list.
- In `handle_inbound`, a message with a `pdf` or `xml` attachment and no photo or button: `find_open_invoice_for_attachment(conn, profile_id, reply_to)`; when found, `link_message_to_invoice` and `enqueue(conn, invoice_id, ATTACH_STEP)` (`ATTACH_STEP = "attach_upload"` in `pipeline/steps.py`, a fourth file), status `stored` with the invoice id. Not found: as today.
- Tests (cite R-1.5): an XML from the same profile while the newest invoice is `MANUAL_REQUIRED` is linked and queues `attach_upload`; a quoted older `MANUAL_REQUIRED` invoice wins over the newest; another profile's invoice is never matched; with no stuck invoice nothing is queued.

Test: `uv run pytest -q tests/integration/test_intake.py`.

## Step 30: Attach the sender's files (S7.5) `[worker]`

Files: `src/invoicebot/pipeline/uploads.py` (new), `src/invoicebot/store/invoices.py`, `tests/integration/test_pipeline_uploads.py` (new).

- `store/invoices.py`: `list_unattached_uploads(conn, invoice_id) -> list[Attachment]`: the `pdf`/`xml` attachments of the invoice's linked messages whose `media_ref` is in no `files.remote_ref` of the invoice, oldest first. The deliver query that picks a file of a kind orders by `id DESC` (newest wins, D101).
- `make_attach_handler(pool, cfg, client, data_dir, limits)`: acts only on `MANUAL_REQUIRED` (else returns). For each unattached upload: a kind not in the flow's `manual_uploads` is skipped (logged at debug); download with `download_media`; an XML is parsed (`parse_invoice_xml`) and matched (`matches_invoice` against the stored reading): `XmlRejected` → file removed, reply `UPLOAD_XML_INVALID = "El XML que enviaste no es válido; no lo guardé."` (rule BR-F09); no match → removed, reply `UPLOAD_XML_MISMATCH = "El XML que enviaste no corresponde a {serie}-{numero}; no lo guardé."` (rule BR-F06). Kept files are stored with `add_file` (`remote_ref` = the media ref). Then, in one transaction: the flow's `required_docs` all present → `set_state(DELIVERING, expected=MANUAL_REQUIRED)` and `enqueue(DELIVER_STEP)`; else BR-D09 with `received_desc` (`docs_desc` of the kinds stored now) and `missing_desc`. `MediaRejected` → the upload is skipped with BR-S04's text if that is what `handlers._reject` sends for a too-big file, else logged.
- Tests (cite R-1.5, R-5.6, BR-D09, D66): PDF then XML in two messages → BR-D09 after the first ("Aún falta el XML"), `DELIVERING` and a deliver job after the second; a mismatching XML → BR-F06 reply, nothing stored, still `MANUAL_REQUIRED`; an XML with a DOCTYPE → BR-F09 reply; an `xlsx` attachment ignored; the same message handled twice stores its file once; an invoice no longer `MANUAL_REQUIRED` is left alone; two XML uploads → delivery reads the newer.

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py tests/integration/test_pipeline_deliver.py`.

## Step 31: Register the attach handler `[worker:low]`

Files: `src/invoicebot/app.py`.

- `handlers[ATTACH_STEP] = make_attach_handler(pool, cfg, client, settings.data_dir, cfg.limits.media)`.

Test: `uv run pytest -q tests/unit/test_health.py`.

## Step 32: A stuck invoice through the fake channel `[worker]`

Files: `tests/integration/test_conversation_fake.py`.

- One test (cite R-5.5, R-1.5, S7.4): a flow B invoice whose store is `portal: manual`, with no QR link and an inbox fake that never finds it (a registry injected through the fixture), runs the chain to `MANUAL_REQUIRED`; the BR-F07 text lists BR-F11's bullet, the "Correo" not-found bullet and BR-F01's bullet. The sender then sends a PDF and a matching XML through the fake channel; the invoice reaches `DELIVERING`.

Test: `uv run pytest -q tests/integration/test_conversation_fake.py`.
