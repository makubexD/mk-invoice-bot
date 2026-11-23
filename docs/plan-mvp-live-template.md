# Plan: MVP phase 9m · live-template

Roadmap: `docs/plan-mvp.md` row 9m (D86, R-7.10, BR-D02). The 9l live check moved console invoice 8 to `DELIVERING`,
where it parked on BR-D02 (gap `TEMPLATE_INVALID`). The live config's `excel_template` sink still has placeholders:
`template: /data/templates/reembolsos.xlsx` does not exist in the live `appdata` volume, and `template_sha256` is
all zeros. Every flow B delivery parks there, and 9d2's flow B run would too.

The ten private samples (`Formato de reembolsos.xlsx` in `~/Downloads/reembolsos-samples/*/`) are all filled in. They
share one sheet `Template`, no macros, and the cells the sink writes (`C13`, `B16`, `C16`, `E16`, `F16`, `G16`, `H16`,
`I16`). `D16` (`+B4`), `G16` (`F16*18/100`), `I16` (`F16+G16+H16`) and `I17` (`MIN(I16, 50)`) are formulas. The
patcher overwrites `G16` and `I16` with the invoice's own IGV and total and recomputes `I17` (`xlsx_patch.py`), so the
template only needs its input cells blank. Reloading with a changed sink closes the gap and resumes invoice 8 (D157).

Owner's decision (2026-11-22): invoice 8 is delivered for real as the end-to-end flow B check. That creates a folder
under `/Reembolsos` in the personal OneDrive and sends a Graph mail to the owner's work address. The owner removes
them afterwards if they duplicate the October submission.

The template and its copies are the owner's private data: never in git (D86), never sent to the worker. Every step
works on the live host, so every step is inline.

**Needs from the owner first:** a Microsoft sign-in still valid on the live host (9d2 step 1), for step 4.

## Step 1: roadmap row 9m and runbook `[inline: spec and runbook wording]`

1. `docs/plan-mvp.md`: add row 9m `live-template` (plan: `docs/plan-mvp-live-template.md`, D86) after 9l, before 9d2:
   "the flow B Excel template installed and pinned on the live host; invoice 8 delivered end to end; before 9d2".
2. `docs/runbook.md`: a new section "Install or replace the Excel template (D86, R-7.10)" after section 2. Build the
   blank template, copy it into the live `appdata` volume at `templates/reembolsos.xlsx` with
   `podman unshare` (owner `10001`, mode `0640`), put its `sha256sum` in the sink's `template_sha256`, then
   **reload**. A parked `TEMPLATE_INVALID` invoice resumes on that reload (D157). Renumber the later sections and fix
   any cross-reference to them (`grep -n "section [0-9]" docs/`).

Test: `grep -n "9m" docs/plan-mvp.md && grep -n "Excel template (D86" docs/runbook.md`, and
`uv run pytest -q tests/unit/test_docs.py`.

## Step 2: build the blank template `[inline: the owner's private workbook]`

A throwaway script in the session scratchpad, run with `python3 -I`, never in the repo:

1. Pick a sample whose `G16` is `F16*18/100`, `I17` is `MIN(I16, 50)` and `H16` is empty.
2. In `xl/worksheets/sheet1.xml`, remove the value (`<v>`, `<is>` and the `t` attribute) from the input cells `B16`,
   `C16`, `E16`, `F16`, `H16`, and the cached `<v>` of the formula cells `G16`, `I16`, `I17`. Keep every formula,
   style, label and `C13`'s text. Write every other zip member unchanged, in its original order.
3. Fill the result once with the repo's own sink code (`uv run python -I` calling `invoicebot.sinks.excel_template`
   with synthetic values: fecha 2026-11-01, serie `F001`, numero `1`, base `10.00`, igv `1.80`, service_charge `0`,
   total `11.80`, comercio `Synthetic`).

Test: the inspector shows the five input cells empty and the four formulas intact; the synthetic fill succeeds and
its `I16` reads `11.80`; LibreOffice (`soffice --headless --convert-to csv`) opens both files without error.

## Step 3: install, pin and reload `[inline: the owner's live host]`

1. Copy the template into the volume: `podman unshare` `mkdir -p` `templates/`, `cp`, `chown 10001:10001`,
   `chmod 0640`. Check from inside the app container that `/data/templates/reembolsos.xlsx` is readable.
2. `sha256sum` it and set `template_sha256` in `$LIVE_ROOT/config/config.yaml`, nothing else.
3. Reload: `./deploy/deploy-live.sh --config "$LIVE_ROOT/config/config.yaml"`.

Test: the reload passes; the gap `template_invalid:work_microsoft:excel_template` is `fixed`; invoice 8 leaves
`MANUAL_REQUIRED`.

## Step 4: invoice 8 delivered end to end `[inline: the owner's live host]`

Amended 2026-11-22 (owner's decision): the live host runs `env: development` with `mail.send_enabled: false`, so every
mail mode is `dry_run` (R-7.7, D89) and the mail is an `.eml` draft (BR-D07), not a Graph send. Production config
refuses the `console` channel (D165), so the real Graph send moves to 9d2.

1. Follow invoice 8 with `rehearse replies --after 64` and its row in `invoices` until it settles at BR-D07
   (`BATCHED`, draft written).
2. Check the OneDrive folder with the read-only `rclone` remote: PDF, XML and a `Formato de reembolsos.xlsx` whose
   cells match invoice 8. The live profile's placeholder `legal_name` is set to the owner's file name (live config
   only), then **reload**.
3. Press the draft's "Ya lo envié" button (`rehearse send --button 8:sent`).

Test: invoice 8 is `DONE` with BR-D08, and its OneDrive folder ends in `_Sent`.
