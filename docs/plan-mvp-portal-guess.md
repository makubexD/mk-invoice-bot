# Plan: MVP phase 9v · portal-guess

Roadmap: `docs/plan-mvp.md`, row 9v. The owner's note 2 in `specs/spikes/guided-docs.md` and notes 3 and 4 in
`specs/spikes/message-review.md` (2026-11-24): the bot should propose the portal URL printed on the ticket, and the
portal question should take guided answers, not free text.

What the code does today:

- **The printed URL is lost.** `extract/ocr_parse._read_printed_url` finds a URL only in the full-page OCR text. A
  Telegram photo arrives at about 600×1280, so the line under `ESTE COMPROBANTE PODRÁ SER CONSULTADO EN:` is about
  8 px tall and reads `hiips:/Is etomiprobantes, aio`. A re-read of only that strip, 3× upscaled, `eng`, `--psm 7`,
  gave `hitps.//sy.ecomprobantes pe/pehsa` (real: `https://sv.ecomprobantes.pe/pebsa`). `parse_tsv` drops the word
  boxes, so nothing can find the strip today.
- **BR-F14** offers `offer_url(reading)` with [Sí] [No] [No sé]; [Sí] recomputes the URL from the stored reading.
- **BR-F02** has only [No sé]. A typed `https` URL goes straight to the owner (BR-S06) with no echo; any other text
  falls through to the edit paths without a word (`teach.answer` returns False).

In scope: the 9v roadmap row. Out of scope: a vision LLM (later opt-in, never flow B); probing the taught portal (9t
`portal-probe`, stays its own phase); offering a guess from the fetch chain's own BR-F02 (it keeps asking, with the new
buttons); store brand names as path candidates (the handler has no store config; the padrón name is enough for the live
case).

Needs from the owner: review of the texts and D191 in step 1; Telegram for step 13.

Decision this plan proposes (step 1):

- **D191 portal guess (9v).**
  - **Strip re-read.** When the full-page OCR finds no printed URL, the photo helper finds the line that matches the
    parsing rule `printed_url_anchor` (a word starting `CONSULT` and the line ending in `EN` or `EN:`), crops the band
    under it, upscales it and reads it again as one line. The result, reduced to URL characters and capped at 120, is
    stored as `Reading.url_strip`. It is not raw OCR text in the R-9.16 sense: it holds only URL characters from the
    line under the anchor, never a name, an amount or a RUC line.
  - **Repair, outside the helper.** `pipeline/portal_guess.repair_url` turns the strip into a URL: a garbled scheme
    becomes `https://`, spaces and commas in the host become dots, a host close to an approved taught link's host
    (ratio ≥ 0.85) takes that host, the last path segment close to a word of the issuer's name (ratio ≥ 0.75) takes
    that word, and the top-level domain must be one of `pe`, `com`, `net`, `org`. SUNAT hosts and `xn--` labels are
    never offered. The bot never fetches the guess (BR-F14 stays as it is).
  - **Guided answers.** BR-F14 offers one stored candidate with [✅ Sí, es este] [✏️ Corregir] [No sé]. BR-F02 asks
    with [✏️ Lo escribo] [No sé]. [✏️ Corregir] and [✏️ Lo escribo] send BR-F15, which asks for the address. Any text
    while BR-F15 is the newest question answers it: a valid link is echoed back as BR-F14 (the same [Sí] [Corregir]
    [No sé]); anything else gets `BR-F15_retry` and BR-F15 stays the question. A URL typed straight after BR-F02 is
    still taken, but echoed as BR-F14 first, not proposed at once. [Sí] proposes the stored candidate through BR-S06.
    `/portal <ruc> <url>` is unchanged.
  - **One candidate per invoice.** The offered URL is stored in `portal_candidates` (migration 0016), so [Sí]
    teaches exactly what the sender saw.
  - **Not folded into 9t.** 9t probes a taught portal after the owner approves it; 9v only finds and confirms the
    address. They share nothing but the taught link.

## Step 1: D191, spec texts, messages and roadmap row `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/03-design.md`, `specs/02-requirements.md`,
`src/invoicebot/rules/messages_es.py`, `src/invoicebot/rules/catalog.py`, `docs/plan-mvp.md`.

- D191 as above in spec 05, after D190.
- Spec 06: BR-F02's row and text, BR-F14's row (buttons, stored candidate), a new BR-F15 row (sent on [✏️ Corregir]
  or [✏️ Lo escribo]; a text answers it; `BR-F15_retry`), and the "BR-F02's answers" paragraph rewritten for the
  guided flow.
- Spec 03: `Reading.url_strip` in the extraction section, the strip re-read in the photo helper, `portal_candidates`
  in the schema list.
- Spec 02 R-11.4: the sender's answer is confirmed before it goes to the owner.
- `messages_es.py`:
  - `BR-F02`: `No conozco el portal de descarga de {razon_social} (RUC {ruc_emisor}). Suele estar impreso en el
ticket, debajo de "Consulte su comprobante en…". Si lo tienes, toca [✏️ Lo escribo]; si no, toca [No sé].`
  - `BR-F14` unchanged: `¿Es este el portal de {razon_social}?\n{url}`.
  - `BR-F15`: `Escríbeme la dirección del portal de {razon_social} tal como aparece en el ticket, por ejemplo
https://portal.ejemplo.pe/tienda`.
  - `BR-F15_retry`: `No reconozco eso como la dirección de un portal. Escríbela completa, por ejemplo
https://portal.ejemplo.pe/tienda, o toca [No sé].`
  - Buttons: `BUTTON_THIS_ONE = "✅ Sí, es este"`, `BUTTON_WRITE_PORTAL = "✏️ Lo escribo"`; BR-F14 reuses
    `BUTTON_FIX` (`✏️ Corregir`). Each title ≤ `BUTTON_TITLE_MAX`.
- `catalog.py`: `"BR-F15": Rule("BR-F15", None, "BR-F15")`.
- Roadmap row 9v: `plan: docs/plan-mvp-portal-guess.md, D191`.

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_messages_es.py tests/unit/test_docs.py`

## Step 2: Strip re-read spike on the PEBSA photo `[inline: the owner's private photo, aggregates only]`

Files: `specs/spikes/portal-guess.md` (new).

With a throwaway script outside the repo, on the PEBSA ticket photo the live stack received (#25), and on the six
photos in the private samples folder that print a `CONSULT… EN` line: run `tesseract --list-langs` in the live image
(`eng` must be there; it comes with `tesseract-ocr`), then compare for the band under the anchor: crop from the
binarized image vs the deskewed gray, upscale 3× vs 4× (`INTER_CUBIC`), `eng` vs `spa`, `--psm 7`. Record per setting
how many characters differ from the printed URL (edit distance), not the URLs of other tickets. If a setting other
than binarized, 3×, `eng`, `--psm 7`, band height 2.5× the anchor's height wins, edit step 6's constants in this plan
and commit `docs: plan portal-guess edits`.

Test: the spike file exists and names the chosen setting; `uv run pytest -q tests/unit/test_docs.py`.

## Step 3: OCR words keep their boxes, and `deskew` is public `[worker]`

Files: `src/invoicebot/extract/ocr.py`, `tests/unit/test_extract_ocr.py`.

- Add `@dataclass(frozen=True) class OcrBox` with `left: int`, `top: int`, `width: int`, `height: int`, and the
  property `bottom -> int` (`top + height`).
- `OcrWord` gains `box: OcrBox | None = None` (last field, so `extract/pdf.text_to_ocr` keeps working unchanged).
- `parse_tsv` fills `box` from the TSV columns `left`, `top`, `width`, `height` (indexes 6–9, constants
  `TSV_LEFT`, `TSV_TOP`, `TSV_WIDTH`, `TSV_HEIGHT`); a row whose box columns are not integers is skipped, like a bad
  confidence.
- Split `preprocess`: rename `_deskew` to public `deskew(gray)` and add `binarize(straight)` (CLAHE then the adaptive
  threshold, the current body after the deskew). `preprocess(gray)` stays and returns `binarize(deskew(gray))`, so its
  callers are unchanged. The spike (`specs/spikes/portal-guess.md`) found the strip reads best from the deskewed gray,
  in the same coordinates as the boxes.

Test cases (docstrings start with `D191`):

- A TSV word row with `left=10 top=20 width=30 height=8` → its `OcrWord.box == OcrBox(10, 20, 30, 8)` and
  `box.bottom == 28`.
- A row with `top=x` → skipped, the other words of the line kept.
- `preprocess(gray)` equals `binarize(deskew(gray))` for the existing synthetic image (`np.array_equal`).
- The existing real-Tesseract test still reads its words, and each has a box with `width > 0`.

Test: `uv run pytest -q tests/unit/test_extract_ocr.py tests/unit/test_extract_pdf.py`

## Step 4: The `printed_url_anchor` parsing rule `[worker]`

Files: `src/invoicebot/extract/parsing_rules.py`, `config/parsing_rules.yaml`, `tests/unit/test_extract_ocr_parse.py`.

- `ParsingRules` gains `printed_url_anchor: str`, compiled once at load (`re.IGNORECASE`) into a private attribute
  exposed as the property `anchor_pattern -> re.Pattern[str]`, the same way the amount patterns are compiled. A pattern
  that does not compile fails the load with `ValueError`.
- `config/parsing_rules.yaml`: `printed_url_anchor: '\bconsult\w*\b.*\ben\s*[:;.,]?\s*$'` with a comment: the line
  just above a printed portal address; OCR may read the colon as `;` (D191, spike).

Test cases (docstrings start with `D191`):

- `anchor_pattern` matches `ESTE COMPROBANTE PODRÁ SER CONSULTADO EN:`, `ESTE COMPROSANTE PODRÁ SER CONSULTADO EN;`,
  `Consulte su comprobante en`, `CONSULTADO EN :`.
- It does not match `CONSULTADO EN https://portal.example.pe` (the URL is on the same line) nor `TOTAL EN SOLES`.
- Loading rules whose `printed_url_anchor` is `(` raises `ValueError`.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 5: `Reading.url_strip` `[worker]`

Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/merge.py`, `tests/unit/test_extract_fields.py`,
`tests/unit/test_extract_merge.py`.

- `Reading` gains `url_strip: str | None = None` (last field). Add `MAX_URL_STRIP_CHARS = 120` and
  `is_url_strip(value: object) -> bool`: a `str` of 1 to `MAX_URL_STRIP_CHARS` characters, all in
  `[A-Za-z0-9.:/_~% -]`.
- `reading_to_json` writes `url_strip`; `reading_from_json` accepts an optional `url_strip` key, refuses a value that
  is not None and fails `is_url_strip` with `ValueError("reading field 'url_strip' must be URL characters or null")`,
  and still reads a stored reading without the key.
- `merge` keeps the OCR reading's `url_strip`, like `printed_url`.

Test cases (docstrings start with `D191`):

- Round trip of a reading with `url_strip="hitps.//sy.example pe/tiemda"`.
- A stored reading without `url_strip` loads with None.
- `url_strip` of `"a\nb"`, `""` or 121 characters → `ValueError`.
- `merge(ocr_with_strip, qr_reading).url_strip` is the OCR's.

Test: `uv run pytest -q tests/unit/test_extract_fields.py tests/unit/test_extract_merge.py`

## Step 6: Re-read the strip under the anchor `[worker]`

Files: `src/invoicebot/extract/url_strip.py` (new), `tests/unit/test_extract_url_strip.py` (new).

Module docstring: re-read the line under a `CONSULT… EN` anchor as one line, for the portal guess (D191). It imports
`cv2` and lives in the helper only (the `heavy-in-helper-only` contract already covers `invoicebot.extract`). The
settings come from the spike in `specs/spikes/portal-guess.md`.

- Constants: `STRIP_SCALE = 3`, `STRIP_OFFSETS = (0.0, 0.15, 0.3)` and `STRIP_HEIGHTS = (1.6, 1.8)` (both in anchor
  word heights), `STRIP_LANG = "eng"`, `STRIP_PSM = 7`, `URL_STRIP_JUNK = re.compile(r"[^A-Za-z0-9.:/_~%-]+")`.
  `MAX_URL_STRIP_CHARS` is imported from `extract/fields.py`.
- `@dataclass(frozen=True) class Anchor` with `bottom: int` and `word_height: int`.
- `find_anchor(text: OcrText, pattern: re.Pattern[str]) -> Anchor | None`: the first line whose words, joined by a
  space, match `pattern` with `search`; `bottom` is the median of its words' `box.bottom` and `word_height` the median
  of their `box.height` (`statistics.median_low`). None when no line matches, a matching line has a word without a box,
  or `word_height < 1`.
- `strip_boxes(anchor: Anchor, image_width: int, image_height: int) -> list[OcrBox]`: for each offset, then each
  height: `top = anchor.bottom + round(offset * word_height)`, `height = round(factor * word_height)`, `left = 0`,
  `width = image_width`, clipped to the image; a box whose clipped height is under 1 is left out.
- `clean_strip(raw: str) -> str | None`: replace each run matched by `URL_STRIP_JUNK` by one space, strip, cut to
  `MAX_URL_STRIP_CHARS`; None when empty.
- `read_url_strip(straight: np.ndarray, text: OcrText, rules: ParsingRules, ocr_cfg: Ocr,
command: Sequence[str] = (TESSERACT_COMMAND,)) -> str | None`: `straight` is the deskewed gray image the boxes refer
  to. For each strip box: crop, upscale by `STRIP_SCALE` with `cv2.INTER_CUBIC`, run `ocr.read_text` with
  `ocr_cfg.model_copy(update={"lang": STRIP_LANG, "psm": STRIP_PSM})`. Keep the read whose words have the highest mean
  confidence (ties: the first; a read with no words or `None` is skipped), join its words with spaces and return
  `clean_strip` of it. None when there is no anchor, no box or no read.

Test cases (docstrings start with `D191`):

- `find_anchor`: lines `RUC 20000000001` (boxed), `CONSULTADO EN;` (two boxed words, boxes (10,100,80,20) and
  (95,104,20,22)) → `Anchor(bottom=120, word_height=20)`; no matching line → None; the matching line with one unboxed
  word → None.
- `strip_boxes(Anchor(120, 20), 600, 1280)` → six boxes, the first `OcrBox(0, 120, 600, 32)`, the last
  `OcrBox(0, 126, 600, 36)`; `strip_boxes(Anchor(1275, 20), 600, 1280)` keeps only boxes with height ≥ 1, each ending
  at 1280.
- `clean_strip("hitps.//sy.example pe/tiemda ")` → `hitps.//sy.example pe/tiemda`; `"https://a.pe/x»…"` →
  `https://a.pe/x`; `"«»"` → None; a 300-character input → 120 characters.
- `read_url_strip` with a fake `command` (a script that prints a TSV with one word `hitps.//portal.example`, confidence
  `50`), an image of 600×200 and an `OcrText` whose anchor line is boxed → `hitps.//portal.example`; with no anchor →
  None and the fake is never run.
- With a fake whose output depends on the crop height (for example it reads the PNG size from stdin with
  `cv2.imdecode` in a tiny Python script and prints a higher confidence for the tallest crop), the tallest crop's
  word wins.

Test: `uv run pytest -q tests/unit/test_extract_url_strip.py && uv run lint-imports`

## Step 7: The photo helper re-reads the strip `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `tests/unit/test_helper.py`.

In `_read_photo`: compute `straight = deskew(gray)` and `processed = binarize(straight)` in place of
`preprocess(gray)` (one deskew, inside the existing `try`). After `parse_ocr`: when the OCR reading exists and its
`printed_url` is None, call `read_url_strip(straight, text, rules, ocr_cfg)` and, when it returns a string, replace
the OCR reading with `dataclasses.replace(ocr_reading, url_strip=strip)` before `merge`. A `MemoryError` or
`cv2.error` in the re-read keeps the reading without a strip. PDFs are not changed.

Test cases (docstrings start with `D191`), driving `_read_photo` with a synthetic image and a fake Tesseract the
existing helper tests already use, or monkeypatching `read_url_strip`:

- OCR reading with no printed URL, re-read returns `hitps.//portal.example` → the payload's reading has that
  `url_strip`.
- OCR reading with a printed URL → `read_url_strip` is not called and `url_strip` is null.
- Re-read raises `cv2.error` → the payload is the reading without `url_strip`.

Test: `uv run pytest -q tests/unit/test_helper.py`

## Step 8: `repair_url` `[worker]`

Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/ocr_parse.py`,
`src/invoicebot/pipeline/portal_guess.py` (new), `tests/unit/test_pipeline_portal_guess.py` (new).

- Move `_is_printed_host` and its constants (`PUNYCODE_PREFIX`, `HOST_MIN_LABELS`, `TLD_MIN_CHARS`, `TLD_MAX_CHARS`)
  from `ocr_parse.py` to `fields.py` as public `is_printed_host`; `ocr_parse` imports it. No behavior change there.
- `pipeline/portal_guess.py` (docstring: turn the strip under a ticket's `CONSULT… EN` line into the URL BR-F14
  offers, D191), pure functions, `difflib.SequenceMatcher` for ratios:
  `repair_url(strip: str, issuer_name: str | None, known_hosts: frozenset[str]) -> str | None`:
  1. Lowercase and strip. Remove a leading garbled scheme matching `^h\S{2,5}?[:;.,]*[/\\|]+` (so `hitps.//`,
     `https://`, `http:/` all go).
  2. Split at the first `/`: host part, path part (may be empty).
  3. Host: replace each run of `[\s,;]+` by `.`, drop characters outside `[a-z0-9.-]`, collapse repeated dots, strip
     dots at the ends.
  4. If some known host has ratio ≥ `HOST_MATCH_MIN_RATIO = 0.85` with the host, take the best one (ties: the
     alphabetically first).
  5. Path: split on `/`, clean each segment to `[a-z0-9._~-]`, drop empty ones. If `issuer_name` is given, its words
     are the runs of `[a-z0-9]` in its lowercase form with at least `NAME_WORD_MIN_CHARS = 3` characters; if the
     last segment is not one of them and some word has ratio ≥ `NAME_MATCH_MIN_RATIO = 0.75` with it, replace it
     by the best word (ties: the first in the name).
  6. URL = `https://` + host + (`/` + segments joined by `/` when there are any).
  7. Return it only when `fields.is_printed_host(host)` holds, the last label is in `ACCEPTED_TLDS = frozenset({"pe",
"com", "net", "org"})`, `sunat_origins.is_sunat_host(host)` is false and `fields.is_printed_url(url)` holds;
     else None.

Test cases (docstrings start with `D191`):

- `hitps.//sy.example pe/tiemda`, `TIENDA SOL S.A.C.`, ∅ → `https://sy.example.pe/tienda`.
- Same with `{"sv.example.pe"}` → `https://sv.example.pe/tienda`.
- Same with `{"other.example.com"}` → `https://sy.example.pe/tienda` (too far).
- `https://portal.example.pe/tienda`, `TIENDA SOL S.A.C.`, ∅ → unchanged.
- `portal.example.pe`, None, ∅ → `https://portal.example.pe`.
- `hitps.//portal.example pe/abc123`, `TIENDA SOL S.A.C.`, ∅ → `https://portal.example.pe/abc123`.
- `hitps.//portal.example pe/tiemda`, None, ∅ → `https://portal.example.pe/tiemda`.
- `hiips:/is etomi, aio`, None, ∅ → None (top level `aio`).
- `https://e-consulta.sunat.gob.pe/x` → None; `https://xn--prtal-9ta.pe/x` → None; `   ` → None.
- A strip whose repaired URL is over `MAX_PRINTED_URL_CHARS` → None.
- The existing `ocr_parse` printed-URL tests still pass.

Test: `uv run pytest -q tests/unit/test_pipeline_portal_guess.py tests/unit/test_extract_ocr_parse.py && uv run lint-imports`

## Step 9: `portal_candidates` and the approved hosts `[worker]`

Files: `migrations/versions/0016_portal_candidates.py` (new), `src/invoicebot/store/portal_candidates.py` (new),
`src/invoicebot/store/taught_links.py`, `tests/integration/test_store_portal_candidates.py` (new).

- Migration 0016 (revises 0015, expand only, style of 0015): `CREATE TABLE portal_candidates (invoice_id INTEGER
PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE, url TEXT NOT NULL CHECK (char_length(url) <= 300),
updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`; downgrade drops it.
- `store/portal_candidates.py` (docstring: the one URL BR-F14 last offered per invoice, D191): `save(conn, invoice_id,
url) -> None` upserts and sets `updated_at = now()`; `get(conn, invoice_id) -> str | None`. Neither commits.
- `taught_links.approved_hosts(conn) -> frozenset[str]`: the lowercase hostnames of every `approved` link
  (`urllib.parse.urlsplit(url).hostname`, skipping None).

Test cases (docstrings start with `D191`):

- `save` then `get` → the URL; a second `save` replaces it; `get` of an invoice with none → None.
- Deleting the invoice deletes its candidate.
- `approved_hosts` with one approved, one proposed and one rejected link → only the approved host.

Test: `uv run pytest -q tests/integration/test_store_portal_candidates.py`

## Step 10: Guided buttons and [✏️ Corregir] `[worker]`

Files: `src/invoicebot/pipeline/questions.py`, `src/invoicebot/pipeline/portal_offer.py`,
`src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_portal_offer.py`.

- `questions.py`: `BUTTON_FIX_SUFFIX = "fix"`; `offer_buttons(invoice_id) -> list[Button]` → [✅ Sí, es este]
  (`u<id>:yes`), [✏️ Corregir] (`u<id>:fix`), [No sé] (`u<id>:dk`); `ask_portal_buttons(invoice_id) ->
list[Button]` → [✏️ Lo escribo] (`u<id>:fix`), [No sé].
- `portal_offer.py`: `_offer_buttons` is removed for `offer_buttons`; `_ask_br_f02` uses `ask_portal_buttons`.
  The press pattern takes `yes|fix|dk` (`no` is gone). A `fix` press counts when the newest question is the
  invoice's BR-F14 or BR-F02 and queues BR-F15 (`razon_social` or `UNKNOWN_ISSUER`) with rule `BR-F15` and the
  [No sé] button. `dk` also counts when the newest question is BR-F15. `RULE_WRITE_PORTAL = "BR-F15"`.
- `fetch.py`: the chain-end BR-F02 carries `ask_portal_buttons`.

Test cases (docstrings start with `D191`):

- BR-F14 is sent with exactly the three offer buttons, in that order.
- BR-F02 (from [🔗] with no URL) is sent with [✏️ Lo escribo] [No sé].
- [✏️ Corregir] on the newest BR-F14 → one BR-F15 row with [No sé].
- [✏️ Lo escribo] on the newest BR-F02 → one BR-F15 row.
- [✏️ Corregir] on an older BR-F14 (a newer question exists) → BR-S02.
- [No sé] while BR-F15 is newest → `BR-F02_dont_know`.
- An old `u<id>:no` press → logged and nothing queued.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py tests/integration/test_pipeline_fetch.py`

## Step 11: BR-F14 offers the repaired strip and [Sí] teaches the stored candidate `[worker]`

Files: `src/invoicebot/pipeline/portal_offer.py`, `tests/integration/test_pipeline_portal_offer.py`.

- `offer_url(reading, known_hosts: frozenset[str]) -> str | None`: the printed URL; else
  `portal_guess.repair_url(reading.url_strip, reading.value("razon_social"), known_hosts)` when `url_strip` is set;
  else the QR link outside SUNAT, as today.
- `ask_portal` loads `taught_links.approved_hosts(conn)`, and when it has a URL, `portal_candidates.save`s it before
  queueing BR-F14. A new `offer_candidate(conn, addressee, invoice_id, reading, url)` does the save and the queueing
  (step 12 reuses it).
- [Sí] teaches `portal_candidates.get(conn, invoice_id)`, not a recomputed URL; with none it logs
  `ignored BR-F14 offer without a URL or issuer` as today. Before `teach`, it runs `check_taught_url(url,
setup.resolver)`; on `TaughtUrlError` it queues `BR-F15_retry` with rule `BR-F15` and [No sé], and teaches nothing.

Test cases (docstrings start with `D191`):

- A reading with no printed URL and `url_strip="hitps.//sy.example pe/tiemda"`, `razon_social` `TIENDA SOL S.A.C.`
  → BR-F14 text ends `https://sy.example.pe/tienda` and the candidate is stored.
- Same with an approved taught link on `sv.example.pe` for another RUC → offers `https://sv.example.pe/tienda`.
- A strip that repairs to nothing and no QR link → BR-F02.
- A printed URL wins over the strip.
- [Sí] → BR-S06 proposes the stored candidate (change the stored reading after the offer; the proposal still uses
  the candidate).
- [Sí] with a resolver that maps the host to `10.0.0.1` → `BR-F15_retry`, no `taught_links` row.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py`

## Step 12: Typed answers are echoed before they are taught `[worker]`

Files: `src/invoicebot/pipeline/teach.py`, `src/invoicebot/pipeline/portal_offer.py`,
`src/invoicebot/pipeline/intake.py`, `tests/integration/test_pipeline_teach.py`.

- Move `answer` from `teach.py` to `portal_offer.py` (it needs `offer_candidate`; `portal_offer` already imports
  `teach`, so the move avoids a cycle). `intake.py` imports it from there. `teach.py` drops `looks_like_url` from its
  imports and its docstring line about `answer`.
- `answer(conn, setup, msg) -> bool`, by the sender's newest question (`outbox.last_question` with `NOT_QUESTIONS`):
  - **BR-F15:** "no sé" → `dont_know_reply`. Otherwise `check_taught_url(msg.text, setup.resolver)`: a valid URL →
    `offer_candidate` (BR-F14 echo with the offer buttons, candidate stored); `TaughtUrlError` → `BR-F15_retry` with
    rule `BR-F15` and [No sé]. A reading without `ruc_emisor` logs `ignored portal answer without an issuer`. Always
    returns True.
  - **BR-F02:** "no sé" → `dont_know_reply`, True. A text that `looks_like_url` → the same as BR-F15's URL branch,
    True. Anything else → False (the edit paths, as today).
  - Any other question → False.

Test cases (docstrings start with `D191`), moving the existing `answer` tests with it:

- BR-F15 newest, text `https://portal.example.pe/tienda` (resolver to a public IP) → BR-F14 echo with that URL and
  three buttons; no `taught_links` row yet.
- BR-F15 newest, text `mañana te lo paso` → `BR-F15_retry`; BR-F15 is still the newest question; no edit applied.
- BR-F15 newest, text `no sé` → `BR-F02_dont_know`.
- BR-F02 newest, text `https://portal.example.pe/tienda` → BR-F14 echo, not BR-S06.
- BR-F02 newest, text `total 12.50` → `answer` returns False.
- Echo then [✅ Sí, es este] → BR-S06 to the sender and the owner's alert with that URL.
- `/portal 20000000001 https://portal.example.pe` still proposes at once (unchanged).

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py tests/integration/test_pipeline_portal_offer.py
tests/integration/test_intake.py`

## Step 13: Live check on Telegram `[inline: needs the owner's chat]`

Files: `specs/spikes/portal-guess.md`.

Deploy the branch image to `invoicebot-live` (runbook) and, with the owner: send the PEBSA ticket photo; at BR-F13
press [🔗]. Expect BR-F14 with a repaired URL (the host may still be `sy.` on the first run, since no link is approved
yet). Press [✏️ Corregir], type a non-URL (expect `BR-F15_retry`), then the real address (expect the echo), then
[✅ Sí, es este] (expect BR-S06 to the owner). The owner decides [Aprobar] or [Rechazar]. Cancel the test invoice. Record
each result and any wording note in the spike file.

Test: the spike file lists every check above with its result.
