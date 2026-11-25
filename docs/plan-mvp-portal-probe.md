# Plan: MVP phase 9t3 · portal-probe

Roadmap: `docs/plan-mvp.md`, row 9t3. After the owner's [Aprobar] the sandboxed browser opens the taught link, reaches
the form, records it, tries it once for a waiting invoice and puts a draft portal YAML in the `PORTAL_MISSING` issue.

What the code does today: [Aprobar] (`teach.handle_press`) stores the link and the gap's `hint_url`; nothing opens it.
`raise_gap` queues `create_issue` at once. The browser helper (`fetchers/browser`) only runs a finished portal file.
`session.browse` is typed for `Portal` and `FetchResult`. A job that dies fails its invoice, so the new handler must
turn every expected failure into an outcome.

Out of scope: showing a captcha to the sender (9t4); `recaptcha` solving; SUNAT; learning per host and links that moved
(9t2); using a draft without the owner's merge; two-tap confirmations (D189: the bot's own try replaces them); more than
one form try per approval.

Needs from the owner: review of D193, above all its five **Owner decides** items; Telegram and a ticket for step 18.

Decision this plan proposes (step 1), **D193 portal probe**:

- **When.** [Aprobar] queues one `portal_probe` job for the issuer's newest invoice in `FETCHING_DOCS` or
  `MANUAL_REQUIRED`. With none, nothing is probed and the link is stored as today.
- **What it may do.** Opens only the approved `https` URL, behind the guard proxy and the sandbox. At most 3 landing
  steps (links or buttons on the approved host or one of its subdomains, text matched against a fixed list). It never
  fills a page with a password field, never types on another host, and submits at most once. An image captcha,
  reCAPTCHA or hCaptcha ends the probe before anything is typed; none is bypassed. An unmatched required field also
  ends it before typing.
- **What it records.** Read before anything is typed: field selectors, labels, types, option labels and the matching
  invoice value's _name_; captcha kind; after a submit, only the download controls' selectors. No value, page text or
  query string. Stored in `portal_probes` (one row per issuer).
- **Files.** A downloaded XML must match the invoice (`result_from`); a PDF alone must read to the same RUC, serie,
  número and total through the extract helper. Files that match and cover the flow's documents are filed; the rest are
  deleted.
- **Busy.** 429, 5xx, a timeout or a dropped connection retries after 15 min, 1 h and 6 h, then ends as `busy`.
- **The issue.** While a probe can still come, a new `PORTAL_MISSING` issue waits at most 12 h; the probe's end,
  [Rechazar] and [No sé] release it. An issue that already exists gets the report as a comment.
- **Session addresses.** `/(S(...))/` and `;jsessionid=...` are removed when a link is taught and again when probed.

**Owner decides** (the plan proposes the first choice in each):

1. Look-alike hosts are skipped once the host as read gave any HTTP status, and BR-F14 says when the offered host
   differs from the one read (roadmap, carried over from `portal-url`).
2. A matching file is filed without asking the sender, as any portal's is.
3. Holding the issue up to 12 h (under R-11.11's 24 h alert).
4. The form is filled only on the approved host or its subdomains.
5. The public issue may show the site's selectors and the approved path; it shows no invoice data.

## Step 1: D193, spec texts and messages `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `specs/03-design.md`,
`src/invoicebot/rules/messages_es.py`.

- D193 in `05`; D189 gets "Amended by D193"; `02` R-11.4, R-11.5 and R-11.12 (selectors and field names may appear);
  `03` §8 the probe entry; `06` the BR-S06 row and a `portal_probe` paragraph.
- Messages (all have `{ruc_emisor}`, admins only, rule BR-S06):
  - `BR-S06_approved`: `✅ Guardé el portal de RUC {ruc_emisor}.`, plus ` Lo abro ahora y te cuento qué encuentro.` when a
    probe was queued (`BR-S06_approved_probe`).
  - `BR-S06_probe_filed`: `✅ Abrí el portal de RUC {ruc_emisor} y archivé el comprobante #{id}.`
  - `BR-S06_probe_form`: `🔎 Abrí el portal de RUC {ruc_emisor}: {outcome_desc}. El borrador va en el issue.`
  - `BR-S06_probe_failed`: `⚠️ No pude revisar el portal de RUC {ruc_emisor}: {outcome_desc}.`
  - `PROBE_OUTCOME_DESC`: one Spanish line per `ProbeOutcome` of step 4.
  - `BR-F14_other_host`: `En el ticket leí {read_host}, pero responde otra dirección para {razon_social}:\n{url}\n¿Es
correcta? Si no, toca [✏️ Corregir].` (one line in code after each `\n`)

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_messages_es.py tests/unit/test_docs.py`

## Step 2: Session addresses are removed `[worker]`

Files: `src/invoicebot/net/taught_url.py`, `src/invoicebot/pipeline/teach.py`, `tests/unit/test_taught_url.py`,
`tests/integration/test_pipeline_teach.py`.

- `without_session(url: str) -> str`: removes a path segment matching `\([A-Za-z]\([^/()]*\)\)` (repeated), and a
  `;jsessionid=...` suffix of any segment, ignoring case. Query and the rest stay. `check_taught_url` applies it after
  the refusal check and returns the result.

Test cases (docstrings start with `D193`):

- `https://p.example.pe/(S(abc123))/frm.aspx` → `https://p.example.pe/frm.aspx`.
- `https://p.example.pe/a/(S(x))/(F(y))/b` → `https://p.example.pe/a/b`.
- `https://p.example.pe/a;JSESSIONID=9F/b?x=1` → `https://p.example.pe/a/b?x=1`.
- `https://p.example.pe/a(1)/b` and `https://p.example.pe/` → unchanged.
- Teaching the first URL stores the second form; teaching `frm.aspx` again makes no new proposal.

Test: `uv run pytest -q tests/unit/test_taught_url.py tests/integration/test_pipeline_teach.py`

## Step 3: Look-alikes are skipped once the host as read answered `[worker]`

Files: `src/invoicebot/net/url_probe.py`, `tests/unit/test_net_url_probe.py`.

- `_follow` also returns whether any HTTP status came back. In `first_answering`, the first URL's host is the host as
  read; once any URL of that host gave a status (redirect included), later URLs with another host are not requested.
  URLs of the same host still are. Nothing else changes.

Test cases (docstrings `R-5.10, D193`):

- `[A/x 404, A/y 200, B/x]` → `Probed(A/y, False)`, B not requested.
- `[A/x 403, B/x 200]` → None, B not requested.
- `[A/x ConnectError, B/x 200]` → `Probed(B/x, False)`.
- `[A/x GuardBlocked, B/x 503]` → `Probed(B/x, True)`.
- `[A/x 301 to a refused URL, B/x 200]` → None, B not requested.
- The existing `[PORTAL 404, OTHER 503]` and `[404, 200]` cases use a `ConnectError` for the first URL instead.

Test: `uv run pytest -q tests/unit/test_net_url_probe.py tests/integration/test_pipeline_portal_check.py`

## Step 4: The probe's report types `[worker]`

Files: `src/invoicebot/fetchers/browser/probe_protocol.py` (new), `tests/unit/test_browser_probe_protocol.py` (new).

Frozen pydantic models, `extra="forbid"`, no Playwright import:

- `ProbeOutcome(StrEnum)`: `ok`, `captcha`, `unmatched_field`, `login_only`, `no_form`, `other_host`, `not_found`,
  `no_download`, `busy`, `guard_blocked`, `technical`.
- `ProbeRequest`: `url`, `invoice: FetchInvoice`, `out_dir: Path`, `max_bytes: MediaMaxBytes`, `page_timeout_ms > 0`.
- `ProbedField`: `selector` (`^(#[A-Za-z][\w-]{0,79}|\[name='[\w$:.-]{1,80}'\])$`, ASCII), `kind` (`text`, `select`,
  `date`), `label` (at most 60 chars of letters, digits, space and `.,()/°-`), `value_name` (one of `ruc_emisor`,
  `tipo`, `serie`, `numero`, `fecha`, `total`, or None), `options` (at most 20 strings, same rule as `label`).
- `ProbeReport`: `outcome`, `form_path` (`^/[A-Za-z0-9._~/-]{0,200}$` or None), `captcha` (`none`, `image`,
  `recaptcha`, `hcaptcha`), `fields` (at most 20), `submit`, `pdf`, `xml` (each a selector as above or None), `files`
  (`HelperFile` tuple), `error_class` (as `HelperOutput`).

Test cases (docstrings `D193, R-11.12`): a full report round-trips through JSON; selector `#a b`, `#x\n`, `[name='a"]`
and a 100-character id → refused; label with `<`, `@`, a backtick or 61 characters → refused; `form_path` with `?`, `(`
or no leading slash → refused; 21 fields and 21 options → refused; an unknown key and an unknown outcome → refused.

Test: `uv run pytest -q tests/unit/test_browser_probe_protocol.py`

## Step 5: Match form fields to invoice values `[worker:high: reasoning: label heuristics need care on real Peruvian forms]`

Files: `src/invoicebot/fetchers/browser/field_match.py` (new), `tests/unit/test_browser_field_match.py` (new).

Pure functions, no Playwright. `value_name(label: str, html_name: str) -> str | None`: accents and case dropped, the
label first, then the name; at most one value per call. Keywords: `ruc_emisor` (`ruc`, `emisor`), `serie` (`serie`),
`numero` (`correlativo`, `numero`, `nro`, `folio`), `tipo` (`tipo`), `fecha` (`fecha`, `emision`), `total` (`monto`,
`importe`, `total`). Whole words, or a CamelCase part of the name. Order when several match: `tipo`, `serie`, `fecha`,
`total`, `numero`, `ruc_emisor`. A label with `captcha`, `codigo`, `usuario`, `clave`, `cliente`, `receptor` or
`adquirente` → None.
`tipo_option(options, tipo) -> str | None`: the one option matching `01` factura or `03` boleta; None when zero or two
match (`FACTURA DE EXPORTACION` is not a factura; `NOTA DE CREDITO` is neither). `date_text(fecha, placeholder)`:
`yyyy-mm-dd`, `dd/mm/yyyy` and `dd-mm-yyyy` by placeholder (any case), ISO for an `input[type=date]` (`""` placeholder).

Test cases (docstrings `D193`):

- Labels `Serie`, `N° Correlativo`, `Tipo de Documento`, `Fecha de Emisión`, `Monto Total`, `RUC del Emisor` → `serie`,
  `numero`, `tipo`, `fecha`, `total`, `ruc_emisor`.
- Empty label with `txtSerie`, `txtCorrelativo`, `cboTipoDoc`, `txtFecha`, `txtMonto`, `cboEmisor` → the same names.
- `RUC del Cliente`, `Código Captcha`, `Usuario`, `Comentario` → None. `Número de Serie` → `serie`.
- Options `[Seleccione, FACTURA ELECTRONICA, BOLETA DE VENTA]` with `01` and `03` → the second and the third; two
  facturas → None; only `NOTA DE CREDITO` → None; `[FACTURA, FACTURA DE EXPORTACION]` with `01` → `FACTURA`.
- 2026-11-05 with `dd/mm/yyyy`, `DD-MM-YYYY`, `yyyy-mm-dd`, `""` → `05/11/2026`, `05-11-2026`, `2026-11-05`,
  `2026-11-05`; with `fecha` as placeholder → `05/11/2026`.

Test: `uv run pytest -q tests/unit/test_browser_field_match.py`

## Step 6: `browse` serves any result type `[worker]`

Files: `src/invoicebot/fetchers/browser/session.py`, `src/invoicebot/fetchers/browser/__main__.py`,
`src/invoicebot/fetchers/browser/sol.py`, `tests/unit/test_browser_session.py`.

- `@dataclass(frozen=True) class BrowseOptions: http_hosts: frozenset[str]; allow_images: bool`; `portal_options(portal)`
  builds it. `browse[T](options, page_timeout_ms, work, *, chromium_sandbox) -> tuple[T, GuardReason | None]` returns
  the work's result and the proxy's block; the two callers apply `guarded_result`. `launch_options` and `_launch_args`
  take `allow_images`. `emit` takes any `BaseModel`. No behavior change.

Test cases: existing tests pass, adapted to the new arguments; `portal_options` for an `http` portal and for a captcha
portal gives the same hosts and image setting as today (docstring `R-5.10, D193`).

Test: `uv run pytest -q tests/unit/test_browser_session.py tests/unit/test_browser_main.py tests/unit/test_browser_sol.py tests/integration/test_browser_helper.py && uv run mypy src`

## Step 7: Reach and read the form `[worker]`

Files: `src/invoicebot/fetchers/browser/probe_form.py` (new), `tests/integration/test_browser_probe_form.py` (new).

- `async def read_form(page, url) -> FormRead` (frozen dataclass: `outcome: ProbeOutcome | None`, `form_path`,
  `captcha`, `fields`, `submit`): `goto(url)`; 429 or 5xx → `busy`; another non-2xx → `technical`. Then, at most 3
  times, when the page has no form: click the first visible link or button whose text has `comprobante`, `consulta`,
  `consultar`, `sin registro` or `autenticacion datos` (accents and case dropped) and no `sesion`, `registr` (except
  `sin registro`), `eliminar`, `pagar`; a link counts only when its `href` stays on the host or its subdomains.
- A form is a set of at least 2 visible inputs or selects with a matched name (step 5). A visible password field with
  fewer than 2 matches is `login_only`; a form that also has a password field is `login_only` too. The page's host not
  being the approved host or its subdomain → `other_host`. Otherwise `no_form` after 3 clicks.
- The fields' labels come from `label[for]`, the enclosing `label`, `aria-label`, `placeholder`, else the cell before;
  trimmed to the report's characters. A selector is `#id` or `[name=...]`, and must match exactly one element, else the
  field is left out. Captcha: `iframe` to `google.com/recaptcha` or `hcaptcha.com` → its kind; an `img` or input whose
  id, name or src has `captcha` → `image`. `submit`: the form's only visible submit button or the one whose text has
  `buscar`, `consultar` or `ver`.

Test cases with pages routed by Playwright, as `test_browser_form.py` does (docstrings `D193`):

- A direct form with 5 fields → `outcome` None, the 5 fields in order with their value names, the submit selector.
- Landing link, then a menu button, then the form → found, with the form's path and no query.
- A fourth click would be needed → `no_form`. A "Pagar" button only → `no_form` and it is not clicked.
- User and password only → `login_only`. Form with a password field → `login_only`.
- A redirect to another host → `other_host`; a link to another host → not clicked.
- Status 503 → `busy`; 404 → `technical`. Image captcha, a reCAPTCHA iframe, an hCaptcha iframe → that captcha kind.
- A label with `<b>@x</b>` text → cleaned; duplicate ids → that field left out.

Test: `uv run pytest -q tests/integration/test_browser_probe_form.py`

## Step 8: Fill the form once and download `[worker]`

Files: `src/invoicebot/fetchers/browser/probe_fill.py` (new), `tests/integration/test_browser_probe_fill.py` (new).

- `async def probe(page, request) -> ProbeReport`: `read_form`; stop with its outcome, with `captcha`, or with
  `unmatched_field` when a visible `required` field has no value name, a `tipo` select has no `tipo_option`, or `submit`
  is missing. A select with one real option is left as it is. While filling, every non-`GET` request to a host other
  than the approved one or its subdomains is aborted. Fill, submit once, wait for a visible link or button whose text
  or `href` has `pdf` or `xml`, or for a text `no se encontr` / `no existe` → `not_found`; neither → `no_download`.
- Save `pdf` and `xml` to `out_dir`, checking the size limit (`max_bytes`) as `form.py` does; move its `_sha256`
  and size check into a shared function only if used as is. Result selectors follow step 4's rule, else None. Every
  `PlaywrightTimeoutError` is `busy` before the form is found and `no_download` after; any other `PlaywrightError` is
  `technical`. A non-`ok` report removes its files.

Test cases (docstrings `D193`): PDF and XML → `ok`, both files, hashes match; only PDF → `ok` with one file; not-found
text → `not_found`; a page with nothing → `no_download`; image captcha → `captcha` and the server saw no POST and no
typed value; unmatched required field → `unmatched_field`, no POST; a form posting to another host → nothing sent
there, `no_download`; exactly one submit request in every case; PDF over the limit → `technical`, folder empty; no
invoice value appears anywhere in the report's JSON in each of these.

Test: `uv run pytest -q tests/integration/test_browser_probe_fill.py`

## Step 9: The probe's helper entry `[worker]`

Files: `src/invoicebot/fetchers/browser/probe.py` (new), `tests/unit/test_browser_probe.py` (new),
`tests/integration/test_browser_helper.py`.

- Modeled on `browser/sol.py`: reads `ProbeRequest`, limits resources, `browse` with no `http` host and images off,
  prints one `ProbeReport` line. A block of a navigated host turns `technical`, `busy` and `no_download` into
  `guard_blocked`. Bad request → `technical`; any exception → `technical` with its `error_class`, only the class name on
  stderr, exit 0.

Test cases (docstrings `D193, R-9.16`): bad JSON, oversized stdin and a bad argument → `technical`, exit 0; an exception
whose message holds page text → only the class name in stdout and stderr; the block promotes the three outcomes and not
`captcha`; in the integration file, a real run against `http://127.0.0.1` → `guard_blocked` or `technical`, exit 0.

Test: `uv run pytest -q tests/unit/test_browser_probe.py tests/integration/test_browser_helper.py`

## Step 10: The bot's side of the probe helper `[worker]`

Files: `src/invoicebot/fetchers/portal_probe.py` (new), `src/invoicebot/fetchers/portal.py`,
`src/invoicebot/fetchers/sunat_sol.py`, `tests/integration/test_fetchers_portal_probe.py` (new).

- `run_browser_job` becomes generic over the output model, taking a small frozen `BrowserJob` (command, payload,
  timeout, name, out_dir, model) so its arguments stay under the lint cap; both callers adapt.
- `PortalProber(max_bytes, timeout_s, *, helper_command)` with `probe(url, invoice, out_dir) -> Probe` (`report`,
  `files: tuple[FetchedFile, ...]`). A helper timeout → `busy`; a failed or invalid helper → `technical`. With an XML,
  the files pass `result_from` (its failure → outcome `not_found`, files deleted). A PDF alone is checked for path,
  size, hash and `is_pdf` only; step 12 checks its content. `sandbox.check_no_parent_dir` holds.

Test cases with a stub helper command (docstrings `D193, R-9.2`): `ok` with a matching XML → its files; mismatching XML
→ no files, outcome `not_found`; a path outside `out_dir` → `technical`, that file untouched; a PDF that is not a PDF
→ `technical`, deleted; a sleeping helper → `busy`; non-JSON and exit 1 → `technical`; existing portal and SOL tests pass.

Test: `uv run pytest -q tests/integration/test_fetchers_portal_probe.py tests/integration/test_fetchers_portal.py && uv run mypy src && uv run lint-imports`

## Step 11: The `portal_probes` table and store `[worker]`

Files: `migrations/versions/0017_portal_probes.py` (new), `src/invoicebot/store/portal_probes.py` (new),
`tests/integration/test_store_portal_probes.py` (new).

- Table: `ruc_emisor BIGINT PRIMARY KEY`, `link_version INTEGER NOT NULL`, `invoice_id INTEGER REFERENCES invoices(id) ON
DELETE SET NULL`, `state TEXT NOT NULL CHECK (state IN ('queued', 'done'))`, `tries INTEGER NOT NULL DEFAULT 0`,
  `outcome TEXT`, `report_json JSONB`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- `start(conn, ruc, version, invoice_id)` (replaces an older row, `queued`, tries 0), `get`, `get_for_invoice(conn,
invoice_id)` (its `queued` row), `count_try(conn, ruc, version) -> int`, `finish(conn, ruc, version, outcome, report)`
  (False when the version moved or the row is `done`).

Test cases (docstrings `D193`): start, get, count_try twice → 2, finish → `done` with the report; a second `finish` →
False; `finish` with an older version → False and unchanged; `start` with a newer version resets; deleting the invoice
leaves the row; upgrade and downgrade run.

Test: `uv run pytest -q tests/integration/test_store_portal_probes.py`

## Step 12: A lone PDF must be this invoice's `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/probe_files.py` (new),
`tests/integration/test_probe_files.py` (new).

- Make `extract._run_helper` callable from another module (`run_extract_helper`, same arguments in a frozen dataclass if
  lint needs it); no behavior change. `pdf_matches(path, invoice, reader) -> bool` in `probe_files.py`, where `reader`
  is an async callable returning a `Reading` or None: true only when RUC, serie, número and total match by
  `matches_invoice(reading, invoice_reading(invoice))`. A failed read is False.

Test cases (docstrings `D193, R-5.6`): all four equal → True; a different total, número or RUC → False each; a missing
total → False; the reader raising its helper error or returning None → False; existing extract tests pass.

Test: `uv run pytest -q tests/integration/test_probe_files.py tests/integration/test_pipeline_extract.py && uv run mypy src`

## Step 13: The draft YAML and the issue comment `[worker]`

Files: `src/invoicebot/github/probe_text.py` (new), `src/invoicebot/github/transport.py`,
`tests/unit/test_github_probe_text.py` (new), `tests/unit/test_github_transport.py`.

- `render_probe(url: str, report: ProbeReport) -> str | None`: the outcome, the field table (label, kind, value name or
  `sin dato`), and a fenced `yaml` draft made with the YAML serializer and parsed back (R-11.12; not equal → None):
  `url` (`https://host/path` through the existing `_safe_url`, made public), `adapter: generic_form`, `captcha`,
  `status: todo`, `fields` (templates `{serie}`, `{numero}`, `{fecha:%d/%m/%Y}`, `{total:.2f}`, `{ruc_emisor}`; a
  select is `{select: "{tipo_label}"}`), `submit`, `downloads`. The draft loads as `Portal` with a key added. Labels
  are put in the table with `|`, backticks and `@` removed.
- `transport.py`: op `probe_report` with `fingerprint`, `url`, `report`; it finds the issue as `comment` does and posts
  it. An invalid report or None → `PermanentSendError`.

Test cases (docstrings `R-11.12, D193`): a full report → the exact text; a captcha report with no submit → `captcha:
image`, no `submit` key; a URL with a query or a session → reduced or refused (None); a label with `|` → removed; the
draft parses as `Portal`; the transport posts one comment; an issue still being created → `SendLater` as `comment`; an
invalid report → permanent.

Test: `uv run pytest -q tests/unit/test_github_probe_text.py tests/unit/test_github_transport.py && uv run lint-imports`

## Step 14: Hold a new portal issue and release it `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `src/invoicebot/pipeline/gaps.py`, `tests/integration/test_pipeline_gaps.py`,
`tests/integration/test_outbox.py`.

- `outbox.add` gets `delay: timedelta | None = None` (`run_after = now() + delay`). `outbox.release_issue(conn,
fingerprint) -> int` sets `run_after = now()` on the pending `create_issue` row of that fingerprint with `attempts = 0`
  and no `last_error`. `PORTAL_ISSUE_HOLD = timedelta(hours=12)` in `gaps.py`: a `PORTAL_MISSING` `create_issue` row
  waits that long when `gaps.teach_via_chat` is on and no `portal_probes` row of the RUC is `done`; every other kind
  stays immediate. With a `done` probe, the new `create_issue` row is followed by its `probe_report` row (a step 13
  op, which waits for the issue as a comment does), so a gap raised after the probe still gets the report.

Test cases (docstrings `R-11.5, D193`): first raise with teach on → `run_after` 12 h ahead; teach off → now; a `done`
probe → now, plus one `probe_report` row; `PORTAL_BROKEN` → now; `release_issue` → now, returns 1; again for a sent row → 0; a row that failed once
keeps its backoff.

Test: `uv run pytest -q tests/integration/test_pipeline_gaps.py tests/integration/test_outbox.py`

## Step 15: The `portal_probe` job `[worker]`

Files: `src/invoicebot/pipeline/portal_probe.py` (new), `src/invoicebot/pipeline/steps.py`,
`tests/integration/test_pipeline_portal_probe.py` (new).

- `make_portal_probe_handler(deps: ProbeDeps)` (pool, cfg, prober port, PDF reader, data_dir). Ends quietly when there is
  no `queued` probe for the invoice, the link is not `approved` at that version, or the invoice left
  `FETCHING_DOCS`/`MANUAL_REQUIRED` (then `finish` as `technical` and release the issue). Probes
  `without_session(url)` in its own folder under `TMP_DIRECTORY`, no connection held.
- `busy` with tries left (15 min, 1 h, 6 h) → `count_try` and enqueue again with that delay, no message. Otherwise, in
  one transaction with the invoice locked: `finish`; release the issue and, when the gap exists, queue `probe_report`;
  a matching file set that covers `required_docs` → `add_file`, state → `DELIVERING`, enqueue `DELIVER_STEP`, as
  `uploads._commit` and `fetch._record_result` do, and `BR-S06_probe_filed`; else `BR-S06_probe_form` when a form was
  read, or `BR-S06_probe_failed`. Files not stored are deleted, also on an exception.

Test cases with a fake prober (docstrings `D193`; each asserts the messages, the issue row and the folder):

- `ok` with matching PDF, flow needs `[pdf]` → `DELIVERING`, one deliver job, `probe_filed`.
- `ok` with a PDF that does not match → state unchanged, folder gone, `probe_form`.
- `ok` with a PDF, flow needs `[xml, pdf]` → not filed. `captcha`, `unmatched_field`, `login_only` → report queued.
- `busy` four times → jobs at +15 min, +1 h, +6 h, then `probe_failed` once.
- The invoice cancelled while probing → nothing filed, probe `done`, issue released.
- The link replaced (version moved) → nothing written. The job run twice → one message, one deliver job.
- The invoice already `DELIVERING` from another fetcher → nothing filed. A prober exception → folder gone, raised.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_probe.py && uv run mypy src && uv run lint-imports`

## Step 16: [Aprobar] starts the probe `[worker]`

Files: `src/invoicebot/pipeline/teach.py`, `src/invoicebot/store/invoices.py`,
`tests/integration/test_pipeline_teach.py`.

- `newest_waiting_invoice(conn, ruc_emisor) -> int | None` in the store. In `handle_press`, on approval with one:
  `portal_probes.start`, enqueue `PORTAL_PROBE_STEP`, answer `BR-S06_approved_probe`; with none, release the issue and
  answer `BR-S06_approved`. [Rechazar] releases the issue, and so does `dont_know_reply` for the invoice's issuer.
  `_record_hint` stays. `TaughtLink` gains `version`.

Test cases (docstrings `BR-S06, D193`): approval with a `MANUAL_REQUIRED` invoice → one job, one `queued` probe, the new
text; two waiting → the newest; only a `DONE` one → no job, issue released; rejection → no job, issue released; "no sé"
→ issue released; a second press → no second job.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py tests/integration/test_pipeline_portal_offer.py`

## Step 17: Wire the probe and the other-host text `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/pipeline/portal_check.py`, `src/invoicebot/pipeline/portal_offer.py`,
`tests/integration/test_pipeline_portal_check.py`.

- `app.py`: `PORTAL_PROBE_STEP` in `_portal_handlers`, with a `PortalProber` on `browser_command(prefixes,
PROBE_HELPER_COMMAND)` and `cfg.retrieval.portal_timeout_s`. `portal_check._record`: an answering URL whose host differs
  from the first candidate's is offered with `BR-F14_other_host` (rule BR-F14, same buttons).

Test cases (docstrings `BR-F14, D193`): first host gives no answer, second host 200 → `BR-F14_other_host` with both; same
host, other path → BR-F14; [Sí] on it proposes the URL; `_portal_handlers` holds both steps (the carried-over test).

Test: `uv run pytest -q tests/integration/test_pipeline_portal_check.py tests/unit/test_app.py && uv run mypy src`

## Step 18: Live check `[inline: needs the owner's chat]`

Deploy with `deploy/deploy-live.sh`, remove the PEBSA link approved in 9t1, send its photo, teach the link, press
[Aprobar]. Expected: `BR-S06_approved_probe`, then `captcha` (the spike shows an image captcha) and the issue with the
draft and no invoice data; or `busy` with its retries. Then the La Leña site, if the owner
agrees, with one of its real invoices, for the `ok` path. Record the rows in `specs/spikes/portal-probe.md` and cancel the test invoices.

Test: every row passes or is waived by the owner.

## Finish

`py-code-reviewer` and `py-security-auditor` (untrusted pages in the browser, invoice data typed outside, public issue
text). Move this plan to `docs/archive/`.
