# Plan: Telegram phase T0 · telegram-seams

Roadmap: `docs/plan-telegram.md`. A refactor with no behavior change for WhatsApp or the console: it moves the
channel-neutral pieces out of `channels/whatsapp/` and `api/webhook.py`, and turns the per-channel `if`s in `app.py`
into a registry of channel builders. After this phase, adding a channel is a new `channels/<name>/` package, a router
in `api/`, a builder in `wiring/` and one line in the builder tuple: nothing in the pipeline or another adapter
changes (CLAUDE.md, open/closed). It also makes every chat channel optional (D174), so the bot can run without
WhatsApp.

In scope: the button codec, media errors and sender hash move to `channels/`; the six `.importlinter` waivers go; the
shared store-then-acknowledge intake moves to `api/intake_route.py`; `wiring/` holds a `ChannelWiring` per channel;
startup needs at least one enabled chat channel; `preferred_channel` must be an enabled channel; small leaks
(`importer.py` origin channel, the "WhatsApp button title" check text).

Out of scope: anything Telegram-specific (T1), `ProfileIndex` reading per-channel ids through a mapping (no third
channel needs it yet).

## Step 1: D173–D178, roadmap rows `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after the last decision: rows D173–D178 exactly as worded in `docs/plan-telegram.md`
   § Decisions, each with status "(T0 plan, proposed)".
2. `docs/plan-mvp.md`: a row `10` `telegram` (roadmap: `docs/plan-telegram.md`, D173–D178) after 9d2, and update the
   "Carried over from the `whatsapp` review" Telegram bullet: media download is done (`MediaSource`, D165); the button
   codec, media errors and sender hash move in T0; checking the media id against its pattern at the parse boundary
   stays open for T1.

Test: `grep -n "D17[3-8]" specs/05-open-questions.md docs/plan-mvp.md` shows each line.

## Step 2: The button codec gets a channel-neutral home `[worker]`

Files: `src/invoicebot/channels/buttons.py` (new), `src/invoicebot/channels/whatsapp/payloads.py`,
`tests/unit/test_channels_buttons.py` (new), `tests/unit/test_whatsapp_payloads.py`.

1. Move `button_id`, `parse_button_id` and the constants and helpers only they use from `payloads.py` to
   `channels/buttons.py` (module docstring: "The button id codec every channel shares (R-3.5); ids fit Telegram's
   64-byte `callback_data`."). `payloads.py` imports them from there for its own use.
2. Move the codec's tests from `test_whatsapp_payloads.py` to `test_channels_buttons.py` unchanged, and add one test,
   docstring starting `R-1.7`: the longest id the codec can build (largest invoice id the column allows, longest
   action, `choice` and `menu` at their maxima) is at most 64 bytes in UTF-8.

Test: `uv run pytest -q tests/unit/test_channels_buttons.py tests/unit/test_whatsapp_payloads.py`

## Step 3: The pipeline imports the codec from `channels.buttons` `[inline: an import swap in four modules plus four linter lines]`

Files: `src/invoicebot/pipeline/{actions,questions,deliver,replies}.py`, `.importlinter`.

Each module imports `button_id` / `parse_button_id` from `invoicebot.channels.buttons`; delete the four
`pipeline.* -> channels.whatsapp.payloads` waivers in the `pipeline-is-channel-neutral` contract.

Test: `uv run lint-imports && uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_conversation_fake.py`

## Step 4: Media sources raise channel-neutral errors `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/channels/whatsapp/media.py`,
`src/invoicebot/pipeline/uploads.py`, `tests/integration/test_pipeline_uploads.py`.

1. `base.py`, next to `MediaSource`: `class MediaTransient(Exception)` ("a retry may fix it") and `class
   MediaUnusable(Exception)` ("this media can never be kept"). The `MediaSource` docstring says implementations raise
   only these two (or the `media_limits` errors they raise today).
2. `whatsapp/media.py`: wrap a `WhatsAppError` into `MediaTransient` when its status is retryable (move `_retryable`
   and its constants here from `uploads.py`) and into `MediaUnusable` otherwise; wrap `MediaChecksumMismatch` into
   `MediaUnusable`; `raise … from err` each time.
3. `uploads.py`: catch `MediaTransient` (today's retry path) and `MediaUnusable` (today's skip path) instead; drop the
   two WhatsApp imports and `_retryable`.
4. `test_pipeline_uploads.py`: fakes that raised `WhatsAppError` or `MediaChecksumMismatch` raise the neutral errors;
   the expected outcomes do not change.

Test: `uv run pytest -q tests/integration/test_pipeline_uploads.py && uv run mypy src`

## Step 5: WhatsApp media tests and the last two linter waivers `[worker]`

Files: `tests/unit/test_whatsapp_media.py`, `.importlinter`.

1. `test_whatsapp_media.py`: tests expecting `WhatsAppError` or `MediaChecksumMismatch` from the media source expect
   the neutral error instead; add tests, docstrings starting `R-1.11`: a 500 and a 429 give `MediaTransient`, a 404
   gives `MediaUnusable`, a checksum mismatch gives `MediaUnusable`, each with the original as `__cause__`.
2. `.importlinter`: delete the two `pipeline.uploads -> channels.whatsapp.*` waivers; the contract has no waivers
   left, so delete its `ignore_imports` key.

Test: `uv run pytest -q tests/unit/test_whatsapp_media.py && uv run lint-imports`

## Step 6: `sender_hash` gets a channel-neutral home `[worker]`

Files: `src/invoicebot/channels/sender_hash.py` (new), `src/invoicebot/channels/whatsapp/signature.py`, and every
module that imports `sender_hash` from it (today `channels/whatsapp/parse.py`, `api/webhook.py`, `app.py`), plus its
tests. If that is more than five files, stop and report the list.

Move `sender_hash` (and only it) to `channels/sender_hash.py`, module docstring "The keyed hash every channel logs a
sender as (R-9.16)". Importers use the new module; `signature.py` keeps only the WhatsApp signature check. Move its
tests to `tests/unit/test_channels_sender_hash.py` unchanged.

Test: `uv run pytest -q tests/unit/test_channels_sender_hash.py tests/unit/test_whatsapp_signature.py && uv run mypy src`

## Step 7: The sender hash key is a shared channel secret `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

1. `app.py`: take `sender_hash_key` out of `WHATSAPP_SECRETS`. Add `CHANNEL_ADAPTER = "channels"` and
   `CHANNEL_SECRETS = frozenset({SENDER_HASH_KEY})` (reuse the existing name constant) with
   `_channel_secrets(store)` returning `store.scoped(CHANNEL_ADAPTER, set(CHANNEL_SECRETS))`. Every place that read
   the key through the WhatsApp view reads it through the channel view. `_secret_problem` checks the channel view
   whenever any chat channel is enabled.
2. Tests, docstrings starting `R-9.10`: the WhatsApp view no longer declares `sender_hash_key`; the channel view
   declares only it.

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/integration/test_webhook.py tests/integration/test_app_reload.py && uv run mypy src`

## Step 8: The shared intake route `[worker]`

Files: `src/invoicebot/api/intake_route.py` (new), `src/invoicebot/api/webhook.py`,
`tests/integration/test_intake_route.py` (new).

1. Move `MAX_BODY_BYTES`, `_read_body`, `_parse_json`, `_intake` and `_persist` from `webhook.py` to
   `intake_route.py` as public `read_body`, `parse_json`, `intake` and `persist`. `intake` and `persist` take an
   `IntakeDeps` frozen dataclass holding only what they read from `WebhookDeps` today (pool, intake settings, …);
   `WebhookDeps` keeps the WhatsApp-only fields and carries an `IntakeDeps`. Module docstring: "Store an inbound batch,
   then acknowledge (constitution, R-1.8, R-9.15); every channel's router calls it."
2. `webhook.py` keeps only the WhatsApp routes and verification and calls the moved functions.
3. `test_intake_route.py`, docstrings citing R-1.8 and R-9.15: a body over `MAX_BODY_BYTES` is refused before it is
   read whole; a batch is stored before 200; a redelivered message is a duplicate; the database down gives 503.

Test: `uv run pytest -q tests/integration/test_intake_route.py tests/integration/test_webhook.py`

## Step 9: `preferred_channel` must be an enabled channel (D174) `[worker]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

1. `Config` validator: every profile's `preferred_channel` names a chat channel that is enabled in `channels:` and
   for which the profile has an id (`whatsapp` number or `telegram_user_id`; `console` needs none). The error names
   the profile and the channel, never the id.
2. Tests, docstrings starting `D174`: a profile preferring a disabled channel is refused; one preferring `telegram`
   without `telegram_user_id` is refused; `config/example.yaml` still loads.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 10: `ChannelWiring` and the WhatsApp builder `[worker]`

Files: `src/invoicebot/wiring/__init__.py` (new), `src/invoicebot/wiring/channels.py` (new),
`src/invoicebot/wiring/whatsapp.py` (new), `tests/unit/test_wiring_whatsapp.py` (new).

1. `wiring/` is the composition root's channel part (package docstring: "Builds each chat channel from config; only
   `app.py` imports it"). `wiring/channels.py`: a frozen dataclass `ChannelWiring(name: ChannelName, transport:
   Transport, media_source: MediaSource | None, router: APIRouter | None)` and a `Protocol` `ChannelBuilder`:
   `(cfg, store, http, intake_deps) -> ChannelWiring | None` (`None` when the channel is not enabled), plus
   `secret_problem(cfg, store) -> str | None`. Use the exact types `app.py` passes today; read it first.
2. `wiring/whatsapp.py`: a builder that does what `app.py` does today for WhatsApp (channel config, secrets view,
   `WhatsAppClient`, the numbers map, transport, media source, router). Do not change `app.py` yet; copying is
   expected in this step and removed in step 11.
3. Tests, docstrings starting `R-1.7`: disabled or missing config gives `None`; missing secrets give `None` and a
   `secret_problem`; a full config gives a wiring named `whatsapp` with a transport, a media source and a router.

Test: `uv run pytest -q tests/unit/test_wiring_whatsapp.py && uv run mypy src`

## Step 11: `app.py` iterates the channel builders `[worker:high: reasoning: rewiring the composition root's lifespan without changing startup, reload or fail-closed behavior]`

Files: `src/invoicebot/app.py`, `tests/integration/test_app_channels.py` (new).

1. `app.py`: `CHANNEL_BUILDERS: tuple[ChannelBuilder, ...] = (whatsapp_wiring,)`. The lifespan builds every wiring;
   the outbox `transports` map, the media source map and the mounted routers come from the wirings; the WhatsApp
   wiring code that step 10 copied is deleted here. `_secret_problem` asks each builder instead of naming
   `"secrets:whatsapp"`.
2. Fail closed per D174: with no wiring at all, log `no chat channel enabled` and serve `/health` only (today's
   behavior when WhatsApp is missing). With at least one, start the workers and the outbox.
3. Tests (integration), docstrings starting `D174`: WhatsApp configured mounts `/webhook/whatsapp` as before; no chat
   channel starts no workers; the existing lifespan and reload tests stay green.

Test: `uv run pytest -q tests/integration/test_app_channels.py tests/integration/test_app_reload.py tests/integration/test_webhook.py tests/unit/test_app_console.py && uv run mypy src`

## Step 12: The console channel is a builder too `[worker]`

Files: `src/invoicebot/wiring/console.py` (new), `src/invoicebot/app.py`, `tests/unit/test_app_console.py`.

`wiring/console.py`: a builder that does what the three `if console_channel(cfg)` blocks in `app.py` do (transport,
media source, no router); add it to `CHANNEL_BUILDERS` and delete those blocks. Add one test, docstring starting
`D174`: a development config with only the console channel (no WhatsApp) starts the workers and the outbox.

Test: `uv run pytest -q tests/unit/test_app_console.py tests/integration/test_app_channels.py && uv run mypy src`

## Step 13: The import contract for `wiring/` `[worker]`

Files: `.importlinter`.

Add a `forbidden` contract "Only the composition root builds channels": `invoicebot.channels`, `invoicebot.pipeline`,
`invoicebot.queue`, `invoicebot.store`, `invoicebot.rules` and `invoicebot.api` never import `invoicebot.wiring`.

Test: `uv run lint-imports`

## Step 14: Imported invoices take the owner's preferred channel `[worker]`

Files: `src/invoicebot/importer.py`, and the test file that tests the importer today.

`importer.py` (about line 70): the imported invoices' `origin_channel` is the owner profile's `preferred_channel`, not
the literal `"whatsapp"`. One test, docstring starting `R-1.9`: an owner preferring `telegram` imports with that origin
channel; the existing importer tests stay green.

Test: `uv run pytest -q -k importer && uv run mypy src`

## Step 15: The button-title check is channel-neutral `[worker]`

Files: `src/invoicebot/config/checks.py`, `scripts/check_config.py`, `tests/unit/test_config_checks.py`.

Keep the 20-character button-title cap (WhatsApp's, the strictest) but name it channel-neutral (`BUTTON_TITLE_MAX`)
and word the problem "button title over 20 characters"; update the existing tests for the wording.

Test: `uv run pytest -q tests/unit/test_config_checks.py && uv run mypy src`
