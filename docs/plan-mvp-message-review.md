# Plan: MVP phase 9u · message-review

Roadmap: `docs/plan-mvp.md`. The owner's first note after the 9s live run (`specs/spikes/guided-docs.md`,
2026-11-24): every message says what happened, what the bot does next and when, and offers a button for each sensible
answer. BR-F02 has no buttons. `Vuelvo a mirar el correo a las 05:25.` was unclear: say that the bot checks the inbox
for the merchant's PDF, files it on its own when it arrives, and what happens when it never does.

An audit of the sender-facing rows of spec 06 (the operations alerts belong to 9w `ops-alerts`) found:

- **Dead ends with a typed command where a button would do:** BR-F07 (chain end), BR-S03 (technical failure), BR-D03
  (mail not prepared), BR-S08 (parked), BR-S07 (not found).
- **BR-S03 is never sent.** A dead job moves its invoice to `FAILED` with rule BR-S03 (`queue/jobs.py`), but no code
  queues BR-S03 to the sender or `BR-S03_admin` to the admins: the invoice fails in silence.
- **Untrue or unclear next steps:** BR-F02_dont_know says `Ya avisé al administrador` also when BR-F02 came from
  BR-F13 or BR-F14, where no gap is raised (spec 06 BR-F02); BR-S06 tells the sender the admin reviews the link but
  the sender never hears the decision; BR-F13's `a las {next_at:%H:%M}` reads as today when the next look is
  tomorrow, and it does not say what happens when the mail never comes.
- **Review leftovers from 9s:** a command button runs any command (`/` prefix, security audit Low 4); the tests use
  `sy.ecomprobantes.pe`, a host from the owner's own ticket.

In scope: the 9u roadmap row. Out of scope: the smart portal guess (9v `portal-guess`); the ops alerts (9w
`ops-alerts`); learning per issuer which source works ([⏳] teaching "this issuer mails", D189 "later"); [Corregir]
picking one field by button (D189 "later"); WhatsApp live (9d2).

Needs from the owner: review of the texts in step 1 and of D190.

Decision this plan proposes (step 1):

- **D190 message review (9u).**
  - **A next step on every sender message.** Each sender-facing message either asks a question with buttons, or says
    what the bot does next. Where the next step is a command, the message also carries it as a button.
  - **Command buttons.** A button whose id starts with `/` runs that command as if typed (D189). Only `cancelar <id>`,
    `reprocesar <id>`, `pendientes` and `estado` (with or without an id) may arrive as a button; any other `/` id is
    logged and ignored. The id is built in one place, `channels/buttons.command_button_id(command, arg)`.
  - **Buttons added:** BR-F07 [🔄 Reintentar] [✖️ Anular] (`/reprocesar {id}`, `/cancelar {id}`); BR-S03 and BR-D03
    [🔄 Reintentar]; BR-S08 [📋 Pendientes] (`/pendientes`); BR-S07 [📋 Mis comprobantes] (`/estado`); BR-F02 [No sé]
    (answers like BR-F14's [No sé], `u<invoice_id>:dk`). The texts keep the typed command, because a WhatsApp
    template sent outside the 24 h window carries no buttons.
  - **BR-S03 is sent.** When a job dies (`MAX_ATTEMPTS` failures or `MAX_CRASHES` worker deaths), the same
    transaction that fails the invoice queues BR-S03 with [🔄 Reintentar] to the invoice's sender and `BR-S03_admin`
    to the admins. The queue calls a callback the app passes in (`on_dead`), so `queue/` still imports nothing from
    `pipeline/`.
  - **BR-F02's "no sé" says what is true.** While the invoice is `FETCHING_DOCS`: `BR-F02_dont_know` = `Entendido.
Sigo buscando el PDF en tu correo y te aviso cuando llegue.`; in any other state: `BR-F02_dont_know_manual` =
    `Entendido. Ya avisé al administrador para agregar este portal. Si consigues el PDF, envíamelo aquí.`
  - **BR-S06 closes the loop.** The sender's thanks becomes `Gracias. El administrador lo revisará y te aviso lo que
decida.` After [Aprobar] or [Rechazar], the proposer (when it is not the admin who pressed) hears
    `BR-S06_sender_approved`: `✅ El administrador aprobó el portal que enviaste para RUC {ruc_emisor}. Cuando quede
configurado, retomo tus comprobantes y te aviso.` or `BR-S06_sender_rejected`: `El administrador no aprobó el
portal que enviaste para RUC {ruc_emisor}. Si tienes el PDF, envíamelo aquí.`, on the proposer's
    `preferred_channel`.
  - **BR-F13 says when, and what if never.** `⏳ Comprobante #{id}: me falta {docs_desc} para archivarlo.` +
    `{searched_lines}` + `Reviso tu correo por si el comercio te lo envía; si llega, lo archivo solo y te aviso.` +
    `Próxima revisión: {next_desc}. Si no llega hasta {last_desc}, te aviso para que me lo envíes.` + `Si lo tienes,
me ayudas:` and the same three buttons. `{next_desc}` and `{last_desc}` come from `when_desc`: `hoy a las 05:15`,
    `mañana a las 09:00`, or `el 26/11 a las 09:00`, in the owner's timezone, "today" read from the database clock.
    The last look is the first wait's `run_after` plus the rest of the inbox fetcher's schedule.
  - **BR-F13_wait:** `Listo. Sigo revisando tu correo y te aviso cuando llegue el PDF.`

## Step 1: D190, spec texts and roadmap rows `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `docs/plan-mvp.md`,
`src/invoicebot/rules/messages_es.py`.

- `specs/05-open-questions.md`: add D190 as written above, status proposed (owner 2026-11-24).
- `specs/06-business-rules.md`: the chat-message cells of BR-F02 (`Si no, toca [No sé].` instead of `Si no, responde
"no sé".`, and the [No sé] button), BR-F07, BR-D03, BR-S03, BR-S07, BR-S08 (their buttons, texts unchanged), BR-S06
  (the sender's new thanks), and the "BR-S06's owner answers" note (the two sender notices); BR-F13 is changed in
  step 9. Name the `BR-F02_dont_know` and `BR-F02_dont_know_manual` texts in BR-F02's row.
- `docs/plan-mvp.md`: add rows 9u `message-review` (this plan, D190), 9v `portal-guess` and 9w `ops-alerts` (the
  owner's notes 2 and 3 in `specs/spikes/guided-docs.md`), in that order before 9t.
- `src/invoicebot/rules/messages_es.py`: the new BR-F02, BR-S06 and BR-F13_wait texts; new button titles
  `BUTTON_RETRY = "🔄 Reintentar"`, `BUTTON_PENDING = "📋 Pendientes"`, `BUTTON_MY_INVOICES = "📋 Mis comprobantes"`.

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/integration/test_pipeline_teach.py
tests/integration/test_pipeline_docs_help.py tests/integration/test_pipeline_portal_offer.py`

## Step 2: Synthetic portal hosts in the OCR tests `[worker:low]`

Files: `tests/unit/test_extract_ocr_parse.py`.

Replace every `sy.ecomprobantes.pe` with `portal.example.pe` and the path `/pebsa` with `/tienda`, keeping each test's
input and expected value consistent (for example `https://portal.example.pe/tienda`). No other change.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py` (all pass) and
`git grep -n ecomprobantes -- tests src` prints nothing.

## Step 3: `when_desc` for dates in messages `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `tests/unit/test_messages_es.py` (new).

Add `def when_desc(at: datetime, today: date) -> str` next to `state_desc`: `at` is already in the owner's timezone.
Same date as `today` → `hoy a las HH:MM`; the next day → `mañana a las HH:MM`; any other date →
`el DD/MM a las HH:MM`. The three formats are module constants (`WHEN_TODAY`, `WHEN_TOMORROW`, `WHEN_OTHER`).

Test cases (`tests/unit/test_messages_es.py`, docstrings start with `D190`):

- `at` 2026-11-24 05:15, `today` 2026-11-24 → `hoy a las 05:15`.
- `at` 2026-11-25 09:00, `today` 2026-11-24 → `mañana a las 09:00`.
- `at` 2026-11-26 09:00, `today` 2026-11-24 → `el 26/11 a las 09:00`.
- `at` 2027-01-01 00:05, `today` 2026-12-31 → `mañana a las 00:05` (year boundary).

Test: `uv run pytest -q tests/unit/test_messages_es.py`

## Step 4: Command button ids and their allowlist `[worker]`

Files: `src/invoicebot/channels/buttons.py`, `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`,
`tests/unit/test_channels_buttons.py`.

- Move `COMMAND_BUTTON_PREFIX = "/"` from `pipeline/commands.py` to `channels/buttons.py` and add
  `command_button_id(command: str, arg: str = "") -> str` returning `/<command>` or `/<command> <arg>`;
  `commands._list_choices` builds its ids with it. `intake.py` imports the prefix from its new home.
- Add `is_button_command(command: str, arg: str) -> bool` in `channels/buttons.py`: True only for `cancelar` or
  `reprocesar` with an all-digit `arg` of 1–9 digits, `pendientes` with an empty `arg`, and `estado` with an empty or
  all-digit `arg`. `intake._handle_press` runs a `/` press only when `is_button_command` is True; otherwise it logs
  `ignored command button` with `extra={"profile_id": ...}` and answers nothing.

Test cases (`tests/unit/test_channels_buttons.py`, docstrings start with `D190`):

- `command_button_id("reprocesar", "12")` → `/reprocesar 12`; `command_button_id("pendientes")` → `/pendientes`.
- `is_button_command` True for (`cancelar`, `12`), (`reprocesar`, `123456789`), (`pendientes`, ``), (`estado`, ``),
  (`estado`, `7`).
- False for (`cancelar`, ``), (`cancelar`, `12a`), (`reprocesar`, `1234567890`), (`pendientes`, `x`), (`ayuda`, ``),
  (`gap`, `ignorar 3`), (`portal`, `https://x.pe`).

Test: `uv run pytest -q tests/unit/test_channels_buttons.py tests/integration/test_commands.py
tests/integration/test_intake.py`

## Step 5: A rejected command button press is ignored `[worker]`

Files: `tests/integration/test_commands.py`.

Add, next to the bare `/reprocesar` button test, a test that a press with id `/gap ignorar 3` from an admin profile
queues no outbox row and changes no gap (docstring `D190: a command button outside the allowlist runs nothing.`), and
one that a press with id `/estado` lists the recent invoices like the typed command
(docstring `D190: an allowed command button runs like the typed command.`).

Test: `uv run pytest -q tests/integration/test_commands.py`

## Step 6: Buttons on BR-F07, BR-D03, BR-S08 and BR-S07 `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/park.py`,
`src/invoicebot/pipeline/commands.py`.

Build every button with `command_button_id` and the new titles:

- `fetch._exhausted` (BR-F07): [🔄 Reintentar] `/reprocesar {id}` and [✖️ Anular] (`BUTTON_VOID`) `/cancelar {id}`.
- `deliver` BR-D03 to the sender (`RULE_SEND_FAILED`, both places that queue it): [🔄 Reintentar] `/reprocesar {id}`.
- `park.py` BR-S08: [📋 Pendientes] `/pendientes`.
- `commands.resolve_target`'s BR-S07 reply: [📋 Mis comprobantes] `/estado`.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py tests/integration/test_pipeline_deliver.py
tests/integration/test_park.py tests/integration/test_commands.py` (update only the button expectations these four
messages now carry; step 7 adds the new cases).

## Step 7: Tests for the new buttons `[worker]`

Files: `tests/integration/test_pipeline_fetch.py`, `tests/integration/test_pipeline_deliver.py`,
`tests/integration/test_park.py`, `tests/integration/test_commands.py`.

One test each, docstrings starting with `D190`:

- BR-F07 at the chain's end carries `[{"id": "/reprocesar <id>", "title": "🔄 Reintentar"}, {"id": "/cancelar <id>",
"title": "✖️ Anular"}]`.
- BR-D03 to the sender carries `[{"id": "/reprocesar <id>", "title": "🔄 Reintentar"}]`.
- BR-S08 carries `[{"id": "/pendientes", "title": "📋 Pendientes"}]`.
- `/cancelar 999999` for an unknown invoice answers BR-S07 with `[{"id": "/estado", "title": "📋 Mis comprobantes"}]`.

Test: the same command as step 6.

## Step 8: BR-F13's look times reach its text `[worker]`

Files: `src/invoicebot/pipeline/manual_message.py`, `src/invoicebot/pipeline/fetch.py`.

- `docs_needed_text` takes `next_at`, `last_at` (both in the owner's timezone) and `today: date`, and passes
  `next_desc=when_desc(next_at, today)` and `last_desc=when_desc(last_at, today)` to `MESSAGES["BR-F13"].format`
  next to the existing `next_at` (the current text ignores the extra keys).
- In `fetch.py`, the first inbox wait computes `last_at` = its `run_after` plus the sum of the inbox fetcher's
  schedule entries after the first (`schedule[1:]`), and `today` = the database clock's `now()` as a date in
  `cfg.owner.timezone`; thread the schedule to `_wait_notice` with the fewest new parameters (lint caps arguments at
  6: group them in `_Inputs` if needed).

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/integration/test_pipeline_fetch.py
tests/integration/test_pipeline_docs_help.py` (all still pass; the text is unchanged).

## Step 9: BR-F13's new text `[worker]`

Files: `specs/06-business-rules.md`, `src/invoicebot/rules/messages_es.py`, `tests/unit/test_manual_message.py`,
`tests/integration/test_pipeline_fetch.py`.

- BR-F13's row and message become D190's text (`{next_desc}`, `{last_desc}`; drop `{next_at:%H:%M}`), and
  `docs_needed_text` stops passing `next_at` to `format`.
- Test cases (docstrings start with `D190, BR-F13`): `next_at` 2026-11-24 05:15 and `last_at` 2026-11-25 12:20 with
  `today` 2026-11-24 → the text contains `Próxima revisión: hoy a las 05:15.` and `Si no llega hasta mañana a las
12:20`; with `next_at` the next day → `Próxima revisión: mañana a las …`. In `test_pipeline_fetch.py`, the first
  inbox wait with schedule `5m, 1h, 6h, 24h` names a last look 31 h 5 m after the wait started, as `when_desc`
  renders it.

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/integration/test_pipeline_fetch.py
tests/unit/test_rules_parity.py`

## Step 10: BR-F02's [No sé] button `[worker]`

Files: `src/invoicebot/pipeline/portal_offer.py`, `tests/integration/test_pipeline_portal_offer.py`.

- `_ask_br_f02` queues BR-F02 with one button, [No sé] (`BUTTON_DONT_KNOW`), id `u{invoice_id}:dk`.
- `handle_press` accepts `dk` when the sender's newest question (`NOT_QUESTIONS` ignored) is BR-F14 **or** BR-F02 for
  that invoice; `yes` and `no` still need BR-F14. A `dk` whose newest question is neither answers BR-S02 as today.

Test cases (docstrings start with `D190, BR-F02`):

- `ask_portal` with no URL queues BR-F02 with `[{"id": "u<id>:dk", "title": "No sé"}]`.
- [No sé] after BR-F02 answers `BR-F02_dont_know` (step 11 changes which text).
- [Sí] after BR-F02 (a forged `u<id>:yes`) answers BR-S02 and teaches nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py`

## Step 11: BR-F02's "no sé" answer by state `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/teach.py`, `src/invoicebot/pipeline/portal_offer.py`,
`tests/integration/test_pipeline_portal_offer.py`.

- `messages_es.py`: `BR-F02_dont_know` and the new `BR-F02_dont_know_manual` get D190's texts.
- `teach.py`: one function `dont_know_reply(conn, addressee, invoice_id)` queues `BR-F02_dont_know` when the invoice
  is `FETCHING_DOCS` and `BR-F02_dont_know_manual` otherwise, both with `rule_id=RULE_DONT_KNOW` and the invoice id;
  `teach.answer`'s typed "no sé" and `portal_offer.handle_press`'s `dk` both call it.

Test cases (docstrings start with `D190, BR-F02`): [No sé] on a `FETCHING_DOCS` invoice answers the fetching text; on
a `MANUAL_REQUIRED` invoice the manual text.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py tests/integration/test_pipeline_teach.py`

## Step 12: The proposer hears the owner's decision `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

- `messages_es.py`: `BR-S06_sender_approved` and `BR-S06_sender_rejected` with D190's texts.
- `teach.handle_press`: after the decision, when `link.profile_id` differs from the pressing profile and that
  profile has a `preferred_channel`, queue the matching notice to it (`rule_id` = the key's name) in the same
  transaction. No notice when the proposer pressed, or has no preferred channel.

Test cases (docstrings start with `D190, BR-S06`): the assistant proposes and the owner approves → the assistant gets
`BR-S06_sender_approved`; rejects → `BR-S06_sender_rejected`; the owner proposes and approves → the owner gets only
`BR-S06_approved`.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py`

## Step 13: BR-S03's notice for a dead job `[worker]`

Files: `src/invoicebot/pipeline/failed.py` (new), `tests/integration/test_pipeline_failed.py` (new).

`async def notify_dead(conn, invoice_id: int, step: str, error: str, profiles: Mapping[str, Profile]) -> None` (module
docstring: "Tell the sender and the admins that an invoice's job died (BR-S03, D190)."):

- loads the invoice head; with no head, returns;
- queues `MESSAGES["BR-S03"].format(id=invoice_id)` to the invoice's sender (its channel and profile), `rule_id`
  `BR-S03`, the invoice id, and [🔄 Reintentar] `/reprocesar {id}`;
- queues `BR-S03_admin` (`step`, `id`, `error_code` = the first word of `error`, at most 40 characters) with
  `queue_admin_alert`.

Test cases (docstrings start with `D190, BR-S03`): an invoice of profile `asistente` gets both rows with those values;
an unknown invoice id queues nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_failed.py`

## Step 14: The queue calls `on_dead` when a job dies `[inline: one signature change spans six caller files]`

Files: `src/invoicebot/queue/jobs.py`, `src/invoicebot/queue/worker.py`, `src/invoicebot/app.py`,
`tests/integration/test_jobs.py`, `tests/integration/test_worker.py`, `tests/integration/test_intake.py`.

- `queue/jobs.py`: `OnDead = Callable[[AsyncConnection[Any], int, str, str], Awaitable[None]]` (connection, invoice
  id, step, error). `claim(..., on_dead)` and `fail(..., on_dead)` take it as a required keyword and call it right
  after `_fail_invoice`, in the same transaction.
- `queue/worker.py`: `Worker(pool, handlers, *, on_dead, ...)` passes it to both.
- `app.py`: `Worker(pool, handlers, on_dead=partial(notify_dead, profiles=cfg.profiles))` (wrap to match `OnDead`).
- `test_worker.py` and `test_intake.py` pass a no-op `on_dead` where they build a `Worker` or call `claim`.

Test cases (`tests/integration/test_jobs.py`, docstrings start with `D190, BR-S03`): the attempt that reaches
`MAX_ATTEMPTS` calls `on_dead` once with the invoice id, step and error; an earlier attempt does not; a job whose
worker died `MAX_CRASHES` times calls it once from `claim`; a rollback after `on_dead` raises leaves the job and the
invoice unchanged.

Test: `uv run pytest -q tests/integration/test_jobs.py tests/integration/test_worker.py tests/integration/test_intake.py`

## Step 15: Live check on Telegram `[inline: needs the owner's chat]`

Deploy the branch image to the live stack (`deploy/deploy-live.sh`) and, with the owner on Telegram, check: a PEBSA
ticket's BR-F13 names `Próxima revisión: hoy/mañana a las …` and a last look; [🔗] → BR-F02 with [No sé]; [No sé] →
the fetching text; `/cancelar 999999` → BR-S07 with [📋 Mis comprobantes], and that button lists the recent invoices.
Record the results in `specs/spikes/message-review.md`.

Test: `uv run pytest -q tests/unit/test_docs.py`
