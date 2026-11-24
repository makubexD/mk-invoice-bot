# Plan: Telegram phase T2 · telegram-live

Roadmap: `docs/plan-telegram.md`. Puts the T1 adapter in front of real users: the owner creates the bot (D179), the
bot's two secrets go into the age store, the `/webhook/telegram` path opens on the Tailscale Funnel, a CLI registers
the webhook, and flows A, B and `personal_dni` are rehearsed live from Telegram. Before that, three small code changes
close the T1 review follow-ups and give the owner the tools the setup needs.

In scope: the roadmap's T2 row and "T2 owner setup" steps 1–11; the two T1 review follow-ups (webhook-secret strength,
BR-O03 fallback channel); the user-id helper and the `setWebhook`/`getWebhookInfo` subcommand T1 left to T2.

Out of scope: a polling
mode (D173); groups (D175); 9d2 `live-chat` (still waits for Meta).

Needs from the owner: the bot and its token (steps 7–8), each sender pressing Start, the live run (steps 9–12). Code
steps 1–6 need nothing from the owner.

Decisions this plan proposes (step 1): D184–D186 below; D187 added by the owner's plan edit on 2026-11-23.

- **D184 webhook secret strength:** `telegram_webhook_secret` must match `[A-Za-z0-9_-]{32,256}` (Telegram's charset,
  at least 32 characters; `secrets.token_urlsafe(32)` gives 43). A configured Telegram channel with a shorter or
  off-charset secret fails startup and reload with `secrets:telegram_webhook_weak`, like a missing secret (R-9.6). The
  value is never logged or echoed.
- **D185 BR-O03 fallback:** when a channel's token is rejected, each admin whose `preferred_channel` is that channel
  gets the alert on their other enabled chat channel when they have an id there; an admin with no other route keeps
  the preferred channel (the alert waits in the outbox with the rest, and the error log stays the only signal). Only
  BR-O03 reroutes; every other admin alert still uses `preferred_channel`.
- **D186 setup CLI:** `invoicebot telegram ids` reads pending updates with `getUpdates` and prints one line per private
  sender, `<from.id> @<username or ->`, never message text; it exits 1 with "a webhook is set; run `telegram webhook
  --delete` first" when Telegram answers 409. `invoicebot telegram webhook --url <https://…/webhook/telegram>` calls
  `setWebhook` with the stored secret, `allowed_updates=["message","callback_query"]` and `drop_pending_updates=true`
  (the Start messages sent for `ids` are not replayed as unknown commands); `--info` prints `getWebhookInfo`'s `url`,
  `pending_update_count`, `last_error_date` and `last_error_message`; `--delete` calls `deleteWebhook`. All three read
  the token from the age store, so it is never typed (R-9.9).

- **D187 command menu:** the Telegram `/` menu (`setMyCommands`) is built from `pipeline/commands.COMMANDS`, so it
  never drifts from `/ayuda`: each entry is the command name and its help line after the first `": "`. The default
  scope lists the commands every sender may run; each admin profile with a `telegram_user_id` gets a chat scope that
  also lists the admin-only ones. `invoicebot telegram commands` sets both. `/start` needs nothing: an unknown command
  already answers the help (R-10.1).

## Step 1: D184–D186, roadmap row `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after D183: rows D184–D186 worded as above, each "(T2 plan, proposed)".
2. `specs/06-business-rules.md`, BR-O03 row, Action cell: "Alert (on the admin's other chat channel when the rejected one is their
   preferred, D185); sends on that channel wait in the outbox".
3. `docs/plan-telegram.md`, phases table: the T2 row's Plan cell becomes `docs/plan-telegram-live.md`; step 7 of the
   owner setup names `invoicebot telegram ids` and step 9 `invoicebot telegram webhook` (D186).

Test: `grep -n "D18[4-6]" specs/05-open-questions.md specs/06-business-rules.md docs/plan-telegram.md` shows each decision.

## Step 2: Webhook secret strength `[worker]`

Files: `src/invoicebot/wiring/telegram.py`, `tests/unit/test_wiring_telegram.py`.

1. Add `WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")` with a comment citing D184.
2. In `TelegramBuilder.secret_problem`, after the existing `secrets:telegram` check and before `secrets:channels`:
   when `WEBHOOK_SECRET_PATTERN.fullmatch(<telegram_webhook_secret>)` is `None`, return
   `"secrets:telegram_webhook_weak"`. Read the value through the existing `_telegram_secrets(store)` view.
3. Tests, docstrings `D184` / `R-9.6`, over a test store with the bot token and the channel secrets present and Telegram
   enabled:
   - a 43-character `token_urlsafe` secret → `None`;
   - a 32-character `[A-Za-z0-9_-]` secret → `None`;
   - a 31-character secret → `"secrets:telegram_webhook_weak"`;
   - a 40-character secret holding a space → `"secrets:telegram_webhook_weak"`;
   - a 257-character secret → `"secrets:telegram_webhook_weak"`;
   - Telegram disabled and a 5-character secret → `None`;
   - the returned code never contains the secret value (assert on the weak cases).

Test: `uv run pytest -q tests/unit/test_wiring_telegram.py tests/unit/test_app_wiring.py`

## Step 3: BR-O03 fallback channel `[worker]`

Files: `src/invoicebot/wiring/channels.py`, `tests/unit/test_wiring_channels.py` (new).

1. Add a pure function `alert_profiles(cfg: Config, channel: str) -> dict[str, Profile]` (docstring: "Return the
   profiles BR-O03 alerts, with each admin moved off the rejected channel when it has another route (D185)."). For each
   profile in `cfg.profiles`: when it is an admin, its `preferred_channel == channel`, and some other channel in
   `enabled_chat_channels(cfg.channels)` (from `invoicebot.config.models`) is one the profile has an id for (`whatsapp`
   for WhatsApp, `telegram_user_id` for Telegram), return `profile.model_copy(update={"preferred_channel": other})`;
   otherwise return the profile unchanged. Pick the other channel in sorted order so the result is deterministic.
   The console channel never counts as a fallback (`preferred_channel` only allows WhatsApp and Telegram).
2. `alert_channel_token` passes `alert_profiles(cfg, channel)` instead of `cfg.profiles`, and the `# shortcut:` comment
   is replaced by one line citing D185.
3. Tests, docstrings `BR-O03` / `D185`, building `Config` from `config/example.yaml`'s shape with both WhatsApp and
   Telegram enabled:
   - admin preferring Telegram with a WhatsApp number, channel `telegram` → that admin's `preferred_channel` is
     `whatsapp`;
   - admin preferring Telegram with no WhatsApp number, channel `telegram` → unchanged (`telegram`);
   - admin preferring Telegram with a WhatsApp number but WhatsApp disabled, channel `telegram` → unchanged;
   - admin preferring WhatsApp, channel `telegram` → unchanged;
   - non-admin preferring Telegram with a WhatsApp number, channel `telegram` → unchanged;
   - admin preferring WhatsApp with a Telegram id, channel `whatsapp` → `telegram`;
   - `cfg.profiles` itself is not mutated (compare before and after).

Test: `uv run pytest -q tests/unit/test_wiring_channels.py tests/unit/test_telegram_transport.py`

## Step 4: Telegram setup calls `[worker]`

Files: `src/invoicebot/channels/telegram/setup.py` (new), `tests/unit/test_telegram_setup.py` (new).

Module docstring: "The one-off Bot API calls the owner's setup runs: sender ids and the webhook (D186)." Functions take
a `TelegramClient` and use its `call`; nothing here is imported by the running bot.

1. `class WebhookActive(Exception)` — `getUpdates` refused because a webhook is set.
2. `async def sender_ids(client) -> list[tuple[int, str | None]]`: `call("getUpdates", {"allowed_updates":
   ["message"], "timeout": 0})`; a `TelegramError` with status 409 raises `WebhookActive`. From each update's
   `message`, keep only private chats (`chat.type == "private"` and `chat.id == from.id`, D175) and return
   `(from.id, from.username or None)`, first-seen order, each id once. Malformed updates are skipped. Message text is
   never read.
3. `async def set_webhook(client, url: str, secret: str) -> None`: refuse (`ValueError`) a `url` that is not `https://`
   or does not end in `/webhook/telegram`; then `call("setWebhook", {"url": url, "secret_token": secret,
   "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True})`.
4. `async def webhook_info(client) -> WebhookInfo`: a frozen dataclass `WebhookInfo(url: str, pending: int,
   last_error_date: int | None, last_error_message: str | None)` from `getWebhookInfo`.
5. `async def delete_webhook(client) -> None`: `call("deleteWebhook", {"drop_pending_updates": False})`.
6. Tests with `httpx.MockTransport`, docstrings `D186` / `D175`; assert the request body for each call:
   - `sender_ids` over two private messages from ids 100000001 (`@a`) and 100000002 (no username), a repeat from
     100000001 and a group message → `[(100000001, "a"), (100000002, None)]`;
   - `sender_ids` when the API answers 409 → raises `WebhookActive`;
   - `set_webhook` with `https://h.example.ts.net/webhook/telegram` → body has the four keys above and the secret;
   - `set_webhook` with `http://…/webhook/telegram` and with `https://…/webhook/whatsapp` → `ValueError`, no request
     sent;
   - `webhook_info` over a full answer and over one without the error fields → the dataclass, `None` for missing
     fields;
   - `delete_webhook` → one `deleteWebhook` request.

Test: `uv run pytest -q tests/unit/test_telegram_setup.py && uv run lint-imports`

## Step 5: `telegram` CLI subcommands `[worker]`

Files: `src/invoicebot/cli.py`, `tests/unit/test_cli_telegram.py` (new).

1. Parser: a `telegram` subparser with its own subcommands (`dest="telegram_command"`, required): `ids` (no
   arguments) and `webhook` with a mutually exclusive required group `--url URL` | `--info` | `--delete`.
2. `main` dispatches `telegram` to `_telegram(parser, resolved, cfg, args, telegram_transport)`; `main` gains a keyword
   `telegram_transport: httpx.AsyncBaseTransport | None = None`, the same way `google_transport` is passed.
3. `_telegram`: `parser.error` when no Telegram channel is enabled in `cfg`, when `settings.secrets_path` or
   `settings.age_identity_path` is unset, or when the store lacks `telegram_bot_token` (names the secret, never a
   value). It loads the store the way `_auth_google` does, builds a `TelegramClient` over an `httpx.AsyncClient` with
   the given transport, and runs the step-4 function:
   - `ids` prints one line per sender, `<id> @<username>` or `<id> -`, and "no messages yet: press Start in the bot,
     then run again" when empty; `WebhookActive` → prints "a webhook is set; run `invoicebot telegram webhook
     --delete` first" to stderr and returns 1;
   - `webhook --url` reads `telegram_webhook_secret` from the store, calls `set_webhook`, prints "webhook set"; a
     `ValueError` becomes `parser.error`;
   - `webhook --info` prints `url`, `pending`, `last_error_date`, `last_error_message`, one per line;
   - `webhook --delete` prints "webhook deleted".
   A `TelegramError` prints its status and message (no URL, no token) and returns 1.
4. Tests in `tests/unit/test_cli_telegram.py`, docstrings `D186` / `R-9.16`, using a test age store and identity in
   `tmp_path` (as `tests/unit/test_cli_google_setup.py` does) and `httpx.MockTransport`:
   - `telegram ids` with two senders → exit 0, two lines in stdout;
   - `telegram ids` answered 409 → exit 1, the delete hint in stderr;
   - `telegram webhook --url https://h.example.ts.net/webhook/telegram` → exit 0, the request carries the stored
     secret;
   - `telegram webhook --info` → exit 0, four lines;
   - Telegram disabled in the config → exit 2;
   - a 401 answer → exit 1, and neither stdout nor stderr contains the token;
   - `--url` and `--info` together → exit 2.

Test: `uv run pytest -q tests/unit/test_cli_telegram.py tests/unit/test_cli.py tests/unit/test_docs.py`

## Step 6: Runbook and deploy guide `[inline: owner-facing instructions]`

1. `docs/runbook.md`: a new `## 11. Telegram bot` with: create the bot (roadmap owner setup steps 1–6, condensed); get
   sender ids with `podman exec "$APP" invoicebot telegram ids` before any webhook is set; register with `telegram
   webhook --url https://<machine>.<tailnet>.ts.net/webhook/telegram` and check with `--info`; "a sender must press
   Start first: a send before that fails for good (403)"; token rotation (BotFather `/revoke`, store the new token with
   §5, reload, run `telegram webhook --url` again); lost owning account (new bot, steps again, tell senders the new
   username). §5 item 5 names Telegram next to WhatsApp.
2. `deploy/README.md` § 5: add
   `sudo tailscale funnel --bg --set-path /webhook/telegram http://127.0.0.1:8080/webhook/telegram`.
3. `config/example.yaml`: the Telegram comment notes the D184 secret rule.
4. If `tests/unit/test_docs.py` checks CLI commands in the README, add `telegram` there.

Test: `uv run pytest -q tests/unit/test_docs.py`

## Step 7: Owner creates the bot `[inline: owner action]`

The owner does roadmap owner setup steps 1–6: two-step verification (their choice), `/newbot`, the token straight into
the live age store as `telegram_bot_token` with `scripts/secrets_edit.py` (runbook §5), a `telegram_webhook_secret`
from `secrets.token_urlsafe(32)`, `/setjoingroups` → Disable, and every sender opens the bot and presses Start. The
session never sees a value; the owner confirms each item.

Test: the owner confirms steps 1–6 are done.

## Step 8: Deploy, collect ids, enable the channel `[inline: live host]`

1. Deploy this branch's image with `deploy/deploy-live.sh` (Telegram still disabled in the live config).
2. `podman exec "$APP" invoicebot telegram ids` lists each sender; the owner puts each `telegram_user_id` into the
   live profiles (no ids in the repo) and sets `channels[telegram].enabled: true`. `preferred_channel` stays as is
   unless the owner wants admin alerts on Telegram (while Meta is pending, WhatsApp may be unreachable: suggest
   `telegram` for the owner's profile).
3. Reload; the log shows no `secrets:` problem and `/health` answers.

Test: `podman exec "$APP" invoicebot telegram ids` prints the senders; the reload log line is clean.

## Step 9: Open the ingress and register the webhook `[inline: live host]`

1. The owner runs the `tailscale funnel` line from step 6 (needs sudo); `tailscale funnel status` lists
   `/webhook/telegram`.
2. `podman exec "$APP" invoicebot telegram webhook --url https://<machine>.<tailnet>.ts.net/webhook/telegram`.
3. `telegram webhook --info`: `url` matches, `pending` 0, no `last_error_message`.
4. The owner sends `/ayuda` from Telegram and gets the help text back.

Test: `--info` shows no error and `/ayuda` is answered on Telegram.

## Step 10: Command menu entries from the registry `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `tests/unit/test_command_menu.py` (new).

1. Add `def menu_entries(is_admin: bool) -> list[tuple[str, str]]` (docstring: "Return the chat menu's `(command,
   description)` pairs the sender may run, in help order (D187)."). For each `name, command` in `COMMANDS` that
   `help_text` would list for `is_admin`, the description is `command.help` after its first `": "`. Reuse the same
   admin filter `help_text` uses (one home: extract a private `_visible(is_admin)` iterator both call).
2. Tests, docstrings `D187` / `R-10.1`:
   - `menu_entries(False)` names are `COMMANDS`' non-admin keys in order, without `gaps` and `gap`;
   - `menu_entries(True)` names are all `COMMANDS` keys in order;
   - the `estado` entry's description is `"cómo va el comprobante #12"`;
   - every name matches `[a-z0-9_]{1,32}` and every description is 3–256 characters (Telegram's limits);
   - `help_text(True)` is unchanged (equal to its value built by hand from `HELP_HEADER` and every help line).

Test: `uv run pytest -q tests/unit/test_command_menu.py tests/integration/test_commands.py`

## Step 11: `setMyCommands` call `[worker]`

Files: `src/invoicebot/channels/telegram/setup.py`, `tests/unit/test_telegram_setup.py`.

1. Add `async def set_commands(client, entries: Sequence[tuple[str, str]], chat_id: int | None = None) -> None`:
   `call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in entries], "scope": scope})`
   with `scope = {"type": "default"}` when `chat_id` is `None`, else `{"type": "chat", "chat_id": chat_id}` (D187).
   No import from `pipeline`: the caller passes the entries.
2. Tests with `httpx.MockTransport`, docstring `D187`: no `chat_id` → body has the two commands and the default scope;
   `chat_id=100000001` → the chat scope with that id; an empty `entries` → `"commands": []`.

Test: `uv run pytest -q tests/unit/test_telegram_setup.py && uv run lint-imports`

## Step 12: `telegram commands` subcommand `[worker]`

Files: `src/invoicebot/cli.py`, `tests/unit/test_cli_telegram.py`, `README.md`.

1. A `commands` subcommand under `telegram` (no arguments). `_telegram` needs only `telegram_bot_token` for it. It
   calls `set_commands(client, menu_entries(False))`, then for each profile in `cfg.profiles` that is `admin` and has
   a `telegram_user_id`, `set_commands(client, menu_entries(True), chat_id=<that id>)`; it prints `menu set:
   default` and one `menu set: <profile id>` line per admin (the profile id, never the user id).
2. README's CLI table row for `telegram` reads `ids\|webhook\|commands` and mentions the menu.
3. Tests in `tests/unit/test_cli_telegram.py`, docstring `D187`, over the example config with Telegram enabled and
   one admin profile with id 100000001: exit 0; two `setMyCommands` requests, the first with the default scope and
   no `gaps`, the second with the chat scope for 100000001 and `gaps`; stdout has `menu set: default` and the admin
   profile's id; the output never contains 100000001.

Test: `uv run pytest -q tests/unit/test_cli_telegram.py tests/unit/test_docs.py`

## Step 13: Live rehearsal from Telegram `[inline: live run]`

From the owner's Telegram, one at a time, checking each answer and the invoice state (`/estado`):

0. Redeploy this branch's image, run `invoicebot telegram commands`, and check the `/` menu on the owner's phone
   (admin entries included).
1. Flow A: a receipt photo (sent as a photo) → read, routed, PDF-only path (D142).
2. Flow B: a photo sent as a document (file) → read; the PDF and XML uploads as documents, quoting the bot's ask.
3. `personal_dni`: a DNI boleta → Sheets row, BR-D18.
4. A button press ([Editar] or [Confirmar]) → the client stops spinning (D177), the next message comes.
5. Commands: `/estado`, `/reprocesar <id>`, `/gaps`, `/cancelar`; a `/cmd@<bot>` form.
6. An XML sent from a Telegram client: note whether it arrives as `text/xml` or `application/octet-stream` (T1 left
   this to T2; an octet-stream XML is refused today).
7. An admin alert and a gap notice arrive on Telegram (the owner's `preferred_channel` from step 8).
8. Cross-channel `/estado` is skipped while WhatsApp is not live (9d2 covers it).

Test: each item answered as the specs say; any mismatch becomes a finding (fix on this branch, or a roadmap row).

## Step 14: Record the live run `[inline: live results]`

1. `specs/spikes/telegram.md` (new): date, image id, what each step-13 item did, the XML mime answer, anything that
   surprised.
2. `docs/plan-telegram.md`: T2 row Plan cell "Done <date>, `docs/archive/plan-telegram-live.md`"; any new follow-up
   (e.g. octet-stream XML by file name) as a new row or note.

Test: `uv run pytest -q tests/unit/test_docs.py`
