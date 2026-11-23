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

## Decisions (owner answers 2026-11-23; recorded as D173–D178 in T0 step 1)

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

## Phases

| Phase | Plan | What | Needs from the owner |
| --- | --- | --- | --- |
| T0 `telegram-seams` | `docs/plan-telegram-seams.md` | Refactor with no behavior change: neutral button codec, media errors and sender hash; shared intake route; a channel registry (`wiring/`) so `app.py` iterates builders; any channel optional (D174). After T0, a channel is a new package plus one line in the builder tuple. | Nothing |
| T1 `telegram-adapter` | drafted when T0 finishes | `channels/telegram/` client, parser, transport, media source; `api/telegram_webhook.py`; `wiring/telegram.py`; `TelegramChannel` config (`mode` fixed to webhook, `api_base`); the conversation tests parametrized over fake, WhatsApp and Telegram fixtures (S14.1); cross-channel `/estado` and upload tests (S14.2, D176); token-in-URL redaction test (R-9.16). All offline with `httpx.MockTransport`. | Nothing |
| T2 `telegram-live` | drafted when T1 finishes | Bot from BotFather; `telegram_bot_token` and `telegram_webhook_secret` in the age store; Funnel path; `setWebhook`; each profile's `telegram_user_id`; live rehearsal of flows A, B and `personal_dni`, commands, photo vs document upload, an admin alert, a gap notice; runbook entries (token rotation, "send /start first"). | Bot token, user ids, the live run |

T1 sketch (sized for the step rules, ≤4 files each): config model and D-notes; client (`TelegramError(status,
retry_after)`); parser (text, largest photo, document, `callback_query`, `reply_to_message`, `/cmd@bot` stripping,
non-private chats refused); transport (plain text, one inline button per row, 429 → `SendLater`, 400/403 →
`PermanentSendError`, 401 → BR-O03); media source (`getFile`, 20 MB cap before reading); webhook router; callback
acknowledgement; wiring builder; parity and cross-channel tests; redaction test.

Telegram facts the design relies on: `callback_data` ≤ 64 bytes (today's longest button id is 49); `sendMessage` ≤
4096 characters, sent without `parse_mode`; `getFile` downloads ≤ 20 MB; about 1 message/s per chat and 30/s overall;
a user must message the bot first (a 403 before that is permanent); no 24-hour window and no templates.
