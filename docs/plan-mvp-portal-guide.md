# Plan: MVP phase 9t4 · portal-guide

Roadmap: `docs/plan-mvp.md`, row 9t4. Branch `feat/portal-guide`, from `main` after 9t4a's merge (#60).

What 9t4a's live check left (2026-11-27, `specs/spikes/portal-link.md`): PEBSA's portal is named and linked, but the
bot cannot query it, because its form asks for an image captcha. The owner asked that the bot fill the form and show
the captcha in the chat.

What a read-only GET showed while drafting (2026-11-27): PEBSA runs a **cookieless** ASP.NET session (the session id
is in the URL path, `/(S(...))/`, and no cookie is set). The form posts back to itself with `__VIEWSTATE` (about 1.4
KB) and `__EVENTVALIDATION`. The captcha is `CaptchaImage.axd?guid=<uuid>` (MSCaptcha), whose answer the server keeps
by that guid. A new GET of the form makes a new captcha, so a resume must **replay the postback**: the session URL,
the form's own fields, the answer and the submit button, posted once. No cookie and no browser state is saved.

Owner decisions (2026-11-27, from 9t4a): save and resume (the helper saves the form and exits, the sender's answer
starts a new helper); D82 and the 300 s job lease stay; no local OCR (R-5.3's OCR part, S10.3, is deferred); flow B
must not change.

Decision this plan proposes (step 2):

- **D209 An image captcha is shown to the sender and the form is replayed** (reopens D99). (1) A portal whose
  `captcha` is a `PortalCaptcha` with `solver: [user]` and `status: active` runs the helper in _ask_ mode: it fills
  the fields, saves the captcha element as a PNG (at most 64 KiB) and the form's state (the page URL, the form's
  action URL, its fields in order minus the captcha input, the submit button's name and value; at most 256 KiB), and
  answers `captcha_asked`. (2) The bot stores the state in `portal_captchas`, moves the invoice to `WAITING_CAPTCHA`
  (BR-F18) and sends the PNG as a document whose caption asks for the letters. (3) The sender's next text, when that
  question is the newest, is the answer: 1 to 12 ASCII letters or digits, else BR-F20 asks again. The answer moves the
  invoice back to `FETCHING_DOCS` and the fetch job runs the helper in _resume_ mode: a fresh browser posts the saved
  fields plus the answer to the action URL (which must be `https` on the portal's own host), through the guard proxy,
  then reads the result page as a normal fetch does. (4) A result page with no file and no not-found marker that
  shows the captcha input again is `captcha_wrong` (PEBSA's not-found page shows the captcha input too, so the
  not-found marker is checked first): the bot asks again with a new captcha, up to `retrieval.captcha.ask_user_tries` (default 2) asks in
  the round, then the reason is `captcha_unsolvable` and the chain moves on. (5) No answer within
  `ask_user_timeout_s` (150 in the shipped configs: the step 1 spike measured PEBSA's captcha living 180–240 s) is
  `captcha_timeout`: BR-F05 is sent and the chain moves on. A late answer gets
  BR-F19. (6) One helper per fetch (D82) and the 300 s lease hold: no helper waits for a person.

Out of scope: local OCR (S10.3), reCAPTCHA and paid solvers (R-5.4 stays BR-F04), inline photos (the PNG goes as a
document), `lalucha-ddns` (its file is `todo`, `http`; it can follow PEBSA with a portal file only), the SUNAT API
(9t5), any flow B change.

## Step 1: live spike on PEBSA `[inline: live portal spike, reads the captcha by eye]`

Files: `specs/spikes/portal-guide.md` (new). The spike script lives in the session scratchpad, never in the repo.

On the host, with Playwright from the project's environment and the invoice from live #52 (PEBSA), not through the bot:

- (a) Open the base URL, reach the form, fill it with the invoice's values, save the captcha PNG and the form state as
  D209 (1) describes; close the browser. Read the PNG, then in a **new** browser replay the postback with the answer
  after 30 s. Record whether the result page is the invoice.
- (b) Repeat with a wait of 240 s, then 290 s, to learn whether the server's captcha outlives `ask_user_timeout_s`.
- (c) Repeat with a wrong answer: record the marker that shows it (the captcha input again, an error label).
- (d) On the result page: the download buttons (PDF, XML) and their selectors, whether a download is a postback, the
  not-found marker (a made-up número), and the value formats the form accepts (`txtCorrelativo` padding, `txtMonto`
  decimals, `txtFecha` `yy-mm-dd`).

Write the results in `specs/spikes/portal-guide.md` as a table, with no invoice values, RUCs or images. **Stop** and
alert the owner if (a) fails: replaying the postback is the whole design. If (b) shows the captcha expires sooner than
300 s, note the measured time; step 2 then sets the default `ask_user_timeout_s` below it.

Test: `npx prettier --check specs/spikes/portal-guide.md`.

## Step 2: D209, spec rows and rule ids `[inline: spec text and owner decisions]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/03-design.md`, `specs/04-tasks.md`,
`specs/02-requirements.md`, `docs/plan-mvp.md`.

- `05-open-questions.md`: add the D209 row after D208 (text above, with the spike's numbers); D99's row gets "reopened
  by D209 (9t4)".
- `06-business-rules.md`: BR-F05 reads "The sender does not answer an image captcha within `ask_user_timeout_s` | Send
  BR-F05, skip to the next fetcher (D209)". New rows: **BR-F18** "A portal asks for an image captcha (D209) | Send the
  image and ask for its letters; the invoice waits in `WAITING_CAPTCHA`", **BR-F19** "An answer arrives after the
  captcha expired | Say so; the chain already moved on", **BR-F20** "The answer is not 1–12 letters or digits, or the
  portal rejects it | Ask again, up to `ask_user_tries` asks in the round".
- `03-design.md`: the `WAITING_CAPTCHA` row cites D209; §6 drops `ask_user(image, prompt, timeout)` for "save and
  resume (D209)"; the captcha example's `solver` is `[user]`.
- `04-tasks.md`: S10.4 "built in 9t4 (D209)"; S10.3 "deferred (D209)".
- `02-requirements.md`: R-5.3 gets "(local OCR deferred, D209)".
- `docs/plan-mvp.md`: row 9t4 gets this plan's path.

Test: `npx prettier --check specs/*.md docs/plan-mvp.md`.

## Step 3: captcha config and reasons `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/fetchers/base.py`, `config/example.yaml`,
`tests/unit/test_config_models.py`.

- `models.py`: `Captcha` gets `ask_user_tries: int = Field(default=2, ge=1, le=5)`; `ask_user_timeout_s` gets
  `ge=60, le=900`. `PortalCaptcha.solver` becomes `tuple[Literal["user"], ...]` with at least one entry (a
  `min_length=1` field); the docstring cites D209.
- `base.py`: `FetchReason` gets `CAPTCHA_ASKED = "captcha_asked"`, `CAPTCHA_WRONG = "captcha_wrong"`,
  `CAPTCHA_TIMEOUT = "captcha_timeout"`, each with a `#:` comment citing D209.
- `example.yaml`: `ask_user_tries: 2` under `retrieval.captcha`, with a comment.
- Tests (docstrings start with `D209`): (a) the default is 2; (b) `ask_user_tries: 0` is refused; (c) a
  `PortalCaptcha` with `solver: [tesseract]` is refused; (d) `solver: []` is refused; (e) `solver: [user]` loads.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 4: the captcha wire format `[worker]`

Files: `src/invoicebot/fetchers/browser/protocol.py`, `tests/unit/test_browser_protocol.py`.

- `CaptchaForm` (frozen, `extra="forbid"`): `page_url: str`, `action_url: str`, `fields: tuple[tuple[str, str],
...]` (at most 200 pairs, each name ≤ 128 chars), `submit_name: str` (≤ 128), `submit_value: str` (≤ 256). A
  validator caps the JSON of the whole model at 256 KiB (`MAX_CAPTCHA_FORM_BYTES`).
- `CaptchaChallenge` (frozen): the PNG's `path: Path`, `size: int` (1 ≤ size ≤ 64 KiB,
  `MAX_CAPTCHA_IMAGE_BYTES`), `sha256` (same pattern as `HelperFile`), and `form: CaptchaForm`.
- `HelperOutput` gets `captcha: CaptchaChallenge | None = None`; a validator requires it exactly when `reason` is
  `captcha_asked`.
- `CaptchaResume` (frozen): `form: CaptchaForm`, `answer: str` (pattern `^[A-Za-z0-9]{1,12}$`, `CAPTCHA_ANSWER_PATTERN`).
  `HelperRequest` gets `resume: CaptchaResume | None = None`.
- Tests (`D209`): (a) a `captcha_asked` output without `captcha` is refused; (b) an `ok` output with one is refused;
  (c) a form over 256 KiB is refused; (d) an image of 64 KiB + 1 is refused; (e) answers `ab12` pass, `ab 12`, `` and
  13 letters are refused; (f) a round trip of a request with `resume` keeps every field.

Test: `uv run pytest -q tests/unit/test_browser_protocol.py`.

## Step 5: the chain knows captcha reasons `[worker]`

Files: `src/invoicebot/fetchers/chain.py`, `tests/unit/test_fetchers_chain.py`.

- `decide()` gets `captcha_tries: int`. `CAPTCHA_ASKED` never ends a fetcher (it is recorded only so the round
  counts asks; `decide` is not called while the invoice waits). `CAPTCHA_WRONG` runs the same fetcher again while the
  round's count of `CAPTCHA_ASKED` for it is below `captcha_tries`, else that fetcher is over (as a final reason).
  `CAPTCHA_TIMEOUT` is final. No other reason changes.
- Tests (`D209`): (a) last attempt `captcha_wrong` after one ask with tries 2 → run the same fetcher; (b) after two
  asks → next fetcher; (c) `captcha_timeout` → next fetcher; (d) `captcha_asked` as the last attempt → run the same
  fetcher (the resume); (e) an existing case per reason still passes unchanged.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py`.

## Step 6: split filling from submitting `[worker:low]`

Files: `src/invoicebot/fetchers/browser/form.py`.

- Refactor only: `fill_and_download` is split into `fill_form(page, portal, actions)` (types, selects, picks,
  calendars) and `read_result(page, portal, out_dir, max_bytes, saved)` (waits for the not-found marker or the
  downloads on the page that is already loaded, saves the files). `fill_and_download` keeps its signature: it calls
  `fill_form`, clicks `submit`, then `read_result`, so its behavior and errors are unchanged. Public names get
  docstrings.
- No new test; every existing test passes unchanged.

Test: `uv run pytest -q tests/integration/test_browser_form.py tests/integration/test_fetchers_portal.py`.

## Step 7: the helper's ask mode `[worker]`

Files: `src/invoicebot/fetchers/browser/captcha_ask.py` (new), `src/invoicebot/fetchers/browser/__main__.py`,
`tests/integration/test_browser_captcha_ask.py` (new).

- `captcha_ask.py` (docstring: "Fill a captcha portal's form and save the captcha and the form for a resume
  (D209)."): `async def ask_captcha(page, portal, invoice, out_dir) -> FetchResult | CaptchaChallenge`. It opens the
  portal URL, calls `fill_form`, screenshots `portal.captcha.image` to `out_dir/captcha.png` (type `png`), refuses one
  over `MAX_CAPTCHA_IMAGE_BYTES` (`technical`), and reads the form of `portal.captcha.input` with one
  `page.evaluate` that returns its resolved `action`, `location.href` and the `FormData` entries as string pairs
  (file inputs dropped, the captcha input dropped); the submit button is `portal.submit`'s `name` and `value`.
  Playwright errors become `technical` or `timeout` as in `form.py`.
- `__main__.py`: when the portal's captcha is a `PortalCaptcha` and the request has no `resume`, run `ask_captcha`;
  a challenge is emitted as `HelperOutput(reason=captcha_asked, captcha=...)` with the PNG's size and SHA-256.
- Tests (`D209`), on a local page served as `tests/integration/test_browser_form.py` serves its pages: (a) a form with two hidden
  fields, a text field and a captcha image gives a PNG and those fields in order, without the captcha input; (b) a
  missing captcha selector gives `timeout`; (c) a captcha image over the cap gives `technical`.

Test: `uv run pytest -q tests/integration/test_browser_captcha_ask.py tests/unit/test_browser_main.py`.

## Step 8: the helper's resume mode `[worker]`

Files: `src/invoicebot/fetchers/browser/captcha_resume.py` (new), `src/invoicebot/fetchers/browser/__main__.py`,
`tests/integration/test_browser_captcha_resume.py` (new).

- `captcha_resume.py` (docstring: "Replay a saved form with the sender's captcha answer and read the result
  (D209)."): `async def resume_captcha(page, portal, resume, out_dir, max_bytes) -> FetchResult`. It routes the one
  navigation to `resume.form.action_url` as a `POST` (`application/x-www-form-urlencoded`) of the saved fields, the
  captcha input's `name` with the answer, and the submit name and value, in that order; it then calls step 6's
  `read_result` on the loaded page. If no file was saved, the not-found marker is absent and `portal.captcha.input`
  is visible on the result page, the reason is `captcha_wrong`; the not-found marker wins over the captcha input.
- `__main__.py`: a request with `resume` runs `resume_captcha`.
- Tests (`D209`), on a local server that checks the posted body: (a) the right answer gives the files and `ok`;
  (b) a wrong answer re-serves the form and gives `captcha_wrong`; (c) the body carries the hidden fields unchanged and
  in order; (d) the not-found marker gives `not_found_yet` as a normal fetch does, even when the page also shows the
  captcha input again.

Test: `uv run pytest -q tests/integration/test_browser_captcha_resume.py tests/unit/test_browser_main.py`.

## Step 9: the portal fetcher asks and resumes `[worker]`

Files: `src/invoicebot/fetchers/portal.py`, `src/invoicebot/fetchers/base.py`, `tests/integration/test_fetchers_portal.py`.

- `base.py`: `FetchResult` gets `challenge: CaptchaChallenge | None = None` (import from the protocol module under
  `TYPE_CHECKING` only if a cycle appears; else a plain import).
- `portal.py`: `_portal_for` keeps `captcha_unsolvable` for `image`, `recaptcha` and any captcha that is not a
  `PortalCaptcha` with `user`; a `PortalCaptcha` portal that is `active` runs. A `captcha_asked` output is checked: the
  PNG lies inside `out_dir`, its size and SHA-256 match the bot's own (reuse `check_files` logic), and the form's
  `action_url` and `page_url` are `https` on the portal URL's host; else `technical`. New method
  `async def resume(self, invoice, form, answer, out_dir) -> FetchResult` sends a request with `resume` and handles
  the output as `fetch` does.
- Tests (`D209`), with a fake helper command as the existing tests use: (a) a valid challenge comes back on the
  result; (b) an action URL on another host gives `technical`; (c) a PNG outside `out_dir` gives `technical`; (d) a
  `captcha: image` portal still gives `captcha_unsolvable`; (e) `resume` passes the answer and form to the helper's
  stdin.

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py`.

## Step 10: the `portal_captchas` table `[worker]`

Files: `migrations/versions/0020_portal_captchas.py` (new), `src/invoicebot/store/captchas.py` (new),
`tests/integration/test_store_captchas.py` (new).

- Migration 0020 (expand only, `op.execute`): `portal_captchas(invoice_id bigint PRIMARY KEY REFERENCES invoices ON
DELETE CASCADE, portal_key text NOT NULL, form_json jsonb NOT NULL, image_path text NOT NULL, answer text NULL,
asked_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL)`.
- `captchas.py` (docstring: "Store the one pending captcha of an invoice (D209)."): `put(conn, invoice_id,
portal_key, form, image_path, timeout_s)` (upsert; `expires_at = now() + timeout`), `get(conn, invoice_id) ->
PendingCaptcha | None` (frozen dataclass, with `expired: bool` from the database clock), `set_answer(conn,
invoice_id, answer) -> bool` (only while not expired and unanswered), `delete(conn, invoice_id)`. Never commits.
- Tests (`D209`): (a) put then get; (b) a second put replaces the first; (c) `set_answer` after expiry returns False
  (expiry set in the past by a 0 s timeout); (d) delete removes it; (e) deleting the invoice cascades.

Test: `uv run pytest -q tests/integration/test_store_captchas.py tests/integration/test_schema.py`.

## Step 11: captcha messages `[worker:low]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/rules/catalog.py`, `tests/unit/test_messages_es.py`.

- `MESSAGES["BR-F18"] = "🔐 El portal de {portal} pide un captcha. Escríbeme las letras de la imagen (tienes {minutes}
min)."`; `MESSAGES["BR-F19"] = "⌛ Ese captcha ya venció; seguí con otras opciones."`; `MESSAGES["BR-F20"] = "🔁 No
pude con ese captcha. Escríbeme las letras de esta nueva imagen."`; `MESSAGES["BR-F20_shape"] = "✏️ Escríbeme solo
las letras y números de la imagen."`. Register BR-F18, BR-F19 and BR-F20 in `catalog.py` like BR-F05.
- Tests (`D209`): each new id is in the catalog and formats with its placeholders.

Test: `uv run pytest -q tests/unit/test_messages_es.py`.

## Step 12: the fetch step asks and resumes `[worker:high: reasoning: state moves, the round's asks and the resume share one transaction across fetch.py]`

Files: `src/invoicebot/pipeline/captcha.py` (new), `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/steps.py`,
`tests/integration/test_pipeline_captcha.py` (new).

- `steps.py`: `CAPTCHA_TIMEOUT_STEP = "captcha_timeout"`.
- `captcha.py` (docstring: "Ask the sender for a portal's captcha and resume the fetch with the answer (D209)."):
  `ask(conn, ctx, invoice, result)`: moves the PNG into `data_dir/captchas/<invoice>/` (`CAPTCHA_DIRECTORY`), `put`s
  the row with `ask_user_timeout_s`, `set_state(WAITING_CAPTCHA, expected=FETCHING_DOCS, rule="BR-F18")` (BR-F20 when
  the last attempt was `captcha_wrong`), queues the PNG with `queue_document` and the BR-F18 or BR-F20 caption, and
  enqueues `CAPTCHA_TIMEOUT_STEP` at `expires_at`, all in the caller's transaction.
- `fetch.py`: before `decide`, if the invoice has an answered `portal_captchas` row, call the portal fetcher's
  `resume` and record its attempt as a normal fetch; delete the row and its PNG after the result is recorded. A
  `captcha_asked` result is recorded as an attempt and goes to `ask`, not `_record_result`'s retry path. `decide` gets
  `captcha_tries` from config. The `resume` dependency enters through the existing registry entry (the portal fetcher),
  never a module global.
- Tests (`D209`), with a fake portal fetcher: (a) `captcha_asked` → `WAITING_CAPTCHA`, one outbox document with
  BR-F18, one row, one timeout job; (b) an answered row → `resume` is called with the answer, `ok` → `DELIVERING` and
  the row is gone; (c) `captcha_wrong` with tries 2 → asks again with BR-F20; (d) a second `captcha_wrong` → next
  fetcher; (e) flow B's fetch path with a no-captcha portal is unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_captcha.py tests/integration/test_pipeline_fetch.py`.

## Step 13: the sender's answer `[worker]`

Files: `src/invoicebot/pipeline/captcha_reply.py` (new), `src/invoicebot/pipeline/intake.py`,
`tests/integration/test_pipeline_captcha_reply.py` (new).

- `captcha_reply.py`: `async def answer(conn, profile_id, msg) -> bool`. When `last_question` for the sender is BR-F18
  or BR-F20 and that invoice is `WAITING_CAPTCHA`: a text matching `CAPTCHA_ANSWER_PATTERN` (after stripping spaces at
  the ends) is stored with `set_answer`; on True the invoice goes to `FETCHING_DOCS` (rule BR-F18) and `FETCH_STEP` is
  enqueued; on False (expired) BR-F19 is sent. A text that does not match sends `BR-F20_shape` and stays. Returns
  whether it handled the text.
- `intake.py`: call it before `portal_offer.answer` in `_store_with_overrides`, for text messages only.
- Tests (`D209`): (a) `AB12` → `FETCHING_DOCS`, answer stored, a fetch job; (b) `ab 12 x` → `BR-F20_shape`, state
  unchanged; (c) after expiry → BR-F19, state unchanged; (d) a text when the newest question is another rule → not
  handled; (e) a photo is not handled.

Test: `uv run pytest -q tests/integration/test_pipeline_captcha_reply.py tests/integration/test_intake.py`.

## Step 14: the captcha timeout and wiring `[worker]`

Files: `src/invoicebot/pipeline/captcha_timeout.py` (new), `src/invoicebot/app.py`,
`tests/integration/test_pipeline_captcha_timeout.py` (new).

- `captcha_timeout.py`: `make_captcha_timeout_handler(...)`: when the invoice is still `WAITING_CAPTCHA` with an
  unanswered, expired row, record a `captcha_timeout` attempt for `portal_registry`, delete the row and its PNG, send
  BR-F05, `set_state(FETCHING_DOCS, rule="BR-F05")` and enqueue `FETCH_STEP`, in one transaction. Anything else does
  nothing (the sender answered, or the invoice moved).
- `app.py`: register `CAPTCHA_TIMEOUT_STEP` in `handlers`.
- Housekeeping is not touched: a cancelled invoice keeps its row only until the invoice is purged (cascade); its PNG
  lies under `data_dir/captchas/`, which step 12's resume and this timeout clear.
- Tests (`D209`): (a) expired and unanswered → BR-F05 sent, `FETCHING_DOCS`, a `captcha_timeout` attempt, row gone;
  (b) answered → nothing; (c) invoice `CANCELLED` → nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_captcha_timeout.py`.

## Step 15: a late answer gets BR-F19 `[worker]`

Files: `src/invoicebot/pipeline/captcha_reply.py`, `tests/integration/test_pipeline_captcha_reply.py`.

- Owner decision (2026-11-27): D209's "a late answer gets BR-F19" also holds after the timeout. In `answer`, when the
  newest question is BR-F05, the portal fetcher's newest attempt is `captcha_timeout` and the stripped text matches
  `CAPTCHA_ANSWER_PATTERN`: queue BR-F19 for that invoice and return True. Any other text after BR-F05 is not handled.
- Tests (`D209`): (a) after the timeout's BR-F05, `AB12` → BR-F19, state and attempts unchanged; (b) after BR-F05,
  `ab 12 x` → not handled; (c) BR-F05 newest but the portal's newest attempt is not `captcha_timeout` → not handled.

Test: `uv run pytest -q tests/integration/test_pipeline_captcha_reply.py`.

## Step 16: PEBSA's store and portal files `[inline: values from the step 1 spike]`

Files: `config/portals/pebsa.yaml` (new), `config/stores/<PEBSA's RUC>.yaml` (new, D47), `config/example.yaml`,
`config/config.yaml`.

- `example.yaml` and `config.yaml`: `retrieval.captcha.ask_user_timeout_s: 150`, with a comment citing D209 and the
  spike (the captcha lives 180–240 s).

- The portal file from the live config, with `url` set to `frmAutentificacionEstandar.aspx` (the spike: the
  Convivencia page never answers), the spike's fields, `captcha: {image: "img[src*='CaptchaImage.axd']",
input: "#ctl00_bodyContentPlace_txtCaptcha", solver: [user]}`, `submit`, `downloads`, `not_found`,
  `tipo_labels`, and `status: active`. Its notes cite D209 and the spike.
  Test: `uv run pytest -q tests/unit/test_config_checks.py tests/unit/test_config_models.py`.

## Step 17: live check `[inline: live run, needs the owner's Telegram]`

Files: `specs/spikes/portal-guide.md`.

Tag the live commit, back up the live config, deploy the branch's image, and install PEBSA's files in the live config.
The live config also sets `ask_user_timeout_s: 150`. Then **ask the owner** to send a recent PEBSA ticket on
Telegram (flow A): the portal has no record of #52's ticket (step 1), so (b) needs one it knows, and that run also
records the result page and download buttons step 1 deferred. Rows: (a) the captcha PNG arrives with BR-F18;
(b) the right answer gives the files and delivery; (c) a wrong answer gives BR-F20 and a new image; (d) no answer for
`ask_user_timeout_s` gives BR-F05 and the next fetcher; (e) a late answer gives BR-F19; (f) flow B untouched. Record
the rows, with image ids and no invoice values.

Test: `npx prettier --check specs/spikes/portal-guide.md`.

## Step 18: runbook `[inline: user-facing docs]`

Files: `docs/runbook.md`.

- "Add a portal" gets a captcha part: `captcha: {image, input, solver: [user]}`, how to find the two selectors, what
  the sender sees, and that `ask_user_timeout_s` must stay under the site's own captcha lifetime (the spike's number).

Test: `npx prettier --check docs/runbook.md`.
