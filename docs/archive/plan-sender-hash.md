# Plan: keyed sender hash

Roadmap: `docs/plan-mvp.md`, "Carried over from the `whatsapp` review", first item. Requirements: R-1.2, R-9.12. This must land before the first live trial with real senders.

Problem: `sender_hash` in `src/invoicebot/channels/whatsapp/signature.py` is an unkeyed, truncated SHA-256 of the phone number. There are only about 10^8–10^9 Peruvian mobile numbers, so anyone who reads a log line can brute-force the hash back to the number. The fix is an HMAC-SHA256 keyed with a new secret, `sender_hash_key`, from the age store.

Decisions:

- The secret is named `sender_hash_key`, without a channel prefix: the Telegram adapter will declare the same key later, so one sender gets the same digest on both channels' logs.
- The digest keeps its shape: the first 12 lower-case hex characters. Without the key, 48 bits is plenty for log correlation.
- The raw sender id never leaves `parse.py`: the key goes down into `parse_webhook`, which still returns digests only.
- The channel fails closed without the key: `sender_hash_key` joins `WHATSAPP_SECRETS`, so a missing key keeps the webhook unmounted, as with the other three secrets.

Lint for every step: `uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run lint-imports`. Step 1 changes `parse_webhook`'s signature and step 2 updates its caller, so after step 1 only `ruff` has to pass; `mypy` and `lint-imports` must pass after step 2.

## Step 1: Keyed `sender_hash` in the WhatsApp parser `[worker]`

Files: `src/invoicebot/channels/whatsapp/signature.py`, `src/invoicebot/channels/whatsapp/parse.py`, `tests/unit/test_whatsapp_signature.py`, `tests/unit/test_whatsapp_parse.py`.

- `signature.py`: change `sender_hash(sender_id: str) -> str` to `sender_hash(key: str, sender_id: str) -> str`. It returns the first `_HASH_LENGTH` (12) hex characters of `hmac.new(key.encode("utf-8"), sender_id.encode("utf-8"), hashlib.sha256).hexdigest()`. An empty `key` raises `ValueError("sender hash key is empty")`. Docstring: "Return the 12-character keyed hash under which a sender id may be logged (R-1.2, R-9.12)." Update the module docstring's last sentence to "Sender ids are only ever logged as a short keyed hash."
- `parse.py`: `parse_webhook` gains a keyword-only `sender_hash_key: str` after `admin_profiles`. Pass it to `_parse_message` (also keyword-only, after `admin_profiles`), which calls `sender_hash(sender_hash_key, sender_id)` for an unknown sender. Update `parse_webhook`'s module docstring to say "keyed :func:`sender_hash` digest". Nothing else changes.
- Tests: update every existing call of `sender_hash` and `parse_webhook` in the two test files to pass a synthetic key constant, `HASH_KEY = "synthetic-hash-key"`. Replace `test_sender_hash_is_short_stable_and_hides_the_id` with these cases, each docstring starting with `R-1.2, R-9.12:`:
  1. `sender_hash(HASH_KEY, "51999000001")` is 12 lower-case hex characters, equals a second call with the same arguments, and does not contain `"51999000001"`.
  2. The digest equals `hmac.new(HASH_KEY.encode(), b"51999000001", hashlib.sha256).hexdigest()[:12]`.
  3. The digest differs from `hashlib.sha256(b"51999000001").hexdigest()[:12]` (the old unkeyed value).
  4. The same id under two different keys gives two different digests.
  5. `sender_hash("", "51999000001")` raises `ValueError` matching `"empty"`.
- `test_whatsapp_parse.py`: the existing unknown-sender test (around line 178) expects `[sender_hash(HASH_KEY, UNKNOWN_NUMBER)]`.

Test: `uv run pytest -q tests/unit/test_whatsapp_signature.py tests/unit/test_whatsapp_parse.py`

## Step 2: Wire `sender_hash_key` from the age store `[worker]`

Files: `src/invoicebot/api/webhook.py`, `src/invoicebot/app.py`, `tests/integration/test_webhook.py`, `tests/unit/test_health.py`.

- `webhook.py`: add the field `sender_hash_key: str` to `WebhookDeps`, after `verify_token`. `receive` passes `sender_hash_key=deps.sender_hash_key` to `parse_webhook`.
- `app.py`: add `"sender_hash_key"` to `WHATSAPP_SECRETS`. `_webhook_deps` sets `sender_hash_key=secrets.get("sender_hash_key")`.
- `tests/unit/test_health.py`: `_write_runtime_files` adds `"sender_hash_key": "synthetic-hash-value"` to the encrypted values, so the existing wired-runtime tests still mount the webhook.
- `tests/integration/test_webhook.py`: add the constant `HASH_KEY = "synthetic-hash-key"` beside `APP_KEY`; `_deps` passes `sender_hash_key=HASH_KEY`; the unknown-sender test (around line 277) expects `f"ignored sender {sender_hash(HASH_KEY, unknown_number)}"`. Add to that test, under its existing docstring ID R-1.2, an assertion that the unkeyed `hashlib.sha256(unknown_number.encode()).hexdigest()[:12]` is not in `caplog.text`.

Test: `podman compose -f deploy/docker-compose.yml up -d postgres && uv run pytest -q tests/unit/test_health.py tests/integration/test_webhook.py`

## Step 3: Document the new secret `[worker:low]`

Files: `deploy/README.md`.

- In the WhatsApp section, "The channel needs three secrets" becomes "four secrets". Add a table row after `whatsapp_verify_token`: `` | `sender_hash_key` | Generate with `openssl rand -hex 32`; keys the hash under which unknown senders are logged (R-9.12). Changing it changes every logged digest | ``.

Test: `grep -c "sender_hash_key" deploy/README.md` prints `1`.
