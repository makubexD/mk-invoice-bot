# Plan: MVP phase 9k · rehearse

Roadmap: `docs/plan-mvp.md` row 9k (D165). Meta business verification is still pending, so every 9d2 live run waits on
WhatsApp. This phase adds a dev-only `console` channel: a CLI feeds a photo, PDF, text or button press into the live
pipeline and prints the bot's replies from the outbox. Flow A and flow B can then be rehearsed end to end on the live
host now (Drive, Sheets, OneDrive, Excel, mail dry run), and when Meta approves only the WhatsApp channel itself is new.

The console channel is a new adapter behind the existing ports (`Channel`, `MediaSource`, outbox `Transport`),
registered in one place; the pipeline does not learn its name. It is off unless the config lists it, and config load
refuses it when `env: production`.

**Needs from the owner first:** nothing for steps 1 to 8. Steps 9 and 10 use the live host, the owner's private
config and real invoices.

## Step 1: D165 and the spec text `[inline: design decision and spec wording]`

1. `specs/05-open-questions.md`: add D165 (proposed): a dev-only `console` channel for rehearsals while Meta is
   pending; inbound through `invoicebot rehearse send` (stdin bytes, capped), outbound rows kept in the outbox and
   printed by `invoicebot rehearse replies`; off unless configured, refused in production; replies to a console
   invoice go back to `console` because they follow `origin_channel`.
2. `specs/02-requirements.md` R-1.9 and `specs/03-design.md` (channel adapters): one sentence each naming the
   console adapter as dev-only (D165).
3. `docs/plan-mvp.md`: add row 9k `rehearse` (plan: `docs/plan-mvp-rehearse.md`, D165), before 9d2.

Test: `grep -n D165 specs/02-requirements.md specs/03-design.md specs/05-open-questions.md docs/plan-mvp.md` shows
each file.

## Step 2: The download handler takes a media source per channel `[worker]`

Files: `src/invoicebot/pipeline/handlers.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_handlers.py`.

`make_download_handler(pool, client, data_dir, limits)` is wired to `WhatsAppClient`. Change it to take
`sources: Mapping[str, MediaSource]` in place of `client`, the same shape `make_attach_handler` already takes, and
pick the source by the invoice's `origin_channel`. An invoice whose channel has no source raises
`LookupError(f"no media source for channel {channel}")`. In `app.py` pass
`{WHATSAPP_ADAPTER: WhatsAppMediaSource(client)}`, the same object the attach handler gets (build it once and pass
it to both). No behavior change for WhatsApp.

Test cases in `tests/integration/test_pipeline_handlers.py` (update the existing download tests to pass a mapping):

- a WhatsApp invoice downloads through the `whatsapp` source exactly as before (existing tests still pass);
- an invoice whose `origin_channel` has no source in the mapping raises `LookupError` and stores no `files` row.

Test: `uv run pytest -q tests/integration/test_pipeline_handlers.py`

## Step 3: `console` in the channel name and the schema `[worker]`

Files: `src/invoicebot/channels/base.py`, `migrations/versions/0013_console_channel.py`,
`tests/integration/test_schema.py`.

1. `ChannelName` becomes `Literal["whatsapp", "telegram", "console"]`.
2. Migration `0013` (expand only, plain SQL through `op.execute`): replace the CHECK constraints that list channels
   so they also allow `'console'`: `invoices.origin_channel` (`'whatsapp', 'telegram', 'console'`) and
   `outbox.channel` (`'whatsapp', 'telegram', 'github', 'event', 'console'`). Look up the real constraint names in
   the database (Postgres names them `invoices_origin_channel_check` and `outbox_channel_check` by default; confirm
   with the test) and use `DROP CONSTRAINT ... , ADD CONSTRAINT ... CHECK (...)` in one `ALTER TABLE` each. Widening
   a CHECK is an expansion: every existing row still passes and the previous image never writes `console`.
   `downgrade` restores the old lists.

Test cases in `tests/integration/test_schema.py`:

- an `invoices` row with `origin_channel = 'console'` inserts;
- an `outbox` row with `channel = 'console'` inserts;
- `origin_channel = 'sms'` is still refused (CheckViolation).

Test: `uv run pytest -q tests/integration/test_schema.py`

## Step 4: The console channel's config `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, the config model's unit test file
(`tests/unit/test_config*.py`, the one that already tests `channels`).

1. Add `ConsoleChannel(BaseModel)`: `extra="forbid"`, `kind: Literal["console"]`, `profile: str` (the profile the
   console speaks as). Add it to the `Channel` union (discriminator `kind`).
2. Config validation: a `console` channel whose `profile` is not in `profiles` is refused naming the profile; a
   `console` channel with `env: production` is refused ("the console channel is dev-only (D165)"); at most one
   `console` channel.
3. `config/example.yaml`: a commented example under `channels` with the synthetic owner profile and a `# D165:
   dev-only rehearsal channel` comment.

Test cases (unit):

- `kind: console, profile: <existing>` with `env: development` loads;
- an unknown `profile` is refused, the message names it;
- `env: production` with a console channel is refused, the message names D165;
- two console channels are refused;
- `config/example.yaml` still loads (existing test).

Test: `uv run pytest -q tests/unit -k "config"`

## Step 5: The console adapter `[worker]`

Files: `src/invoicebot/channels/console.py` (new), `src/invoicebot/channels/profiles.py`,
`src/invoicebot/channels/whatsapp/parse.py`, `tests/unit/test_channels_console.py` (new).

1. Move `_sender_role(profile_id, owner_profile, admin_profiles)` from `whatsapp/parse.py` to `channels/profiles.py`
   as public `sender_role`; `parse.py` imports it (one home: both channels derive the role the same way).
2. `channels/console.py` (module docstring: the dev-only rehearsal channel, D165):
   - `CONSOLE = "console"` and `inbox_dir(data_dir) -> data_dir / "console" / "inbox"`.
   - `ConsoleMediaSource(data_dir)` implements `MediaSource`: `media_ref` must match `^[A-Za-z0-9._-]{1,64}$` and not
     start with `.` (else `ValueError`); the file is `inbox_dir / media_ref`; its size must be within the cap
     `limits` gives for `kind` (else raise the same `MediaRejected` the WhatsApp source raises for an oversize
     file); it is copied into `out_dir`, hashed with sha256 and returned as `DownloadedMedia`, then the inbox file
     is removed. No partial file is left on failure.
   - `console_transport() -> Transport`: delivers a row by doing nothing (the row stays in the outbox, where
     `rehearse replies` reads it) and logs the row id at DEBUG.

Test cases (unit, `tmp_path`):

- a file in the inbox is returned with the right sha256, size and mime, lands in `out_dir` and leaves the inbox;
- `media_ref` values `../x.jpg`, `a/b.jpg`, `.hidden`, and a 65-character name raise `ValueError`;
- a file over the photo cap raises `MediaRejected` and leaves nothing in `out_dir`;
- a missing file raises `FileNotFoundError`;
- `sender_role` returns `owner`, `admin` and `sender` for the three cases (WhatsApp parse tests still pass).

Test: `uv run pytest -q tests/unit/test_channels_console.py tests/unit -k "parse or console"`

## Step 6: Wire the console channel `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_console.py` (new).

Add `_console_entries(cfg, data_dir) -> tuple[dict[str, MediaSource], dict[str, Transport]]` in `app.py`: empty
dicts without a `ConsoleChannel` in `cfg.channels`, else `{CONSOLE: ConsoleMediaSource(data_dir)}` and
`{CONSOLE: console_transport()}`. Merge the sources into the mapping the download and attach handlers get (step 2)
and the transport into `transports`. No `if channel == ...` anywhere else.

Test cases (unit):

- a config without a console channel gives two empty dicts;
- a config with one gives one `ConsoleMediaSource` and one transport, both under `"console"`.

Test: `uv run pytest -q tests/unit/test_app_console.py`

## Step 7: `invoicebot rehearse send` `[worker:high: reasoning: reuse the webhook's exact intake arguments without copying them]`

Files: `src/invoicebot/cli.py`, `src/invoicebot/channels/console.py`, `src/invoicebot/api/webhook.py` (only if the
intake arguments must move), `tests/integration/test_cli_rehearse.py` (new).

`invoicebot rehearse send [--name FILE] [--text TEXT] [--button ID]` (at least one of them):

- With `--name`, read the file's bytes from stdin, at most the largest media cap plus one byte (refuse more, before
  writing), write them to `inbox_dir/<name>` under a fresh unique prefix, and attach it with the kind and mime its
  extension gives (`.jpg`/`.jpeg`/`.png` photo, `.pdf` pdf, `.xml` xml; anything else is refused).
- Build one `InboundMessage(channel="console", external_id="console-<uuid4 hex>", profile_id=<console profile>,
  sender_role=sender_role(...), text, button_id, attachments, received_at=now)`.
- Run `handle_inbound` in one transaction with the same arguments the webhook passes (`max_queued_per_sender`,
  `media_max_bytes`, `timezone`, `roles`). Those arguments have one home: if the webhook builds them inline, move
  that construction to a function both import; never copy it.
- Print the intake result and the invoice id when there is one. The logic lives in `channels/console.py`; `cli.py`
  only parses arguments and calls it. Usage: `podman exec -i "$APP" invoicebot rehearse send --name f.jpg < f.jpg`.
- Refused when the config has no console channel ("console channel not configured (D165)"), exit 2.

Test cases (integration, real Postgres, the fake config fixture with a console channel):

- a photo from stdin creates one invoice with `origin_channel = 'console'`, a `download_media` job and a BR-S09
  reply in the outbox with `channel = 'console'`;
- `--text "/ayuda"` (or the repo's help command) stores a reply and creates no invoice;
- stdin over the cap is refused, writes no inbox file and creates nothing;
- `--name x.exe` is refused;
- without a console channel in the config it exits 2 with the D165 message.

Test: `uv run pytest -q tests/integration/test_cli_rehearse.py`

## Step 8: `invoicebot rehearse replies` `[worker]`

Files: `src/invoicebot/store/` (the module that owns outbox queries; add one function there),
`src/invoicebot/channels/console.py`, `src/invoicebot/cli.py`, `tests/integration/test_cli_rehearse.py`.

`invoicebot rehearse replies [--after ID]` prints every outbox row with `channel = 'console'` and `id > ID` (default
0), oldest first: the row id, its rule id, its status, its text, then one line per button as `[<id>] <title>`. The
last line is `next: --after <last id>`. Store function: `console_replies(conn, after_id) -> list[...]` with
parameterized SQL, returning a frozen dataclass per row. Payloads are validated with `OutboundMessage` before
printing.

Test cases (integration):

- after a `rehearse send` photo, `replies` prints the BR-S09 text and `next: --after <id>`;
- `--after <that id>` prints no rows and the same `next` line;
- a row with buttons prints each `[id] title` line;
- rows of other channels are never printed.

Test: `uv run pytest -q tests/integration/test_cli_rehearse.py`

## Step 9: Live with the console channel `[inline: the owner's private config and the live host]`

1. `docs/runbook.md`: a "Rehearse without WhatsApp (D165)" section: enable, `rehearse send` with stdin, `rehearse
   replies --after`, pressing a button with `--button`, and removing the channel from the config when 9d2 starts.
2. Add `{kind: console, profile: <owner profile>}` to the live and private configs. Build the image, check the
   candidate with it, deploy (`0013` migrates on deploy), reload, `/health`.
3. Smoke: send one invoice photo; `replies` shows BR-S09.

Test: `/health` ok; the smoke photo gets BR-S09 in `rehearse replies`.

## Step 10: Rehearse flow A and flow B `[inline: the owner's real invoices and accounts]`

Run the 9d2 step 3 and step 4 checks through the console instead of WhatsApp, except the ones that need the chat
itself: flow A (photo → Drive files, one Sheet row, BR-D16 links; `/reprocesar` updates the row; PDF-only store;
DNI boleta) and flow B (OneDrive folder, Excel, mail in `dry_run`, `/pagado`). Record what works and what breaks in
`specs/spikes/host.md` under "Console rehearsal (9k)". A break is a finding: fix it in this phase if small, else add
it to `docs/plan-mvp.md`. M3 and M6 are still recorded only by the live chat in 9d2.

Test: the checks above, by hand; the notes exist.
