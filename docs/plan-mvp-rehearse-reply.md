# Plan: MVP phase 9l · rehearse-reply

Roadmap: `docs/plan-mvp.md` row 9l (D165 amended). The console channel cannot quote a message, so a PDF or XML sent
while the profile has more than one `MANUAL_REQUIRED` invoice always gets BR-D14 ("reply to the right photo"). The live
host has six such invoices (#8–#13), so the flow A and flow B manual-upload rehearsals are blocked.

`rehearse send --reply-to <id>` quotes the console reply `#<id>` that `rehearse replies` printed. The console transport
already returns `console-<outbox id>` as the channel message id, and intake already resolves a quoted bot message
through the outbox (D102). So this phase only fills `reply_to_external_id`; the pipeline and the store do not change.

**Needs from the owner first:** nothing for steps 1 to 3. Step 4 uses the live host and the owner's open invoices.

## Step 1: D165 amended and roadmap row 9l `[inline: design decision and spec wording]`

1. `specs/05-open-questions.md`, row D165: add one sentence: `rehearse send --reply-to <id>` quotes the console reply
   `#<id>` (its channel message id is `console-<id>`), so a manual upload picks its invoice as a WhatsApp reply does
   (D102); a reply still `pending` has no channel message id yet and the quote does not resolve.
2. `docs/plan-mvp.md`: add row 9l `rehearse-reply` (plan: `docs/plan-mvp-rehearse-reply.md`, D165) after 9k, before
   9d2.

Test: `grep -n "reply-to" specs/05-open-questions.md && grep -n "9l" docs/plan-mvp.md` shows both lines.

## Step 2: `--reply-to` on `rehearse send` `[worker]`

Files: `src/invoicebot/channels/console.py`, `src/invoicebot/rehearse.py`, `src/invoicebot/cli.py`,
`tests/unit/test_rehearse.py`.

1. `channels/console.py`: add `def console_message_id(outbox_id: int) -> str` returning `f"console-{outbox_id}"`, with
   a one-line docstring citing D165. `console_transport()`'s `deliver` returns `console_message_id(row.id)` (one home
   for the format).
2. `rehearse.py`: `RehearseSend` gains `reply_to: int | None = None` (document it in the class docstring: the console
   reply id to quote, D102, D165). In `rehearse_send`, build the `InboundMessage` with
   `reply_to_external_id=None if request.reply_to is None else console_message_id(request.reply_to)`.
3. `cli.py`: `send.add_argument("--reply-to", type=int, help="console reply id to quote (the #<id> rehearse replies printed)")`,
   and pass `reply_to=args.reply_to` into `rehearse.RehearseSend`. `--reply-to` alone (no `--name`, `--text` or
   `--button`) keeps the existing "needs one of" error. A value below 1 is a `parser.error("--reply-to must be a
positive reply id")`.
4. `tests/unit/test_rehearse.py`, each docstring starting with `D165`:
   - `console_message_id(42)` returns `"console-42"`.
   - `RehearseSend(name="a.pdf", reply_to=7)` keeps `reply_to == 7`; `RehearseSend(name="a.pdf")` has
     `reply_to is None`.

Test: `uv run pytest -q tests/unit/test_rehearse.py tests/unit/test_channels_console.py`, and `uv run ruff check` plus
`uv run mypy src` on the changed files.

## Step 3: The quote picks the invoice, end to end `[worker]`

Files: `tests/integration/test_cli_rehearse.py`, `tests/integration/test_cli.py` (only if the CLI's argument
errors are tested there today; otherwise leave it).

Add to `test_cli_rehearse.py`, with the file's existing config and settings helpers and a fresh profile per test. Each
docstring starts with `D102, D165`. Arrange, in both tests: two console invoices for the profile, each with
`create_invoice(conn, profile_id, "console")`, `set_state(conn, invoice_id, "MANUAL_REQUIRED", rule_id="BR-F07")` and
one bot message `queue_message(conn, Addressee("console", profile_id), "Synthetic bot message.", invoice_id=...,
rule_id="BR-F07")`; then deliver them with `OutboxSender(pool, {"console": console_transport()}, ...).send_pending()`
built as the app builds it, so each row is `sent` with channel message id `console-<id>`. Keep the outbox id of the
first invoice's message as `quoted`.

- **Quoted upload links to the quoted invoice:** `rehearse_send(..., RehearseSend(name="doc.pdf", reply_to=quoted),
io.BytesIO(b"%PDF-1.4 synthetic"))` returns `invoice_id ==` the first invoice; its `messages` row has that
  `invoice_id`; the first invoice has one `attach_upload` job; no BR-D14 row was queued for the profile; the inbox
  file still exists (the attach job will consume it).
- **Unquoted upload asks which invoice:** the same send without `reply_to` returns `invoice_id is None`; one BR-D14
  row is queued for the profile; neither invoice has an `attach_upload` job; the inbox file is gone.

If `test_cli.py` covers `rehearse send` argument errors, add there: `rehearse send --reply-to 0 --name a.pdf` exits 2
with "--reply-to must be a positive reply id".

Test: `uv run pytest -q tests/integration/test_cli_rehearse.py` (needs the compose Postgres). Both new tests fail on
`main` (the first gets BR-D14), which shows they test the quote.

## Step 4: Live: rehearse a manual upload with a quote `[inline: the owner's live host and real invoices]`

1. `docs/runbook.md` section 9: after the button step, add "Quote a reply: `rehearse send --reply-to <id> --name
f.pdf < f.pdf`, with the `#<id>` of the bot's message about that invoice; wait until `rehearse replies` shows it
   `sent`". Replace the "Known limit" paragraph with that one sentence on `pending` replies.
2. Deploy the image (`./deploy/deploy-live.sh`), check `/health`.
3. With #8–#13 open: `rehearse replies` to find one invoice's BR-F07 message id, then send its PDF or XML with
   `--reply-to`. Expect the attach reply (BR-D09 or the delivering path), not BR-D14.

Test: the live reply for the quoted send is not BR-D14, and `rehearse replies` shows the invoice moving on.
