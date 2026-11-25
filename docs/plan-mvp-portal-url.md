# Plan: MVP phase 9t1 · portal-url

Roadmap: `docs/plan-mvp.md`, row 9t1, the first slice of 9t. The owner's answers of 2026-11-25 (roadmap, "Carried over
from the `portal-guess` review") hold more work than one plan's 20 steps, so 9t is split, in this order:

| Phase | Slug           | What                                                                                                       |
| ----- | -------------- | ---------------------------------------------------------------------------------------------------------- |
| 9t1   | `portal-url`   | this plan: try the candidate addresses and offer the one that answers; three small answers (steps 2 to 4)  |
| 9t2   | `portal-learn` | remember the confirmed reads per issuer and host, drop a dead link, propose a rule; an approved link moved |
| 9t3   | `portal-probe` | after [Aprobar], the sandboxed browser probes the form, tries it once and drafts the portal YAML (D189)    |
| 9t4   | `portal-guide` | the captcha shown to the sender (reopens D99), then upload, then SUNAT's API                               |
| 9y    | `smart-edit`   | [✏️ Corregir] on a summary: fields as buttons, value preloaded, the answer checked before it is applied    |

What the code does today: [🔗] (`docs_help.teach_portal`) calls `portal_offer.ask_portal`, which offers one URL at once
(`printed_url`, else `repair_url(url_strip)`, else the QR link) and never tries it. It runs inside intake's
transaction, so a request cannot be made there. `net/http.GuardedTransport` and `app.py`'s `guarded_http` client
already pin the checked address and follow no redirect.

Out of scope: learning and the `GitHub` rule issue (9t2); a URL printed over two lines (the strip is one line, needs its
own spike, 9t2); trying a typed URL (it is echoed as today); trying a QR link (it can be a one-use document link).

Needs from the owner: review of D192 and the texts in step 1; Telegram and a ticket for step 12.

Decision this plan proposes (step 1):

- **D192 portal URL reconciliation (9t1, amends D191).**
  - **Candidates.** `portal_guess.candidate_urls` repairs OCR slips and returns at most 8 URLs, best first. The host as
    read comes before a close approved host (owner). Path case is kept first and lowered second.
  - **Trying.** [🔗] with a printed URL or a strip answers `BR-F14_checking` and queues a `portal_check` job. The job
    sends plain `GET`s through the request guard: `https`, default port, public address, no SUNAT host, at most 5
    redirects with each hop checked the same way, no body read, no cookies, 8 s per candidate and 20 s in all, one at a
    time. Status 200 to 299 after redirects counts as an answer. Replaces D191's "the bot never fetches the guess".
  - **Offer.** The first candidate that answers is offered as BR-F14. When none answers, the first candidate is shown as
    `BR-F14_unverified` (rule BR-F14, same buttons: the site may just be down). A strip that gives no candidate is shown
    as `BR-F15_unread` (rule BR-F15). A page that answers is no proof it is the issuer's, so [Sí] and [Aprobar] stay.
  - **Never stuck.** A failed request is an answer, not an error, so the job does not fail the invoice over the network.
    The job is at least once: a crash after its commit can repeat the offer, with the same URL and working buttons.
  - **Answers.** `campo=valor` is an edit while BR-F15 is open. A URL typed while BR-F14 is newest is the correction. A
    newer, different link replaces the one that waits for the owner; the old [Aprobar] answers `BR-S06_stale`.

## Step 1: D192, spec texts and messages `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `specs/03-design.md`,
`src/invoicebot/rules/messages_es.py`.

- D192 as above in `05`; D191 gets "Amended by D192".
- `06`: the BR-F14, BR-F15 and BR-S06 rows and the "BR-F02's answers" paragraph. `02` R-11.4: the guess may be tried
  under the request guard before it is offered. `03` §5 "Printed portal URL": the `portal_check` job.
- Messages:
  - `BR-F14_checking`: `Reviso la dirección impresa en el ticket y te la muestro en un momento.`
  - `BR-F14_unverified`: `Leí esta dirección para {razon_social}, pero no me responde:\n{url}\n¿Es correcta? Si no, toca
[✏️ Corregir].` (one line in code after each `\n`)
  - `BR-F15_unread`: `En el ticket leí «{strip}», pero no es una dirección completa. Escríbeme la dirección del portal de
{razon_social} tal como aparece, o toca [No sé].`
  - `BR-S06_stale`: `Ese enlace de RUC {ruc_emisor} ya fue reemplazado por uno más nuevo; decide en el aviso más
reciente.`

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_messages_es.py tests/unit/test_docs.py`

## Step 2: A newer link replaces the waiting one `[worker]`

Files: `src/invoicebot/store/taught_links.py`, `src/invoicebot/pipeline/teach.py`,
`tests/integration/test_pipeline_teach.py`.

- `propose`: the `ON CONFLICT` condition becomes `taught_links.state = 'rejected' OR (taught_links.state = 'proposed'
AND taught_links.url <> EXCLUDED.url)`. Everything else in the statement stays. Update the docstrings of `propose`, of
  `teach.py` and of `teach` to say so.
- `teach.handle_press`: when `decide` returns None, read `taught_links.get`; if a link is `proposed`, queue
  `MESSAGES["BR-S06_stale"].format(ruc_emisor=...)` to the pressing admin with rule id `BR-S06_stale` (constant
  `RULE_DECISION_STALE`), then return. Any other state, or no link, returns silently as today.

Test cases (docstrings start with `BR-S06`):

- Waiting URL A, then different URL B from another sender → row has B, that sender, `proposed`, version 2; each admin
  has two alerts, the second with version 2 buttons and B; the sender is thanked.
- Waiting URL A, then A again → version stays 1, no second admin alert, sender thanked. (Rework
  `test_propose_second_link_while_proposed_asks_no_admin_again` into this case; its different-URL expectation is
  replaced by the owner's answer.)
- Version 1's [Aprobar] after B → link still `proposed` with B; the admin gets `BR-S06_stale`. Extend
  `test_press_older_proposal_version_changes_nothing` only with the new message.
- Version 2's [Aprobar] → B `approved`.
- An approved link, then another URL → unchanged (existing test still passes).
- A second press after a decision → no new outbox row (existing test still passes).

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py`

## Step 3: `campo=valor` is an edit while BR-F15 is open `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`, `specs/06-business-rules.md`.

- In `_store_with_overrides`, compute `edits = parse_edits(msg.text)` and `overrides = parse_overrides(msg.text)` before
  the portal answer. Skip `answer` when the text is an edit: `edits.fields`, or an error whose `field` is not None, or
  `overrides`. An error with `field` None (unknown key) is not an edit. Keep the rest of the flow, without parsing
  twice. Update the docstring. In `06`, only if step 1 left it out, the BR-F15 row says the same.

Test cases (docstrings start with `BR-F15, D192`), with BR-F15 the sender's newest question for an editable invoice:

- `total=118` → the edit is applied; no `BR-F15_retry` row.
- `ruc=123` → the edit's own error reply; no `BR-F15_retry`.
- `motivo=almuerzo` → the override is stored; no `BR-F15_retry`.
- `https://portal.example.pe/?ruc=2` (resolver answers a public address) → echoed as BR-F14, no edit.
- `color=rojo` → `BR-F15_retry`.
- `hola` → `BR-F15_retry`.

Test: `uv run pytest -q tests/integration/test_intake.py tests/unit/test_edits_parse.py`

## Step 4: A URL typed while BR-F14 is newest is the correction `[worker]`

Files: `src/invoicebot/pipeline/portal_offer.py`, `tests/integration/test_pipeline_portal_offer.py`.

- `answer`: when the newest question is BR-F14, handle the text only if `looks_like_url(msg.text)`, the invoice exists
  and is not in `SETTLED`, and `portal_candidates.get` is not None; then call `_offer_typed_url` and return True.
  Otherwise return False. Update the docstring.

Test cases (docstrings start with `BR-F14, D192`), BR-F14 newest with candidate A:

- Typing valid `https` URL B → a new BR-F14 with B; the candidate is B; A's [Sí] is now BR-S02.
- B's [Sí] → B is proposed.
- A URL the resolver answers with a private address → `BR-F15_retry`; the candidate stays A.
- `hola` → `answer` returns False and queues nothing.
- A `DONE` invoice → False and nothing queued.
- The candidate already deleted by [Sí] (newest question still BR-F14) → False and nothing queued.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py`

## Step 5: More OCR slips repaired in the scheme and host `[worker]`

Files: `src/invoicebot/pipeline/portal_guess.py`, `tests/unit/test_pipeline_portal_guess.py`.

The helper's `clean_strip` already turns `;`, `,`, `|` and accented letters into spaces, so the repair sees spaces
there. Keep `repair_url`'s signature and its existing tests.

- Scheme: after the current pattern, if it removed nothing, a leading token matching `^([a-z0-9]{4,6})[.:]*[\s:/]+` is
  dropped when its `SequenceMatcher` ratio to `https` or to `http` is at least `SCHEME_MATCH_MIN_RATIO = 0.6`.
- Host, after the separators become dots: a last label `corn` becomes `com`, `compe` becomes `com.pe`, `cornpe` becomes
  `com.pe`; `corn.pe` at the end becomes `com.pe`. A first label `ww`, `wwv`, `vvww`, `wvw` or `wwvv` becomes `www`.
- A trailing period after the path's last segment is dropped, as the host's already is.

Test cases (docstrings start with `D192`), issuer None and no known hosts:

- `harps: sv.example.pe/x` → `https://sv.example.pe/x`.
- `htips sv.example.pe/x` → `https://sv.example.pe/x`.
- `hotel.example.pe/x` → `https://hotel.example.pe/x` (no scheme dropped).
- `hotel example.pe/x` → `https://hotel.example.pe/x`.
- `portal.example.corn/x` → `https://portal.example.com/x`.
- `portal.example.corn.pe` → `https://portal.example.com.pe`.
- `portal.example.compe/x` → `https://portal.example.com.pe/x`.
- `wwv.example.pe/x` → `https://www.example.pe/x`.
- `https://portal.example.pe/tienda.` → `https://portal.example.pe/tienda`.
- `sv.example. pe/x` → `https://sv.example.pe/x`.
- `https://e-consulta.sunat.gob.pe/x` → None (unchanged).

Test: `uv run pytest -q tests/unit/test_pipeline_portal_guess.py`

## Step 6: `candidate_urls` `[worker]`

Files: `src/invoicebot/pipeline/portal_guess.py`, `tests/unit/test_pipeline_portal_guess.py`.

- `candidate_urls(text: str, issuer_name: str | None, known_hosts: frozenset[str]) -> tuple[str, ...]`, pure, at most
  `MAX_CANDIDATES = 8`, no repeats, only URLs `_is_portal_url` accepts, in this order:
  1. The host as read (step 5's repair, without `_closest_known_host`) with each path: the last segment repaired from the
     issuer's name (today's rule, lowercase), the path as read with its capitals, the path in lowercase.
  2. The closest known host (today's rule), if any, with the first path.
  3. The host with the spaces removed instead of turned into dots, when the text's host part has spaces, first path.
  4. The host with a leading `www.` added, or removed when it has one, first path.
  5. The host with one look-alike changed at one place outside the last label, first path, in the order of
     `LOOKALIKES = (("0","o"),("1","l"),("5","s"),("8","b"),("vv","w"),("rn","m"),("l","1"),("l","i"),("i","l"),
("o","0"),("m","rn"))`, each pair left to right.
- The path's segment filter keeps capitals here (`[^A-Za-z0-9._~-]` is dropped). Lowercase only the host and the issuer
  comparison.
- `repair_url` stays until step 11; step 10 removes its last caller.

Test cases (docstrings start with `BR-F14, D192`):

- `hitps.//sy.example pe/tiemda`, `TIENDA SOL S.A.C.`, `{"sv.example.pe"}` → first `https://sy.example.pe/tienda`, second
  `https://sy.example.pe/tiemda`, and `https://sv.example.pe/tienda` comes after both.
- `https://portal.example.pe/Tienda/ABC`, None, ∅ → first two are `…/Tienda/ABC` and `…/tienda/abc`.
- `https://portal.example.pe`, None, ∅ → first `https://portal.example.pe`, second `https://www.portal.example.pe`.
- `https://www.portal.example.pe/x` → second is `https://portal.example.pe/x`.
- `https://p0rtal.example.pe/x` → `https://portal.example.pe/x` is in the result.
- `por tal.example.pe/x` → `https://portal.example.pe/x` is in the result, after `https://por.tal.example.pe/x`.
- A host with many look-alikes (`solol1.mill0.pe/x`) → exactly 8, all different.
- `hiips:/is etomi, aio`, `   ` and `https://e-consulta.sunat.gob.pe/x` → `()`.
- Known host `e-consulta.sunat.gob.pe` close to a read host → never in the result.

Test: `uv run pytest -q tests/unit/test_pipeline_portal_guess.py`

## Step 7: `net/url_probe` `[worker]`

Files: `src/invoicebot/net/url_probe.py` (new), `tests/unit/test_net_url_probe.py` (new).

- Docstring: say which of a few guessed addresses answers, reading no body (R-5.10, D192).
- `async def first_answering(http: httpx.AsyncClient, urls: Sequence[str]) -> str | None`: the caller passes a client over
  `GuardedTransport`. Tries `urls` in order, one at a time, at most `PROBE_TIMEOUT_S = 8.0` each and
  `PROBE_TOTAL_S = 20.0` in all (`asyncio.timeout`); returns the first URL that answers (the URL given, not where it
  redirects), else None.
- One try: refuse when `taught_url_refusal(url)` is not None; a streamed `GET` with the
  headers `Accept: text/html` and `Accept-Encoding: identity`, never reading the body; a 3xx with a `location` goes on
  to `urljoin(current, location)`, at most `MAX_PROBE_REDIRECTS = 5`, each hop refused by the same check; 200 to 299
  answers; any other status does not. Build each request with `http.build_request` and delete its `cookie` header
  before `http.send(request, stream=True, follow_redirects=False)`; close the response unread. The shared client's
  cookie jar is not cleared.
- `GuardBlocked`, `httpx.HTTPError`, `httpx.InvalidURL`, `TimeoutError` and `ValueError` (a bad `location`) are "no
  answer". Log only the exception class or the status, never the URL or the host.

Test cases (docstrings start with `R-5.10, D192`), over `httpx.MockTransport`:

- 200 → that URL. 404 then 200 → the second. 403, 500 → None. Empty list → None and no request.
- 302 to another public `https` URL answering 200 → the first URL given.
- 302 to `http://…`, to `https://127.0.0.1/`, to `https://e-consulta.sunat.gob.pe/`, to `https://x.example.pe:8443/` →
  None, and the target is never requested.
- Six redirects → None after the first URL and five hops; five redirects then 200 → the URL given. A 302 without `location` → None.
- A transport raising `GuardBlocked`, `ConnectError`, `ReadTimeout` → the next URL is tried.
- A hop that hangs past a patched `PROBE_TIMEOUT_S` → the next URL is tried; a patched `PROBE_TOTAL_S` → None.
- A body stream that raises when read → still an answer (the body is not read).
- A 200 that sets a cookie, then a second call → the second request has no `cookie` header.
- The log holds neither the URL nor the host.
- Over `GuardedTransport` with a resolver answering `10.0.0.1` → None and the inner transport gets no request.

Test: `uv run pytest -q tests/unit/test_net_url_probe.py && uv run lint-imports`

## Step 8: The `portal_check` handler `[worker]`

Files: `src/invoicebot/pipeline/steps.py`, `src/invoicebot/pipeline/portal_check.py` (new),
`src/invoicebot/pipeline/portal_offer.py`, `tests/integration/test_pipeline_portal_check.py` (new).

- `steps.py`: `PORTAL_CHECK_STEP = "portal_check"` with a comment like its neighbors.
- `portal_offer.py`: `offer_candidate` gets a keyword `message: str = RULE_PORTAL_OFFER` naming the text (the rule id,
  buttons and stored candidate stay BR-F14's); constant `MESSAGE_UNVERIFIED = "BR-F14_unverified"`. New
  `ask_unread(conn, addressee, invoice_id, reading, strip)` queues `BR-F15_unread` under rule BR-F15 with the [No sé]
  button. Where the reading has no `ruc_emisor`, `ask_unread` logs and queues nothing, as `_ask_br_f02` does.
- `portal_check.py`: `make_portal_check_handler(pool: Pool, http: httpx.AsyncClient) -> Handler`:
  1. One connection: `load_invoice_head`, `load_reading`, `taught_links.approved_hosts`. Stop quietly unless the head
     exists, its state is `FETCHING_DOCS` or `MANUAL_REQUIRED`, and the reading exists.
  2. `text = reading.printed_url or reading.url_strip`; stop quietly when None. `candidate_urls(text, razon_social,
hosts)`, then `first_answering`, with no connection held.
  3. One transaction: load the head again and stop quietly if its state left those two; then the answering URL →
     `offer_candidate`; none but candidates → `offer_candidate(..., message=MESSAGE_UNVERIFIED)` with the first; no
     candidates → a QR link `offer_url` would accept → `offer_candidate` with it, else `ask_unread` with the text.
  - The addressee is `Addressee(head.origin_channel, head.profile_id)`. Log `step done` with the step, the invoice id and
    the duration, as `sunat_recheck` does; no URL.
- Split the QR part of `offer_url` into `qr_offer(reading) -> str | None` and call it from both.

Test cases (docstrings start with `BR-F14, D192`), real Postgres and `httpx.MockTransport`:

- Strip with two candidates, the second answers 200 → BR-F14 with the second; the candidate row is the second.
- None answers → `BR-F14_unverified` text with the first candidate, rule BR-F14, three buttons; [Sí] proposes it.
- `printed_url` set → its first candidate is tried first.
- Strip that gives no candidate and no QR link → `BR-F15_unread` with the strip and [No sé], rule BR-F15; a QR link that
  can be taught → BR-F14 with the link and no request sent.
- State `CANCELLED` and `DONE` → no request, nothing queued; a state moved to `CANCELLED` by the mock handler during the
  request → nothing queued.
- The transport raises `ConnectError` every time → the unverified offer, and the handler does not raise.
- No reading → nothing queued.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_check.py tests/integration/test_pipeline_portal_offer.py && uv run lint-imports`

## Step 9: The app registers `portal_check` `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

- In `handlers`: `PORTAL_CHECK_STEP: make_portal_check_handler(pool, guarded_http)`.

Test case (docstring starts with `D192`): the handler mapping the app gives the worker has `portal_check`; use the seam
the file's other handler tests use. If it has none, build the smallest one that keeps the lifespan under the lint caps.

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/unit/test_app.py`

## Step 10: [🔗] queues the check `[worker]`

Files: `src/invoicebot/pipeline/portal_offer.py`, `src/invoicebot/pipeline/teach.py`,
`tests/integration/test_pipeline_portal_offer.py`, `tests/integration/test_pipeline_docs_help.py`.

- `ask_portal`: with a `printed_url` or a `url_strip`, queue `BR-F14_checking` (rule id `BR-F14_checking`, constant
  `RULE_PORTAL_CHECKING` in `teach.py`, added to `NOT_QUESTIONS`) and `enqueue(conn, invoice_id, PORTAL_CHECK_STEP)`, in
  the caller's transaction. Otherwise the QR link or BR-F02 as today, at once.
- Remove `offer_url`, whose last caller this was.
- The offer tests that expected an immediate BR-F14 from `printed_url` or a strip expect the notice and one job; the
  offers themselves are step 8's tests. No case may be dropped without its step 8 twin.

Test cases (docstrings start with `BR-F14, D192`):

- A strip, and a printed URL: each → one `BR-F14_checking` row, one `portal_check` job, no candidate row.
- A QR link only → BR-F14 at once, no job. Nothing → BR-F02 at once, no job.
- A second press on the same BR-F13's [🔗] → BR-S02 and still one job.
- A pending BR-F02 of another invoice, then `BR-F14_checking`, then a typed URL → the URL answers that BR-F02.
- The transaction rolls back → no row and no job.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py tests/integration/test_pipeline_docs_help.py && uv run mypy src`

## Step 11: Remove `repair_url` `[worker]`

Files: `src/invoicebot/pipeline/portal_guess.py`, `tests/unit/test_pipeline_portal_guess.py`.

- Remove `repair_url`, which has no caller left, and any private helper only it used. Update the module docstring: the
  candidates are tried before one is offered (D192).
- Every `repair_url` test case moves to `candidate_urls(...)[0]`, or `()` where it expected None, with the same values
  except one, by the owner's answer: with a close known host, the host as read is first.

Test: `uv run pytest -q tests/unit/test_pipeline_portal_guess.py && uv run mypy src`

## Step 12: Live check on Telegram `[inline: needs the owner's chat]`

Deploy the branch's image and record only results in `specs/spikes/portal-guess.md` ("Live check 9t1"): the PEBSA
ticket, [🔗], the notice, then BR-F14 with the address that answers and the seconds it took; a URL typed over BR-F14
(the owner's own test); `total=…` while BR-F15 is open; a second link replacing the waiting one and the old [Aprobar].
Then cancel the test invoice.

Test: every row passes or is waived by the owner.

Amended by the owner on 2026-11-25, after the first live run (3 redirects and 4 s were too tight for PEBSA's portal,
which used 3 or 4 redirects and 2.7 to 5.1 s): 8 s per candidate and 5 redirects, 20 s in all. The typed URL over BR-F14,
`total=…` and the second link are waived. The photo is sent once more on the new image.

Second run, 2026-11-25 (image `2852ea97f9d4`): `sy.ecomprobantes.pe` answers 503 on and off (19 of 30 tries with curl), so
the limits were not the cause of that run. Owner: choose the best approach; a slow site must not slow the others. Hence
steps 13 to 16, with no background retries (a 503 already proves the server exists; retrying belongs to 9t3), and 9t3
comes before 9t2.

## Step 13: D192 and the "site is down" text `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `src/invoicebot/rules/messages_es.py`,
`docs/plan-mvp.md`.

- D192: a candidate that fails with 429, 5xx or a connection dropped after it opened is busy: its server exists. The
  check stops there and offers it as `BR-F14_busy`, instead of a weaker guess. No retry in 9t1.
- `BR-F14_busy`: `Leí esta dirección para {razon_social}, pero el sitio está caído ahora:\n{url}\n¿Es correcta? Si
no, toca [✏️ Corregir].` (one line in code after each `\n`)
- Roadmap: 9t3 before 9t2, and 9t3 analyzes and tries the site before the issue is created (owner 2026-11-25).

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_messages_es.py tests/unit/test_docs.py`

## Step 14: The probe tells a busy address from no answer `[worker]`

Files: `src/invoicebot/net/url_probe.py`, `tests/unit/test_net_url_probe.py`, `src/invoicebot/pipeline/portal_check.py`.

- `@dataclass(frozen=True) class Probed: url: str; busy: bool`. `first_answering(http, urls) -> Probed | None`: the first
  URL that answers (`busy=False`) or is busy (`busy=True`), whichever comes first in order; the rest are not tried. Busy
  is 429, 500 to 599, or `httpx.ReadError`, on the URL or any hop (httpx also raises `RemoteProtocolError` for a malformed location, so that one stays no answer). All else stays no answer.
- One INFO line per call, `probe finished`, with `extra` `outcome` (`answered`, `busy`, `none`), `tried` and `reasons` (per
  tried URL: the status, the exception class name, the refusal value or `TimeoutError`). Never a URL or host.
- `portal_check.py`: only the call site, so it type-checks: it keeps today's behavior by treating a busy result as None.

Test cases (docstrings `R-5.10, D192`); existing ones compare with `Probed(url, False)`, and the 500 case moves to busy:

- 429, 500, 502, 503 → `Probed(PORTAL, True)`; 403 and 404 → None.
- `ReadError` → busy; `RemoteProtocolError`, `ConnectError`, `ReadTimeout`, `GuardBlocked` → the next URL is tried.
- 503 on the second hop of a redirect → `Probed(PORTAL, True)`.
- `[PORTAL busy, OTHER 200]` → `Probed(PORTAL, True)` and OTHER is never requested.
- `[PORTAL 404, OTHER 503]` → `Probed(OTHER, True)`.
- The log has one `probe finished` INFO record with the outcome, the count and the reasons, and no URL or host, for an
  answered, a busy and a none call, and when the total deadline passes.

Test: `uv run pytest -q tests/unit/test_net_url_probe.py && uv run mypy src`

## Step 15: The check offers a busy address with its own text `[worker]`

Files: `src/invoicebot/pipeline/portal_check.py`, `src/invoicebot/pipeline/portal_offer.py`,
`tests/integration/test_pipeline_portal_check.py`.

- `portal_offer.py`: `MESSAGE_BUSY = "BR-F14_busy"` next to `MESSAGE_UNVERIFIED`. `_record` offers a busy result with it
  (rule BR-F14, same buttons), an answer as today, and None as today. Update both docstrings.

Test cases (docstrings `BR-F14, D192`):

- First candidate 503 → one `BR-F14_busy` message with that URL and the three buttons; no other candidate is requested.
- First candidate 404, second 503 → `BR-F14_busy` with the second.
- [Sí, es este] on a `BR-F14_busy` offer proposes the link as it does on BR-F14.
- All 404 → `BR-F14_unverified` (existing test still passes).

Test: `uv run pytest -q tests/integration/test_pipeline_portal_check.py && uv run mypy src`

## Step 16: Live check again `[inline: needs the owner's chat]`

Deploy, send the PEBSA photo, tap [🔗]. BR-F14 with the address that answers or `BR-F14_busy` passes, matched with the
logged reasons. Record every row in `specs/spikes/portal-guess.md` ("Live check 9t1"), the waived ones and both earlier
runs included, then cancel the test invoice. [Aprobar] is not pressed.

Test: every row passes or is waived by the owner.

## Finish

`py-code-reviewer` and `py-security-auditor` (new outbound requests from untrusted text). Move this plan to
`docs/archive/`.
