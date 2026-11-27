# Plan: MVP phase 9z10 · smtp-send

Roadmap: `docs/plan-mvp.md`, row 9z10. Branch `feat/smtp-send`, from `main`.

What the live run showed (2026-11-27, #45): the flow B mail went out through Graph in the D197 HTML shape and still
bounced at the work tenant with `550 5.7.501 … AS(130)`. Since 2026-11-16 every Graph `sendMail` from a personal
Outlook.com account bounces this way while the web UI keeps sending (`specs/spikes/smtp-probe.md`). One probe mail
through `smtp-mail.outlook.com:587` with OAuth (`AUTH XOAUTH2`, scope `SMTP.Send`), same account, same content, reached
the work inbox.

Decision this plan proposes (step 1), **D205 Flow B mail goes through SMTP with OAuth**:

- A new mail mode `smtp` sends the mail from the sink's `account` through `microsoft.smtp_host` (default
  `smtp-mail.outlook.com`) on `microsoft.smtp_port` (default 587): STARTTLS, `AUTH XOAUTH2` with an access token for
  `https://outlook.office.com/SMTP.Send`, the D197 HTML body as an alternative to the plain text, `From` the account's
  pinned mailbox. It completes like `graph` (`accepted`, BR-D10): the server took the mail.
- The token comes from the account's one MSAL cache. Its sign-in asks for the Graph scopes as today and then, for an
  account an `smtp` mailer uses, a second device code for `SMTP.Send` into the same cache (two codes, once).
- Graph keeps OneDrive and the inbox; the `graph` mode stays for a tenant that accepts it. Live flow B switches to
  `modes: [smtp]`.
- A mail the server accepted can still bounce later; reading the bounce and falling back to an Outlook draft is 9z11
  `bounce-watch`. One summary after the fetch and [Ver documentos] are 9z12 `chat-tidy`.

## Step 1: D205, the specs and the roadmap rows `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `specs/02-requirements.md`, `specs/03-design.md`, `docs/plan-mvp.md`,
`specs/spikes/smtp-probe.md`.

- `specs/05-open-questions.md`: row D205 after D204 with the decision above, marked "(9z10 plan, proposed 2026-11-27,
  from the SMTP probe)".
- `specs/02-requirements.md`: R-7.7 gets mode 7 `smtp` (D205); R-7.8 adds `smtp` next to `graph` (server accepts,
  BR-D10); R-7.9 adds `SMTP.Send` for an account an `smtp` mailer uses.
- `specs/03-design.md`: a row `smtp` in the mail-modes table (SMTP with OAuth, `accepted`, BR-D10).
- `docs/plan-mvp.md`: rows 9z10 `smtp-send` (this plan, D205), 9z11 `bounce-watch` (read the bounce notice in the
  personal inbox, tell the owner truthfully, fall back to an Outlook draft with [Abrir borrador]) and 9z12
  `chat-tidy` (one summary after the fetch, its Revisa line and buttons only for fields still unconfirmed;
  [Ver documentos] on the final message posts the filed files).
- `specs/spikes/smtp-probe.md`: the probe's record (already written).

Test: `npx prettier --check specs/05-open-questions.md specs/02-requirements.md specs/03-design.md docs/plan-mvp.md specs/spikes/smtp-probe.md`

## Step 2: a token for other scopes and adding a resource to a sign-in `[worker]`

Files: `src/invoicebot/auth/msal_cache.py`, `tests/unit/test_auth_msal_cache.py`.

- `TokenProvider.token_for(scopes: Sequence[str]) -> str` (async, `asyncio.to_thread`): the same silent path as
  `token()` (one account in the cache, the pinned mailbox, save if changed) for the given scopes; `token()` becomes
  `token_for(self._scopes)`. The reserved scopes are filtered the same way.
- `TokenProvider.add_scopes_device_code(show, scopes: Sequence[str]) -> None`: loads the existing cache (unlike
  `login_device_code`, which starts fresh), runs a device flow for `scopes`, checks the id token names the pinned
  mailbox (`WrongMailbox` and nothing saved otherwise), then saves. A failed flow raises `AuthRequired`.
- Docstrings cite D205.

Cases (docstrings start with `D205:`; the existing fake MSAL application or HTTP client in that test file):

1. `token_for(["https://outlook.office.com/SMTP.Send"])` asks MSAL's silent call for that scope and returns its token.
2. `token()` still asks for the provider's own scopes.
3. `add_scopes_device_code` keeps the account already in the cache and saves the merged cache.
4. `add_scopes_device_code` signed into another mailbox raises `WrongMailbox` and leaves the file unchanged.

Test: `uv run pytest -q tests/unit/test_auth_msal_cache.py`

## Step 3: the `smtp` mode in config and its scope `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/auth/providers.py`, `tests/unit/test_config_models.py`.

- `MailMode` adds `"smtp"`. `Microsoft` gets `smtp_host: str = "smtp-mail.outlook.com"` and
  `smtp_port: int = Field(default=587, gt=0, lt=65536)`. A constant `SMTP_MODE = "smtp"` next to `GRAPH_MODE`, and
  `smtp_send_accounts(cfg) -> set[str]`: the `account` of every mailer sink whose modes hold `smtp`.
- `providers.py`: `SMTP_SEND_SCOPE = "https://outlook.office.com/SMTP.Send"` and
  `smtp_scopes_for(cfg, account) -> tuple[str, ...]`: `(SMTP_SEND_SCOPE,)` for an account in `smtp_send_accounts`,
  else `()`. `microsoft_accounts` and `delivery_accounts` count an `smtp` account (it delivers mail).
- The tests go in `tests/unit/test_config_models.py`, where the `scopes_for` tests already are.

Cases (docstrings start with `D205:`):

1. A mailer sink with `modes: [smtp]` loads; the defaults are `smtp-mail.outlook.com` and 587.
2. `smtp_port: 0` is refused at load.
3. `smtp_send_accounts` names the account of an `smtp` mailer and not of a `graph`-only one.
4. `smtp_scopes_for` gives `(SMTP_SEND_SCOPE,)` for that account and `()` for another.
5. An account used only by an `smtp` mailer is in `microsoft_accounts` and `delivery_accounts`.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 4: the `.eml` builder takes a sender and an HTML alternative `[worker]`

Files: `src/invoicebot/sinks/eml.py`, `tests/unit/test_sinks_eml.py`.

- `build_eml(mail, *, sender: str | None = None, html: bool = False) -> bytes`: with `sender`, a `From` header (CR/LF
  refused like the other headers); with `html`, `add_alternative(html_body(mail.body), subtype="html")` after the
  plain text, `html_body` imported from `invoicebot.sinks.mailer_graph`. Defaults keep today's dry-run output
  byte-for-byte apart from the `Date`. The module docstring cites D205.

Cases (docstrings start with `D205:`):

1. With `sender`, the parsed message's `From` is that address.
2. A sender holding a newline raises `ValueError`.
3. With `html=True`, the message holds a `text/plain` and a `text/html` part, the latter equal to `html_body(body)`;
   the attachments are still there.
4. Without the new arguments, there is no `From` and no `text/html` part (today's shape).

Test: `uv run pytest -q tests/unit/test_sinks_eml.py`

## Step 5: the SMTP mailer `[worker]`

Files: `src/invoicebot/sinks/mailer_smtp.py` (new), `tests/unit/test_sinks_mailer_smtp.py` (new).

- `class SmtpError(Exception)` carrying only the SMTP reply code (`code: int`, 0 for a connection failure), never an
  address or the server's text (R-9.16).
- `class SmtpMailer` implementing the `Mailer` port: `__init__(self, tokens: ScopedTokenSource, *, host: str,
port: int, timeout_s: float, sender: str, account: str, connect: Callable[[str, int, float], smtplib.SMTP] =
smtplib.SMTP)`, where `ScopedTokenSource` is a small `Protocol` in this module with
  `async def token_for(self, scopes: Sequence[str]) -> str`. `send(mail)` gets the token for `SMTP_SEND_SCOPE`, builds
  `build_eml(mail, sender=sender, html=True)`, then in `asyncio.to_thread`: connect, `ehlo`, `starttls`, `ehlo`,
  `docmd("AUTH", "XOAUTH2 " + base64("user=<sender>\x01auth=Bearer <token>\x01\x01"))`, `sendmail(sender, to + cc,
bytes)`, `quit`. Reply 235 is the only accepted AUTH answer: 535 raises `AuthRequired(account)`, any other raises
  `SmtpError(code)`. `SMTPResponseException` and `SMTPRecipientsRefused` map to `SmtpError` with the code (the first
  refused recipient's code), `OSError` and `SMTPServerDisconnected` to `SmtpError(0)`, each `from` the original.
  The connection always closes (`with`).
- Docstrings cite D205 and say the token never reaches a log or an exception.

Cases (docstrings start with `D205:`; a fake SMTP class passed as `connect`, recording calls):

1. A send runs STARTTLS before AUTH, authenticates with the XOAUTH2 string for the sender and token, and sends the
   bytes to every `to` and `cc` address.
2. The sent bytes have `From` the sender and a `text/html` part.
3. AUTH answered 535 raises `AuthRequired` and sends nothing.
4. AUTH answered 454 raises `SmtpError(454)`.
5. A refused recipient raises `SmtpError` with its code; the message holds no address.
6. A connection `OSError` raises `SmtpError(0)`.
7. The token is not in any raised exception's text.

Test: `uv run pytest -q tests/unit/test_sinks_mailer_smtp.py`

## Step 6: the send mode is mailer-neutral and lists `smtp` `[worker]`

Files: `src/invoicebot/sinks/mailer.py`, `src/invoicebot/pipeline/deliver.py`, `tests/unit/test_sinks_mailer.py`,
`tests/integration/test_pipeline_deliver.py`.

- Rename `GraphSend` to `MailerSend` (docstring: hands the mail to the injected mailer, which sends it once; `graph`
  and `smtp` both use it, D205). `mail_modes(cfg, dry_run, graph, bot_mailer, laptop, smtp: MailerSend | None)`:
  `modes["smtp"]` is `smtp` when given and sending is on, the dry run when sending is off; absent when `smtp` is None.
  Update every caller and test of the old name in these files (`app.py` is renamed in step 7).
- `deliver.py`: `AUTH_PROVIDER_SLUGS` maps `SMTP_MODE` to the Microsoft slug, so BR-D01 names the right sign-in.
- If the rename must touch a file outside this step, stop and report it rather than editing it.

Cases (docstrings start with `D205:`):

1. `mail_modes` with an `smtp` mailer and sending on maps `smtp` to it.
2. With sending off, `smtp` maps to the dry run.
3. Without an `smtp` mailer, `smtp` is absent.
4. An `AuthRequired` from the `smtp` mode parks the invoice as BR-D01 with the Microsoft slug (an existing deliver
   test of the `graph` auth path, repeated for `smtp`).

Test: `uv run pytest -q tests/unit/test_sinks_mailer.py tests/integration/test_pipeline_deliver.py`

## Step 7: wiring and the two-code sign-in `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/cli.py`, `tests/unit/test_app_wiring.py`, `tests/integration/test_cli_auth.py`.

- `app.py`: the old `GraphSend` name becomes `MailerSend`. When `smtp_send_accounts(cfg)` holds the personal account,
  build `SmtpMailer(provider, host=cfg.microsoft.smtp_host, port=cfg.microsoft.smtp_port,
timeout_s=cfg.microsoft.timeout_s, sender=cfg.microsoft.accounts[account], account=account)` on the same
  `TokenProvider` the Graph mailer uses, and pass `MailerSend(smtp_mailer)` to `mail_modes`; else `None`.
- `cli.py` `_auth_microsoft`: after `login_device_code`, when `smtp_scopes_for(cfg, account)` is not empty, print one
  line ("a second code adds sending through SMTP") and call `provider.add_scopes_device_code(_show, those scopes)`.
  A failure there exits 1 and resumes nothing, as the first one does.

Cases (docstrings start with `D205:`; the existing fakes):

1. A config whose flow B mailer lists `smtp` wires an `smtp` mode; one without it wires none.
2. `auth-microsoft` for an `smtp` account runs both device flows, the second for `SMTP.Send`, then resumes.
3. A failed second flow exits 1 and resumes nothing.
4. An account with no `smtp` mailer runs one device flow, as before.

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/integration/test_cli_auth.py`

## Step 8: example config, runbook and the live switch `[inline: host commands and a live check with the owner]`

Files: `config/example.yaml`, `docs/runbook.md`, `specs/spikes/smtp-probe.md`.

- `config/example.yaml`: the flow B mailer comment lists `smtp` (D205) and says why (`graph` from a personal account
  bounces 5.7.501 since 2026-11-16); `microsoft.smtp_host` and `smtp_port` shown commented with their defaults.
- `docs/runbook.md`: the Microsoft sign-in section says an `smtp` account takes two codes.
- After the merge: build and `./deploy/deploy-live.sh <tag>` with live flow B on `modes: [smtp]`; the owner runs
  `invoicebot auth-microsoft --account personal` (two codes); remove the probe cache
  `/data/tokens/smtp-probe.msal.age`; the owner sends one receipt in Telegram; check the personal mailbox for a bounce
  for ten minutes and the owner confirms the work inbox. Record it under "Live run" in the spike file (no addresses).

Test: `npx prettier --check docs/runbook.md specs/spikes/smtp-probe.md && uv run python -c "import yaml; yaml.safe_load(open('config/example.yaml'))"`
