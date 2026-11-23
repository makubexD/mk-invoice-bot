# Roadmap: Telegram as a second chat channel

Goal (owner, 2026-11-23): WhatsApp **and** Telegram are both full interfaces to every flow (A, B, `personal_dni`), every
command (`/reprocesar`, `/estado`, `/gaps`, `/pagado`, `/portal`, …), every button ([Editar], [Aprobar], [Confirmar],
…), uploads (photo, PDF, XML), admin alerts and gap notices. Each channel is optional; the bot runs with either or
both, so it still works if Meta never verifies the WhatsApp number (the worst case).

Specs that already cover it: R-1.7 (Telegram through the same port, allowlist by user id, secret-token webhook, inline
keyboards), R-1.8 (idempotent per `(channel, external_id)`), R-1.9 (replies on the origin channel, admin alerts on the
owner's preferred channel), R-1.10 (window and templates are WhatsApp only), R-1.11 (Telegram `getFile` caps media at
20 MB), R-9.16 (the bot token is in URLs: redact it), BR-O03 (a Telegram 401 is a rejected token), backlog E14/M8
(S14.1 adapter, S14.2 cross-channel `/estado`), S1.7 (both ids resolve to one profile).

## Where the code stands (analysis 2026-11-23)

Already channel-neutral: canonical `InboundMessage`/`OutboundMessage` (`channels/base.py`, `ChannelName` already lists
`telegram`); the outbox `Transport` port and `VALID_CHANNELS`; `MediaSource`; `Addressee(channel, profile_id)` with
replies following `invoices.origin_channel`; quote lookup by `(channel, external_id)`; the schema accepts `telegram`;
`Profile.telegram_user_id` and `preferred_channel` exist; message texts carry no WhatsApp markup.

Not yet open/closed (adding Telegram today would edit these):

- Four pipeline modules import the button codec from `channels/whatsapp/payloads.py`; `pipeline/uploads.py` catches
  `WhatsAppError` and `MediaChecksumMismatch`. `.importlinter` carries six waivers for them.
- `sender_hash` lives in `channels/whatsapp/signature.py` and its key sits in the WhatsApp secret view.
- `api/webhook.py` mixes WhatsApp verification with the generic store-then-acknowledge intake.
- `app.py` wires each channel with its own `if` and refuses to start anything without WhatsApp (`app.py:272`).
- The console channel (D165), the second channel, had to touch the pipeline, the webhook module and `app.py` in three
  places: proof that the seam is missing.

## Decisions (owner answers 2026-11-23; recorded as D173–D179 in T0 step 1)

- **D173 ingress:** a webhook at `/webhook/telegram` through the existing Tailscale Funnel, verified with the
  `X-Telegram-Bot-Api-Secret-Token` header (`hmac.compare_digest`), acknowledged only after the update is stored
  (constitution). `setWebhook` runs from a CLI subcommand with `allowed_updates=["message","callback_query"]`.
- **D174 every channel optional:** startup needs at least one enabled chat channel; in production that is WhatsApp,
  Telegram or both (console stays development-only, D165). Config load refuses a profile whose `preferred_channel` is
  not an enabled channel it has an id for, so admin alerts always have a route.
- **D175 identity:** `telegram_user_id` in the profile config is the allowlist; only private chats are accepted
  (`chat.id == from.id`); unknown ids are ignored like unknown WhatsApp numbers.
- **D176 cross-channel:** an upload matches the profile's open invoices on any of its channels; `/estado` works from
  either; the bot always answers on the invoice's origin channel (S1.7, S14.2). Quote resolution stays within one
  channel.
- **D177 button acknowledgement:** a Telegram button press is answered with `answerCallbackQuery` by the ingress right
  after the press is stored. It is an acknowledgement, not a message, so it bypasses the outbox; a failure is logged
  and ignored.
- **D178 Telegram message ids:** `external_id` is `"<chat_id>:<message_id>"` for both inbound messages and the ids the
  transport returns, since Telegram message ids are unique only per chat. A button press dedupes on the
  `callback_query.id`.
- **D179 bot owner:** the bot is created with BotFather from the owner's personal Telegram account; no dedicated
  number. A Telegram bot is its own identity (`@username` plus token), not a phone account: senders see the bot, never
  the owner's number, and the bot sees only numeric user ids. Two-step verification on that account is recommended, not
  required; the owner decides at T2 (2026-11-23). Losing that account means a new bot, a new token and a new
  `setWebhook` (runbook step 11).

## Phases

| Phase | Plan | What | Needs from the owner |
| --- | --- | --- | --- |
| T0 `telegram-seams` | Done 2026-11-23, `docs/archive/plan-telegram-seams.md` | Refactor with no behavior change: neutral button codec, media errors and sender hash; shared intake route; a channel registry (`wiring/`) so `app.py` iterates builders; any channel optional (D174). After T0, a channel is a new package plus one line in the builder tuple. | Nothing |
| T1 `telegram-adapter` | Done 2026-11-23, `docs/archive/plan-telegram-adapter.md` | `channels/telegram/` client, parser, transport, media source; `api/telegram_webhook.py`; `wiring/telegram.py`; the conversation tests parametrized over fake, WhatsApp and Telegram fixtures (S14.1); cross-channel `/estado` and upload tests (S14.2, D176); token-in-URL redaction test (R-9.16). All offline with `httpx.MockTransport` (D182). | Nothing |
| T2 `telegram-live` | `docs/plan-telegram-live.md` | Bot from BotFather (owner setup below, D179); `telegram_bot_token` and `telegram_webhook_secret` in the age store; Funnel path; `setWebhook`; each profile's `telegram_user_id`; live rehearsal of flows A, B and `personal_dni`, commands, photo vs document upload, an admin alert, a gap notice; runbook entries (token rotation, "send /start first"). T1 review follow-ups: a strength rule for `telegram_webhook_secret` (Telegram allows `[A-Za-z0-9_-]{1,256}`; the owner picks the minimum), and a fallback channel for BR-O03 when the failing channel is the admin's preferred one (`shortcut:` in `wiring/channels.py`). | Bot token, user ids, the live run |

T1 sketch (sized for the step rules, ≤4 files each): config model and D-notes; client (`TelegramError(status,
retry_after)`); parser (text, largest photo, document, `callback_query`, `reply_to_message`, `/cmd@bot` stripping,
non-private chats refused); transport (plain text, one inline button per row, 429 → `SendLater`, 400/403 →
`PermanentSendError`, 401 → BR-O03); media source (`getFile`, 20 MB cap before reading); webhook router; callback
acknowledgement; wiring builder; parity and cross-channel tests; redaction test.

T0 review follow-ups for T1 (2026-11-23):
- Startup and the reload check must agree: `_secret_problem` iterates `CHANNEL_BUILDERS` and special-cases the console,
  while startup keeps whichever builders return a wiring. Give both one rule from the builders (e.g. a `configured(cfg)`
  predicate on the port), so the console builder is not special-cased and "no channel" comes from the builders.
- Until `wiring/telegram.py` exists, `telegram.enabled: true` passes config validation with no transport behind it.
  The T1 builder closes this; if T1 slips, have `check_config` block it.
- `import_origin_channel` uses the owner's preferred channel even when `import_claims --profile` names another profile.
- `tests/unit/test_app_wiring.py` imports the private `_whatsapp_secrets`; test through `secret_problem` instead.
- The `.importlinter` "Only the composition root builds channels" contract lists only some packages; forbid
  `invoicebot.wiring` everywhere except `invoicebot.app`.
- `BUTTON_TITLE_MAX` (config) and `BUTTONS_LABEL_MAX_CHARS` (WhatsApp payloads) both hold 20; a test should pin the
  config cap to at most each channel's cap.

Telegram facts the design relies on: `callback_data` ≤ 64 bytes (today's longest button id is 49); `sendMessage` ≤
4096 characters, sent without `parse_mode`; `getFile` downloads ≤ 20 MB; about 1 message/s per chat and 30/s overall;
a user must message the bot first (a 403 before that is permanent); no 24-hour window and no templates.

## T2 owner setup, step by step (D179)

The T2 plan turns these into its owner steps. Nothing here needs a second SIM or phone.

1. **Protect the owning account (owner's choice, recommended).** In Telegram, Settings → Privacy and Security →
   Two-Step Verification: set a password and a recovery email. The bot is managed from this account, and without it
   anyone who receives an SMS code for the number can take the bot.
2. **Create the bot.** Open `@BotFather` (the verified one) and send `/newbot`. Give a display name (e.g. "Facturas")
   and a username ending in `bot` (e.g. `mk_facturas_bot`); it is public and cannot be changed later.
3. **Store the token at once.** BotFather replies with the token. Put it straight into the age store as
   `telegram_bot_token`; never paste it into a chat, the repo, a shell history or an environment variable (R-9.9,
   R-9.16). Then delete BotFather's message holding it.
4. **Make the webhook secret.** Generate `telegram_webhook_secret` with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` (Telegram allows `A-Z a-z 0-9 _ -`, 1–256
   characters) and store it the same way (D173).
5. **Close the bot to groups.** In BotFather: `/setjoingroups` → Disable (D175 accepts private chats only). Optional:
   `/setdescription`, `/setabouttext`, `/setuserpic`, and `/setcommands` with the Spanish command list, so the
   commands appear in Telegram's menu.
6. **Each user presses Start.** The owner and every other sender open `t.me/<username>` and press **Start**; a bot
   cannot write first (a 403 before that is permanent).
7. **Collect user ids.** Before `setWebhook` (`getUpdates` stops working once a webhook is set), read each sender's
   numeric `from.id` from one message they sent the bot, and put it in their profile as `telegram_user_id` (D175).
   Use `invoicebot telegram ids`, which reads the token from the store, so it is never typed (D186).
8. **Open the ingress.** Add the `/webhook/telegram` path to the Tailscale Funnel, deploy, reload, check `/health`.
9. **Register the webhook.** Run `invoicebot telegram webhook --url <https://…/webhook/telegram>` (D173, D186), then
   confirm with `invoicebot telegram webhook --info` that the URL is set and `last_error_message` is empty.
10. **Rehearse live.** Flows A, B and `personal_dni`, the commands, a photo and a document upload, a button press, an
    admin alert and a gap notice, all from Telegram.
11. **Runbook.** Token rotation: BotFather `/revoke`, store the new token, reload, run `setWebhook` again. Lost
    owning account: create a new bot (steps 2–9) and tell senders the new username.

