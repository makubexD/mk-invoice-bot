# Plan: MVP phase 9t4a · portal-link

Roadmap: `docs/plan-mvp.md`, row 9t4 (split by this plan). Branch `feat/portal-link`, from `main` after 9z11's merge.

What live #50 showed (2026-11-27, flow A, PEBSA): the issuer has an approved taught link (v1) whose probe ended in
`captcha`, but no portal entry, so `portal_registry` logged `no_portal` and BR-F13 said `• Portal del comercio: no lo
conozco`. BR-F13 also hides [🔗 Enseñar el portal] because a taught link exists. The sender taught the portal and was
told the bot does not know it, with no way to open it.

Owner decisions (2026-11-27):

- 9t4 is done in stages: this quick fix first; then 9t4 `portal-guide`, where a portal that asks for an image captcha
  shows it to the sender (save and resume: the helper saves the session and exits, the sender's answer starts a new
  helper; D82 and the 300 s lease stay; no local OCR, R-5.3's OCR part is deferred); then 9t5 `sunat-api`, the
  official SUNAT API as the fallback, once the owner has checked what the tokens return.
- Flow B works and must not change.

Decision this plan proposes (step 1):

- **D208 A known portal the bot cannot query is shown, not hidden.** (1) When the issuer has no portal entry but an
  approved taught link, the portal bullet in BR-F07 and BR-F13 reads `• Portal del comercio: me lo enseñaste, pero
todavía no sé consultarlo` (`FETCH_TAUGHT_PORTAL_LINE`), not `no lo conozco`. (2) When the portal fetcher's last
  reason is `captcha_unsolvable`, `portal_inactive`, or `no_portal` with an approved taught link, BR-F07 and BR-F13
  carry one D194 link `🔗 Abrir el portal` (`LINK_OPEN_PORTAL`): the portal entry's `url` when it is `https`, else the
  approved taught link's URL; nothing when neither is `https`. Telegram shows it as a URL button, WhatsApp and the
  console as a line. The bot opens nothing itself; the link goes through no request guard because the bot does not
  fetch it. (3) PEBSA gets a store file and a portal file `pebsa` with `captcha: image`, `status: todo`, so its bullet
  is BR-F04 and the link is its URL. 9t4 makes it active.

Out of scope: solving the captcha (9t4), the SUNAT API (9t5), any change to flow B's config, BR-F02 and the
`PORTAL_MISSING` gap.

## Step 1: D208, spec 06 rows and the roadmap split `[inline: spec text and owner decisions]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add the D208 row after D207, with the text above.
- `specs/06-business-rules.md`: in BR-F07's and BR-F13's rows, say that the portal bullet follows D208 (1) and that
  the message carries the D208 (2) link. In BR-F04's row, add "the message carries [🔗 Abrir el portal] (D208)".
- `docs/plan-mvp.md`: row 9t4 becomes three rows: `9t4a` `portal-link` (this plan, D208), `9t4` `portal-guide`
  (captcha shown to the sender, save and resume, no OCR; reopens D99), `9t5` `sunat-api` (the official SUNAT API as
  the fallback; starts with a spike on what the owner's tokens return).

Test: `npx prettier --check specs/05-open-questions.md specs/06-business-rules.md docs/plan-mvp.md`.

## Step 2: the taught-portal bullet `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/manual_message.py`,
`src/invoicebot/pipeline/fetch.py`, `tests/unit/test_manual_message.py`.

- `messages_es.py`: next to `FETCH_NO_PORTAL_LINE`, add
  `FETCH_TAUGHT_PORTAL_LINE = "• Portal del comercio: me lo enseñaste, pero todavía no sé consultarlo"` with a
  `#:` comment citing D208.
- `manual_message.py`: add a frozen dataclass `StoreFacts` with `directory: StoreDirectory`, `taught_portal: bool`
  (the issuer has an approved taught link) and `portal_broken: bool = False`. `manual_message(reading, flow, attempts,
facts, gap_line="")` and `docs_needed_text(invoice_id, reading, flow, attempts, facts, looks)` take `facts` in place
  of `directory` (and `portal_broken`). `fetcher_lines(attempts, store, portal, facts)` passes it to `_reason_line`,
  which returns `FETCH_TAUGHT_PORTAL_LINE` for `NO_PORTAL` when `facts.taught_portal`, else the fixed line as now.
  Update the docstrings.
- `fetch.py`: `_wait_notice` and `_exhausted` build `StoreFacts` with `taught_portal` from a new helper
  `_has_approved_link(conn, reading)` (`taught_links.get`, state `approved`; False without a `ruc_emisor`) and
  `portal_broken` as now. No other change.
- `test_manual_message.py`: update the existing calls to `StoreFacts`, and add: (a) `NO_PORTAL` with
  `taught_portal=True` gives `FETCH_TAUGHT_PORTAL_LINE` in BR-F07; (b) the same in BR-F13; (c) `NO_PORTAL` with
  `taught_portal=False` still gives `FETCH_NO_PORTAL_LINE`. Docstrings start with `D208`.

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/integration/test_pipeline_fetch.py`.

## Step 3: the open-portal link `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/portal_link.py` (new),
`tests/unit/test_portal_link.py` (new).

- `messages_es.py`: `LINK_OPEN_PORTAL = "🔗 Abrir el portal"` with a `#:` comment citing D208.
- `portal_link.py` (module docstring: "Choose the link BR-F07 and BR-F13 offer to a portal the bot cannot query
  (D208)."): `open_portal_link(attempts: Sequence[Attempt], portal: Portal | None, taught_url: str | None) -> Link |
None`. It reads the last `portal_registry` attempt's reason (`PORTAL_FETCHER` from `manual_message.py`); for
  `CAPTCHA_UNSOLVABLE` or `PORTAL_INACTIVE` it uses `portal.url`, and for `NO_PORTAL` it uses `taught_url`; when that
  URL is missing or not `https`, or the reason is anything else, it returns None. `taught_url` is the approved link's
  URL, or None. A URL that `Link` rejects returns None (catch `pydantic.ValidationError` only).
- `test_portal_link.py`, one test each, docstrings start with `D208`: (a) `captcha_unsolvable` + https portal URL →
  link with that URL and `LINK_OPEN_PORTAL`; (b) `portal_inactive` + https URL → link; (c) `captcha_unsolvable` +
  `http` URL (`allow_http` portal) → None; (d) `no_portal` + https taught URL → link with the taught URL; (e)
  `no_portal` + no taught URL → None; (f) `captcha_unsolvable` with a portal and a taught URL → the portal's URL; (g)
  last portal reason `not_found` → None; (h) no `portal_registry` attempt → None; (i) two portal attempts, the last
  `captcha_unsolvable` after an earlier `technical` → link. Use synthetic hosts (`portal.ejemplo.pe`).

Test: `uv run pytest -q tests/unit/test_portal_link.py`.

## Step 4: BR-F07 and BR-F13 carry the link `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- `fetch.py`: `_has_approved_link` from step 2 becomes `_approved_link_url(conn, reading) -> str | None` (the
  approved link's URL, else None); `StoreFacts.taught_portal` is `url is not None`. `_notify` takes
  `links: Sequence[Link] = ()` and, when it is not empty, builds the message with `outbound` and `queue_outbound`
  (same addressee, rule and buttons) instead of `queue_message`. `_wait_notice` (the BR-F13 branch only) and
  `_exhausted` pass `open_portal_link(inputs.attempts, portal_for(ruc, directory), url)` as the one link when it is
  not None. Everything else is unchanged.
- `test_pipeline_fetch.py`, docstrings start with `D208`, using the file's existing fixtures and a synthetic issuer:
  (a) an issuer whose portal has `captcha: image` and an https `url` ends in `MANUAL_REQUIRED`: the BR-F07 outbox row
  has the BR-F04 bullet and one link `🔗 Abrir el portal` with the portal's URL; (b) an issuer with no portal entry
  and an approved taught link, in a flow with an inbox: the BR-F13 row has `FETCH_TAUGHT_PORTAL_LINE` and the link
  with the taught URL, and no [🔗 Enseñar el portal] button; (c) an issuer with no portal and no taught link: BR-F07
  has `FETCH_NO_PORTAL_LINE` and no link; (d) a proposed (not approved) taught link: no link, `no lo conozco`.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`.

## Step 5: PEBSA's store and portal files `[inline: real store data and the live host]`

Files: `config/stores/<pebsa-ruc>.yaml` (new), `config/portals/pebsa.yaml` (new).

- The RUC and razón social come from the live `taught_links` row and the invoice's reading, never typed into the plan
  or a commit message. Store file as La Leña's store file with `portal: pebsa` and `comercio: PEBSA`.
- `pebsa.yaml`: `url:` the approved taught link's base `https` URL (session id cleaned, as the probe stored it),
  `adapter: generic_form`, `captcha: image`, `status: todo`, and `notes:` listing the probe's fields (emisor, serie,
  correlativo, tipo, fecha, monto, image captcha) as `lalucha-ddns.yaml` does. No `fields` yet: 9t4 adds them.
- Before any change on the host: the local tag `live-2026-11-27-pre-pebsa` with a summary and no RUCs or invoice ids
  (pushing it waits for the owner's answer). Then copy both files into the live config's `stores/` and `portals/` and
  reload (`deploy/deploy-live.sh --config`), after the merged image is deployed.

Test: `uv run pytest -q tests/unit/test_config_checks.py tests/unit/test_check_fixtures.py` and
the validation `deploy/deploy-live.sh --config` runs before it installs; the CI RUC check
allows the store file.

## Step 6: live check and record `[inline: the live host and the owner's chat]`

Files: `specs/spikes/portal-link.md` (new).

- Deploy the merged image (`./deploy/deploy-live.sh <tag>`), install step 5's files, reload.
- The owner sends a PEBSA receipt in Telegram (or forgets and resends #50's photo). Pass: the fetch reaches BR-F13 or
  BR-F07 with `• Portal …: captcha que no puedo resolver` and a [🔗 Abrir el portal] URL button that opens the PEBSA
  form; flow B is untouched (its next receipt files as before).
- Record the rows (pass, fail, waived) with no RUCs, names or addresses in `specs/spikes/portal-link.md`, committed via
  a PR.

Test: `npx prettier --check specs/spikes/portal-link.md`.
