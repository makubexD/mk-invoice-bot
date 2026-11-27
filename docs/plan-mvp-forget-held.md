# Plan: MVP phase 9z9 · forget-held

Roadmap: `docs/plan-mvp.md`, row 9z9. Branch `fix/forget-held`, from `main`.

What the live run showed (2026-11-27, after 9z8's deploy): the owner sent #43's photo again (it came in as #44 and was
cancelled as a duplicate, BR-E04) and wanted #43 forgotten so the same photo is accepted again. #43 is `BATCHED`
because D194's `send: review` held its mail for [Enviar]; nothing was sent. `/cancelar 43` refuses it (BATCHED is not
in `CANCELLABLE`, D96) and `invoicebot invoice forget 43` refuses it (only `TERMINAL` states, D196). So a held mail
can be neither cancelled nor forgotten.

Decision this plan proposes (step 1), **D204 A held mail can be forgotten**:

- `invoicebot invoice forget` also accepts a `BATCHED` invoice whose batch is in state `review` (its mail is waiting
  for [Enviar] and was never sent, D194). Any other `BATCHED` invoice is still refused.
- On `--yes`, in the same transaction as the key release or the purge, such an invoice is first set `CANCELLED`, so
  a later [Enviar] on its old BR-D19 finds no review to send (BR-D22) instead of sending.
- The dry run says it is held and will be cancelled.

Out of scope: letting `/cancelar` in the chat cancel a held mail (the owner asked for the host cleanup; [Cancelar]
keeps holding as D194 says).

## Step 1: D204 and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D204 after D203 with the decision above, marked "(9z9 plan, proposed
  2026-11-27, from the 9z8 live run)".
- `docs/plan-mvp.md`: add row 9z9 `forget-held` after 9z8 (plan `docs/plan-mvp-forget-held.md`, D204).

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: the target knows when its mail is held `[worker]`

Files: `src/invoicebot/store/forget.py`, `tests/integration/test_store_forget.py`.

- `_LOCK_INVOICE` adds `b.state = 'review'` through `LEFT JOIN batches AS b ON b.id = invoices.batch_id` (the lock
  stays `FOR UPDATE OF invoices`); `ForgetTarget` gets `held: bool` (true only for that case).
- New `async def cancel_held(conn, invoice_id) -> None`: sets the invoice `CANCELLED` through the store's existing
  `set_state` (expected `BATCHED`); does not commit. Docstrings cite D204.

Cases (docstrings start with `D204:`; real Postgres): a `BATCHED` invoice on a `review` batch locks with
`held=True`; one on a `prepared` batch with `held=False`; a `DONE` invoice with `held=False`; `cancel_held` leaves
the invoice `CANCELLED`.

Test: `uv run pytest -q tests/integration/test_store_forget.py`

## Step 3: forget accepts and cancels a held invoice `[worker]`

Files: `src/invoicebot/ops/forget.py`, `tests/integration/test_ops_forget.py`.

- `_require_forgettable` accepts `target.held` as well as `TERMINAL`; the refusal text for other states is unchanged.
- `_finish` calls `cancel_held` before the key release or the purge when `target.held`.
- The dry-run plan (`_plan_lines`) adds a line `state: BATCHED, mail held for review; will be cancelled` when held.
- `forget`'s docstring cites D204.

Cases (docstrings start with `D204:`; real Postgres, the fake drive):

1. A held invoice, dry run: the plan has the held line; nothing changes.
2. A held invoice, `--yes` (key only): it is `CANCELLED` and its key is released.
3. A held invoice, `--purge --yes`: its rows and its now unused batch are gone.
4. A `BATCHED` invoice on a `prepared` batch: refused, as before.

Test: `uv run pytest -q tests/integration/test_ops_forget.py`

## Step 4: runbook and the live cleanup `[inline: host commands and a live check with the owner]`

Files: `docs/runbook.md`.

- §13: a held mail (`BATCHED` waiting for [Enviar]) can be forgotten too; it is cancelled first (D204).
- After the merge and deploy: forget #43 with `--drive --purge` and #44 with `--purge` (the owner runs them if the
  classifier denies), then the owner resends the photo for 9z8's live check.

Test: `npx prettier --check docs/runbook.md`
