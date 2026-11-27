# Plan: MVP phase 9z4 · html-mail

Roadmap: `docs/plan-mvp.md`, row 9z4. Branch `feat/html-mail`, stacked on `feat/forget` (9z3) and `feat/review-ui` (9z2).

What the live runs showed: every flow B mail the bot sent through Graph to the owner's work address bounced with
`550 5.7.501 Spam abuse detected from IP range` (`specs/spikes/open-links.md`, `specs/spikes/forget.md`), while the same
files sent by hand from Outlook arrived. On 2026-11-26 an A/B send from the live account settled it: mail A (today's
shape, a `Text` body) bounced within two seconds; mail B (an `HTML` body in Outlook's default font, with a greeting and
a signature, the same three attachments) was delivered. Outlook.com scores each outgoing mail and sends the suspicious
ones through its high-risk delivery pool, which the work tenant refuses. The burst of test sends also got the personal
account blocked for sending (`ErrorAccountSuspend`); the owner verifies it at outlook.com before step 3.

Out of scope: reading bounce notices and retrying (9x `inbox-match`); the dry_run `.eml` and the laptop agent's mail,
which do not leave through Graph.

Decision this plan proposes (step 1), **D197 Graph mail is HTML**:

- `GraphMailer` sends the rendered body as `contentType: HTML`: the config's plain-text body, HTML-escaped, a blank
  line starting a new paragraph and a single line break a `<br>`, inside one `div` in Outlook's default font
  (Calibri 11pt). The config keeps a plain-text body; no HTML in config.
- The live flow B body ends with a sign-off (`Saludos,` and the owner's name), as the delivered test mail did. That is
  a live config edit, not code.
- Test sends from a live account go one at a time, minutes apart: a burst gets the account blocked.

## Step 1: D197 and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D197 after D196 with the decision above, marked "(9z4 plan, proposed
  2026-11-26, from the A/B send)".
- `docs/plan-mvp.md`: add row 9z4 `html-mail` after 9z3 (plan `docs/plan-mvp-html-mail.md`, D197).

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: the HTML body `[worker]`

Files: `src/invoicebot/sinks/mailer_graph.py`, `tests/unit/test_sinks_mailer_graph.py`.

- A module constant `BODY_STYLE = "font-family: Calibri, Arial, sans-serif; font-size: 11pt"` and a function
  `html_body(text: str) -> str`: `html.escape(text)`, normalize `\r\n` to `\n`, split paragraphs on one or more blank
  lines, drop empty ones, turn a single `\n` inside a paragraph into `<br>`, wrap each paragraph in `<p>` and all of
  them in `<html><body><div style="{BODY_STYLE}">…</div></body></html>`.
- `_message` sends `"body": {"contentType": "HTML", "content": html_body(mail.body)}`; the inline and draft shapes
  both use it. Update the module docstring to cite D197.

Cases (docstrings start with `D197:`):

1. `html_body("Hola,\n\nAdjunto.\n\nSaludos,\nKiefer")` is
   `<html><body><div style="…">` + `<p>Hola,</p><p>Adjunto.</p><p>Saludos,<br>Kiefer</p>` + `</div></body></html>`.
2. `<`, `>`, `&` and `"` in the text come out escaped (`&lt;b&gt;` etc.), so a value rendered into the body cannot add
   markup.
3. `\r\n` line ends and three blank lines in a row give the same paragraphs as `\n` and one blank line.
4. An inline send posts `contentType` `HTML` and the `html_body` content (existing `httpx.MockTransport` test, updated).
5. The draft path (attachments over the inline limit) creates the draft with the same HTML body.

Test: `uv run pytest -q tests/unit/test_sinks_mailer_graph.py`

## Step 3: live config, deploy and one real send `[inline: needs the owner and the live bot]`

Files: `specs/spikes/html-mail.md` (new).

Only after the owner says the account is verified. Add the sign-off to the live flow B `body` in
`$LIVE_ROOT/config/config.yaml` (`"Hola,\n\nAdjunto los documentos solicitados para el reembolso.\n\nSaludos,\n<name>"`),
build the image, `./deploy/deploy-live.sh <tag>`, then the owner sends one invoice photo through Telegram and presses
[Enviar]. Check the personal mailbox (read-only Graph, `Mail.Read`) for a bounce in the next ten minutes, and the owner
confirms the mail in the work inbox. Record each check in the spike file (no RUCs, no addresses).

Test: the spike file records the send, no bounce, and the mail in the work inbox.
