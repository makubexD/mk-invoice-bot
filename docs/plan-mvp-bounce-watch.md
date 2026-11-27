# Plan: MVP phase 9z11 · bounce-watch

Roadmap: `docs/plan-mvp.md`, row 9z11. Branch `feat/bounce-watch`, from `main`.

What the live runs showed (#45 and the open-links, review-ui and forget runs): the mail server accepting a mail does
not mean it arrived. Graph mail from the personal account bounced at the work tenant with `550 5.7.501`, minutes after
BR-D10 had said Outlook accepted it, and the bot never learned of it. SMTP (D205) has not bounced so far, but
Outlook.com picks the outbound IP per mail (`specs/spikes/open-links.md`), so a bounce can still come back. Owner
decisions (2026-11-27, `mvp-roadmap-progress` memory): a bounce watch, yes; the owner is told the truth; an Outlook
draft is the last fallback; Gmail is out of scope.

Decision this plan proposes (step 2), **D207 The bot watches for a bounce and leaves a draft**:

- **Each `smtp` mail gets its own `Message-ID`** (`<…@{sender domain}>`). The batch records it in `batches.email_ref`
  when it goes `sent`.
- **A `bounce_check` job reads the sending account's inbox** first `limits.bounce_check_after_s` (default 120) after
  the send, then every `limits.bounce_check_every_s` (default 300), until `limits.bounce_watch_s` (default 1800) after
  `batches.sent_at`. Outlook.com rewrites the `Message-ID` at submission and keeps the bot's id as
  `X-Microsoft-Original-Message-ID` (`specs/spikes/bounce-watch.md`). A bounce is a delivery report (an NDR) received
  after the send whose `message/rfc822` part carries the bot's id in that header. Subject and time alone never
  decide a match. From the report the bot keeps only the enhanced status
  code (e.g. `5.7.501`), never the server's text or an address (R-9.16).
- **On a bounce** the batch goes `bounced`; the invoice stays `DONE` (filed is filed), the folder loses its `_Sent`
  name (R-7.11), and the bot makes a draft in the account's Drafts folder from the mail's copy in Sent Items, found
  by the report's `In-Reply-To` (Outlook's id).
  - With the draft: BR-D26 `⚠️ El correo de #{id} {serie}-{numero} a {to} rebotó ({status}). Te dejé un borrador
igual en Outlook: ábrelo, toca ✏️ y envíalo. Cuando lo envíes, toca [Ya lo envié].` with [✉️ Abrir borrador] (the
    draft's `webLink`) and [Ya lo envié].
  - Without one: BR-D27, the same first sentence plus `No pude dejarte el borrador: reenvíalo desde Enviados en
Outlook y toca [Ya lo envié].`, with [Ya lo envié].
- **[Ya lo envié] on a bounced batch** sets the batch `sent` again, restores the folder name and answers BR-D08.
- **Scope:** an account that sends through `smtp` also signs in with `Mail.ReadWrite`: reading the inbox needs
  `Mail.Read`, making the draft needs `Mail.ReadWrite`. The live personal account needs one new sign-in. A watch
  whose token lacks the scope, or whose Graph call fails for good, logs `bounce watch blocked` with the status and
  stops. It never moves the invoice.
- Only the `smtp` mode is watched: Graph `sendMail` gives back no `Message-ID`, and D205 took the live flow off it.

Out of scope: retrying the send on its own (a 5.7.501 bounce depends on the IP, so the same route can fail again);
the Junk folder (unless step 1 finds reports landing there); `graph` and `inbox_rule` mail; telling an admin.

## Step 1: spike: the report's shape and the draft from Sent `[inline: live mailbox and the owner's sign-in]`

Files: `specs/spikes/bounce-watch.md` (new).

Mine on the live host (the owner's standing grant for host steps), with a throwaway token cache as in `specs/spikes/smtp-probe.md`
(`/data/tokens/bounce-probe.msal.age`, device code for `Mail.ReadWrite SMTP.Send` on the personal account; the owner
enters the code). Read only, except one probe mail and one draft:

1. Send one SMTP mail with a set `Message-ID` to a made-up address at a domain that refuses unknown users
   (`no-such-user-<random hex>@outlook.com`), so a real NDR comes back.
2. List the inbox for the report: the sender, the subject prefix, the folder it lands in, how soon it came, and
   where the original `Message-ID` appears in `GET /me/messages/{id}/$value` (a `message/delivery-status` part's
   `Status:`, a `text/rfc822-headers` or `message/rfc822` part, `In-Reply-To`/`References`). Check #45's Graph
   bounce for the same shape.
3. Find the probe's copy in Sent Items by `internetMessageId`, `GET` its `$value`, `POST /me/messages` with it
   (base64 MIME, `Content-Type: text/plain`), and record the created draft's `isDraft`, `webLink` and attachments.
   The owner opens the `webLink` on the phone and says whether it opens the draft ready to send. Delete the draft.
4. Record the NDR's MIME skeleton in the spike sheet: the part tree (content types), the header names that carry
   the original `Message-ID`, and the `Status:`/`Action:` lines. No address, host name or IP goes in; step 9 builds
   its test report from this skeleton.
5. Remove `/data/tokens/bounce-probe.msal.age` and any probe files in the container.

The spike sheet records the answers with no addresses. **Gate:** if the report does not carry the original
`Message-ID`, or the MIME draft does not open ready to send, stop and tell the owner. Steps 9 and 10 assume both.

Test: `npx prettier --check specs/spikes/bounce-watch.md` and `grep -cE '@|([0-9]{1,3}\.){3}[0-9]{1,3}' specs/spikes/bounce-watch.md` prints `0`

## Step 2: D207, the rule texts and the roadmap `[inline: spec text and user-facing copy]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: row D207 after D206 with the decision above, adjusted to step 1's findings, marked
  "(9z11 plan, proposed 2026-11-27, owner)".
- `specs/06-business-rules.md`: BR-D10's outcome adds "an `smtp` mail is then watched for a bounce (D207)"; new rows
  BR-D26 and BR-D27 with the texts above; BR-D08's condition adds "or on BR-D26/BR-D27".
- `specs/02-requirements.md`: R-7.7's `smtp` item says the mail carries its own `Message-ID` and is watched for
  `limits.bounce_watch_s` (D207); R-7.9 adds `Mail.ReadWrite` for an account an `smtp` mailer uses.
- `docs/plan-mvp.md`: row 9z11 names this plan and D207.

Test: `npx prettier --check specs/05-open-questions.md specs/06-business-rules.md specs/02-requirements.md docs/plan-mvp.md`

## Step 3: the `bounced` batch state `[worker]`

Files: `migrations/versions/0019_bounced_batch.py` (new), `tests/integration/test_schema.py`.

- Revision `0019`, down `0018`, in 0018's style: drop `batches_state_check` and add it back with `'bounced'` added
  to 0018's list. Downgrade maps `bounced` rows to `sent` first (what the previous image would call them), then
  restores 0018's check.
- Test, next to `test_batch_accepts_review_and_rejects_paused`: a batch accepts `bounced`.

Test: `uv run pytest -q tests/integration/test_schema.py`

## Step 4: the watch's limits `[worker:low]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Limits.bounce_check_after_s: int = Field(default=120, gt=0)`, `bounce_check_every_s: int = Field(default=300,
gt=0)`, `bounce_watch_s: int = Field(default=1800, gt=0)`, each with a comment citing D207; `example.yaml` lists
  them with the defaults.
- Tests: the defaults; `0` is refused for each.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 5: an `smtp` account signs in with `Mail.ReadWrite` `[worker:low]`

Files: `src/invoicebot/auth/providers.py`, `tests/integration/test_cli_auth.py`.

- `MAIL_READWRITE_SCOPE = "Mail.ReadWrite"` (comment: an `smtp` account reads its bounces and makes the draft,
  D207); `scopes_for` appends it when `account in smtp_send_accounts(cfg)`, once, after the existing ones.
- Tests next to `test_scopes_for_personal_cover_files_and_mail_send`: an account with an `smtp` mailer gets
  `Mail.ReadWrite`; one with only a `graph` mailer or only OneDrive does not; an `smtp` account that also reads its
  inbox gets `Mail.Read` and `Mail.ReadWrite` once each.

Test: `uv run pytest -q tests/integration/test_cli_auth.py`

## Step 6: the SMTP mail carries its own `Message-ID` `[worker]`

Files: `src/invoicebot/sinks/eml.py`, `src/invoicebot/sinks/mailer_smtp.py`, `src/invoicebot/sinks/mailer.py`,
`tests/unit/test_sinks_mailer_smtp.py`.

- `build_eml(..., message_id: str | None = None)`: writes the `Message-ID` header when given (CR/LF refused, as for
  the other headers).
- `SmtpMailer.send` makes `email.utils.make_msgid(domain=<the sender's domain>)`, passes it to `build_eml` and
  returns it; its docstring says so.
- `mailer.py`: `Mailer.send` returns `str | None` ("the `Message-ID` when the mailer sets one"); `MailerSend.deliver`
  returns what `send` returns; `InboxRule.deliver` still returns `None`. `GraphMailer.send` stays as it is
  (`None`).
- Tests: the sent bytes carry the returned `Message-ID` once, under the sender's domain; two sends get different ids;
  a subject with a newline is still refused.

Test: `uv run pytest -q tests/unit/test_sinks_mailer_smtp.py tests/unit/test_sinks_eml.py tests/unit/test_sinks_mailer.py`

## Step 7: one home for "the database clock plus N seconds" `[worker:low]`

Files: `src/invoicebot/queue/jobs.py`, `src/invoicebot/pipeline/validate.py`, `tests/integration/test_jobs.py`.

- Move `validate._now_plus` to `queue/jobs.py` unchanged (`hours`, `minutes`, `seconds`) as `db_now_plus`, docstring "the database clock, never Python's". `validate` imports
  it. Steps 8 and 10 are its second and third callers.
- Test: `db_now_plus(conn, seconds=60)` is 60 s after the transaction's `now()`.

Test: `uv run pytest -q tests/integration/test_jobs.py tests/integration/test_pipeline_validate.py`

## Step 8: deliver records the `Message-ID` and schedules the watch `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/steps.py`, `src/invoicebot/store/flow_b.py`,
`tests/integration/test_pipeline_deliver.py`.

- `steps.py`: `BOUNCE_CHECK_STEP = "bounce_check"` with a one-line comment (D207).
- `flow_b.py`: `set_batch_email_ref(conn, batch_id, ref)` and `load_sent_batch(conn, invoice_id) ->
SentBatch | None` (frozen dataclass `id`, `state`, `email_ref`, `sent_at`) for the invoice's batch.
- `deliver._automatic`: keep `mode.deliver`'s result. In the transaction that sets the batch `sent`, when the result
  is a `str`, also `set_batch_email_ref` and `enqueue(conn, invoice_id, BOUNCE_CHECK_STEP, await db_now_plus(conn,
seconds=cfg.limits.bounce_check_after_s))`. The docstring says so (D207).
- Tests: a `smtp`-like mode returning `"<m1@example.com>"` leaves the batch `sent` with that `email_ref` and one
  `bounce_check` job about 120 s ahead; a mode returning `None` enqueues no `bounce_check`.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 9: read the report and make the draft `[worker]`

Files: `src/invoicebot/sinks/mail_bounce.py` (new), `tests/unit/test_sinks_mail_bounce.py` (new).

Module docstring: finds the delivery report of one sent mail in a Microsoft inbox and turns the mail's Sent copy
into a draft (D207). It uses plain `httpx` over Graph like `fetchers/graph_mail.py` (`GRAPH_BASE_URL`,
`auth_headers`, `raise_graph_error`, the `_GRAPH_ID` check before any id goes into a path). Errors carry the status
only (R-9.16).

- `Bounce` (frozen dataclass): `status: str` and `sent_id: str`, the id Outlook gave the mail.
- `MESSAGE_ID = re.compile(r"^<[^<>'\s]{1,250}>$")`: the only shape an id may have before it goes into an OData
  filter.
- `parse_bounce(raw: bytes, message_id: str) -> Bounce | None` (pure, the shape in `specs/spikes/bounce-watch.md`):
  parse with `email.parser.BytesParser(policy=email.policy.default)`. Return `None` unless the report has a
  `message/rfc822` part whose headers have `X-Microsoft-Original-Message-ID` equal to `message_id` (whitespace
  stripped, case-sensitive). `status` is the first `Status:` in the `message/delivery-status` part that matches
  `^[245]\.\d{1,3}\.\d{1,3}$`, else `"?"`. `sent_id` is the report's `In-Reply-To`, else the `message/rfc822`
  part's `Message-ID`. Return `None` when neither matches `MESSAGE_ID`.
- `async find_bounce(http, tokens, *, message_id, since) -> Bounce | None`: list `/me/mailFolders/inbox/messages`
  with `$filter=receivedDateTime ge {since}`, `$select=id,subject`, `$top=50`. For each message, read `$value`
  streamed and capped at `MAX_REPORT_BYTES = 2 MiB` (a bigger one is skipped and logged), then return the first
  `parse_bounce` that is not `None`.
- `async draft_from_sent(http, tokens, *, sent_id) -> str`: `sent_id` must match `MESSAGE_ID`, else `ValueError`.
  Find the Sent Items message with `$filter=internetMessageId eq '{sent_id}'`. Read its `$value`, capped at
  `MAX_DRAFT_BYTES = 3 MiB`. `POST /me/messages` with the base64 MIME as `text/plain` (201, `isDraft`). Return the
  created message's `webLink`, which must be `https` on an `outlook.live.com` or `outlook.office.com` host. Raises
  `DraftUnavailable` when the copy is missing or too big, or the link fails the check.
- Tests with `httpx.MockTransport` and a synthetic report in the spike's shape (built in the test with
  `email.message`, `example.com` addresses):
  - It matches `<fixture-1@example.com>` and returns the status and `sent_id`.
  - Another id gives `None`, and so does a report whose only match is in its text part.
  - A missing `Status:` gives `"?"`.
  - A report over the cap is skipped, and a 403 raises `GraphError` with the status only.
  - A draft round trip returns the link.
  - Each of these raises: a missing Sent copy, a too-big copy, a `javascript:` or other-host `webLink`, and a quote
    inside `sent_id`.

Test: `uv run pytest -q tests/unit/test_sinks_mail_bounce.py`

## Step 10: the `bounce_check` job `[worker:high: reasoning: the order of the external reads, the draft and the one transaction, and what ends the watch]`

Files: `src/invoicebot/pipeline/bounce_watch.py` (new), `src/invoicebot/rules/messages_es.py`,
`src/invoicebot/app.py`, `tests/integration/test_pipeline_bounce_watch.py` (new).

- `messages_es.py`: `BOUNCED_DRAFT` (BR-D26) and `BOUNCED_NO_DRAFT` (BR-D27) as in step 2, with the button titles
  `BUTTON_OPEN_DRAFT = "✉️ Abrir borrador"`; [Ya lo envié] reuses `BUTTON_SENT`.
- `make_bounce_check_handler(pool, cfg, readers)`, where `readers` maps an account to its `find_bounce` and
  `draft_from_sent` bound to that account's token source and the shared HTTP client (a small `Protocol` with those
  two methods, built in `app.py` for each `smtp` account).
  1. Short transaction: `load_sent_batch`. Stop unless it is `sent` with an `email_ref`. Note the flow's `smtp`
     mailer account and `db now()`.
  2. No transaction: `find_bounce(message_id=email_ref, since=sent_at)`. `GraphError` (final) or `AuthRequired`:
     log WARNING `bounce watch blocked` with `invoice_id` and the status, and stop. A transient error propagates to
     the worker's retry.
  3. No report: when `now() < sent_at + bounce_watch_s`, enqueue itself at `db_now_plus(seconds=bounce_check_every_s)`;
     otherwise log INFO `bounce watch clear`.
  4. A report: try `draft_from_sent(sent_id=bounce.sent_id)`. `DraftUnavailable`, `GraphError` or `AuthRequired` means no draft, logged
     WARNING without the link. Then one transaction locks the invoice, re-reads the batch (stop if it is no longer
     `sent`), sets it `bounced`, merges `{"bounce": {"status": bounce.status, "draft": bool}}` into flow data, enqueues
     `FOLDER_STATUS_STEP`, and queues BR-D26 (link button with the `webLink`, plus [Ya lo envié]) or BR-D27 ([Ya lo
     envié]) to the invoice's origin addressee, `{to}` being the mailer's first `to` and `{status}` the code (`?`
     shown as `sin código`).
  - A crash after the draft and before the commit makes a second draft on the retry. That is accepted and the
    docstring says so.
- `app.py`: register `BOUNCE_CHECK_STEP` in the handler mapping, next to `FETCH_PROGRESS_STEP`.
- Tests (fake readers, real Postgres): no report before the deadline re-enqueues once at about +300 s; no report
  after it enqueues nothing; a report with a draft leaves the batch `bounced`, the invoice `DONE`, one BR-D26 with
  the link and [Ya lo envié], and one `folder_status` job; a report whose draft fails queues BR-D27; a batch already
  `bounced` or back to `sent` by hand does nothing; `AuthRequired` in `find_bounce` logs `bounce watch blocked` and
  leaves the batch and the invoice as they were.

Test: `uv run pytest -q tests/integration/test_pipeline_bounce_watch.py`

## Step 11: [Ya lo envié] after a bounce `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/folder_status.py`,
`tests/integration/test_pipeline_actions.py`.

- `actions._sent`: a `bounced` batch on a `DONE` invoice is set `sent`, enqueues `FOLDER_STATUS_STEP` and answers
  BR-D08. There is no state change, since the invoice is already `DONE`. The `prepared` path is as today; every
  other batch state still gets BR-S02. Docstring updated (D207).
- `folder_status.py`: no logic change; its docstring says a `bounced` batch is not sent, so the folder has no
  `_Sent` (D207).
- Tests: a press on a bounced batch leaves it `sent` with BR-D08 and one `folder_status` job, the invoice still
  `DONE`; a second press gets BR-S02; the prepared path is unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_pipeline_folder_status.py`

## Step 12: runbook, deploy and the live check `[inline: host commands and the owner's sign-in]`

Files: `docs/runbook.md`, `specs/spikes/bounce-watch.md`.

- `docs/runbook.md`: new §15 "When a mail bounces (D207)": what BR-D26/BR-D27 mean, the draft, [Ya lo envié], the
  three limits, the `bounce watch blocked` log line and the new sign-in.
- Host (mine): build and `./deploy/deploy-live.sh <tag>`. The owner runs `invoicebot auth-microsoft --account
personal` (`Mail.ReadWrite` is new). The owner sends one receipt in Telegram. The journal shows the
  `bounce_check` runs and `bounce watch clear` about 30 min after the send.
- The bounce path itself was proven live by step 1's probe and is covered by steps 9 and 10's tests against its report. A
  forced bounce through the pipeline would need a flow pointed at a bad address, so it is not run.
- Record the run under "Live run" in `specs/spikes/bounce-watch.md` (no addresses), committed via a PR.

Test: `npx prettier --check docs/runbook.md specs/spikes/bounce-watch.md`
