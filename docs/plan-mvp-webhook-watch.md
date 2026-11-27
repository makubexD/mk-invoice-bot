# Plan: MVP phase 9z7 · webhook-watch

Roadmap: `docs/plan-mvp.md`, row 9z7. Branch `feat/webhook-watch`, from `main` once 9z6 `ocr-buyer` is merged.

What the live run showed (#41, 2026-11-26): twice in one evening Telegram held the bot's updates (a photo, then the
owner's [Cancelar] and `/cancelar 41`) with `pending` above 0 and no `last_error`, while the app, the local proxy and
`tailscale funnel status` all looked healthy. A request to the Funnel's public addresses reached Tailscale's relay and
then hung; `tailscaled` had logged `invalid-packet-filter … rejecting all packets for safety` at its start. Checking
from the host itself proved nothing: MagicDNS resolves the funnel name to the tailnet address, which bypasses the public
relay. Re-registering the webhook got deliveries going once, but `telegram webhook --url` sends
`drop_pending_updates: true`, so the second time it threw the owner's three updates away.

Out of scope: a long-polling fallback for Telegram (a second receive path; worth it only if the watch below proves too
slow); WhatsApp's webhook, which is off in the live config; a VPS (D141).

Decision this plan proposes (step 1), **D201 The webhook route is watched from Telegram's side and repaired**:

- The signal is Telegram's own view: `getWebhookInfo` reporting `pending` above 0 twice, 60 seconds apart, means the
  route is stuck. A check from the host is not used, since the host reaches its own funnel name through the tailnet.
- A user systemd timer runs the watch every 5 minutes. On a stuck route it re-applies the Funnel (`tailscale funnel
reset`, then the three paths from one list), waits, and checks again; still stuck, it sends a desktop notification
  and logs to the journal. It never re-registers the webhook.
- Re-registering the webhook keeps Telegram's pending updates; only `--delete` and a new URL decide their fate.
- The owner's user is the Tailscale operator (`tailscale set --operator`), so the watch and the deploy need no sudo.

## Step 1: D201 and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D201 after D200 with the decision above, marked "(9z7 plan, proposed
  2026-11-26, from live #41; owner: a mechanism so this does not happen again)".
- `docs/plan-mvp.md`: add row 9z7 `webhook-watch` after 9z6 (plan `docs/plan-mvp-webhook-watch.md`, D201).

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: re-registering keeps pending updates `[worker]`

Files: `src/invoicebot/channels/telegram/setup.py`, `tests/unit/test_telegram_setup.py`.

- `set_webhook` sends `"drop_pending_updates": False`; its docstring says re-registering keeps Telegram's pending
  updates (D201).

Cases (docstrings start with `D201:`; `httpx.MockTransport` as the existing tests do):

1. `set_webhook` sends `drop_pending_updates` false, and the URL, secret and `allowed_updates` as before.

Test: `uv run pytest -q tests/unit/test_telegram_setup.py`

## Step 3: `telegram webhook --check` `[worker]`

Files: `src/invoicebot/channels/telegram/setup.py`, `src/invoicebot/cli.py`, `tests/unit/test_telegram_setup.py`,
`tests/unit/test_cli_telegram.py`.

- `setup.py`: `async def webhook_stuck(client: TelegramClient, wait_s: float, sleep: Callable[[float],
Awaitable[None]]) -> bool`: calls `webhook_info`, and when `pending` is 0 returns False at once; otherwise awaits
  `sleep(wait_s)`, calls `webhook_info` again and returns whether `pending` is still above 0 (D201). Module constant
  `STUCK_WAIT_S = 60`.
- `cli.py`: a fourth action `--check` in the `webhook` group (help: "exit 1 when Telegram holds updates it cannot
  deliver"). It prints `webhook: ok` and returns 0, or `webhook: stuck (pending N)` to stderr and returns 1, with N the
  second count; it passes `asyncio.sleep` and `STUCK_WAIT_S`.

Cases (docstrings start with `D201:`; the sleep is a fake that records its argument):

1. First `pending` 0: returns False, one `getWebhookInfo` call, no sleep.
2. `pending` 2 then 0: returns False after one sleep of `wait_s`.
3. `pending` 2 then 3: returns True.
4. CLI `--check` with a healthy fake: exit 0 and `webhook: ok`.
5. CLI `--check` with a stuck fake: exit 1 and `webhook: stuck (pending 3)` on stderr.
6. `--check` together with `--info` is refused by argparse (exit 2).

Test: `uv run pytest -q tests/unit/test_telegram_setup.py tests/unit/test_cli_telegram.py`

## Step 4: `deploy/funnel.sh` re-applies the Funnel `[worker]`

Files: `deploy/funnel.sh` (new), `tests/deploy/test_funnel.sh` (new).

- Bash, `set -euo pipefail`, header comment (D146, D201). The three paths (`/health`, `/webhook/telegram`,
  `/webhook/whatsapp`, each to `http://127.0.0.1:8080<path>`) live in one array. It runs `tailscale funnel reset`,
  then `tailscale funnel --bg --set-path <path> <target>` for each, then `tailscale funnel status`. The `tailscale`
  command comes from `TAILSCALE` (default `tailscale`), so the test can stub it. With `--print` it echoes the commands
  instead of running them.
- Test (bash, in the style of `tests/deploy/test_deploy.sh`): a stub `tailscale` on `PATH` that appends its argv to a
  file. Cases: (1) a plain run calls `funnel reset` first, then the three `--set-path` calls in order, then
  `funnel status`; (2) `--print` calls nothing and prints the same five commands; (3) a failing `reset` stops the
  script with a non-zero exit and no `--set-path` call.

Test: `bash tests/deploy/test_funnel.sh`

## Step 5: the watch script and its timer `[worker]`

Files: `deploy/webhook-watch.sh` (new), `deploy/systemd/invoicebot-webhook-watch.service` (new),
`deploy/systemd/invoicebot-webhook-watch.timer` (new), `tests/deploy/test_webhook_watch.sh` (new).

- `webhook-watch.sh`: runs `$PODMAN exec invoicebot-live-app-1 invoicebot telegram webhook --check` (`PODMAN` default
  `podman`). Exit 0: log `webhook ok` and exit 0. Exit 1: log `webhook stuck, re-applying funnel`, run
  `deploy/funnel.sh` (path from the script's own directory), run the check again; ok: log `webhook recovered`, exit
  0; still stuck: run `$NOTIFY` (default `notify-send`) with `"invoicebot: Telegram webhook stuck"` and a hint to run
  `deploy/funnel.sh` and check `tailscale status`, log it, and exit 1. Any other exit of the check (app down): log it
  and exit with it, without touching the funnel. Logs go to stdout (the journal).
- `.service`: `Type=oneshot`, `Environment=INVOICEBOT_REPO=%h/WorkspaceMaku/Development/invoice-bot` on its own line
  (the one line another checkout edits) and `ExecStart=/usr/bin/env bash -c '"$INVOICEBOT_REPO"/deploy/webhook-watch.sh'`. `.timer`: `OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`, `WantedBy=timers.target`.
- Test (bash): stubs for `podman` (exit codes from a queue file), `tailscale` and `notify-send` on `PATH`. Cases: (1)
  check ok: exit 0, no funnel call, no notify; (2) stuck then ok: one funnel reset, exit 0, no notify; (3) stuck
  twice: one funnel reset, one notify, exit 1; (4) check exits 125 (container missing): no funnel call, no notify,
  exit 125. Plus `systemd-analyze verify` on both units when the command exists (skip that case otherwise).

Test: `bash tests/deploy/test_webhook_watch.sh`

## Step 6: install on the live host `[inline: needs the owner's sudo and the live host]`

Files: `deploy/README.md`.

- README, section 5: the operator line (`sudo tailscale set --operator=$USER`, once), `deploy/funnel.sh` instead of
  the three `sudo tailscale funnel` lines, and a "Webhook watch (D201)" part: copy or link the two units into
  `~/.config/systemd/user/`, `systemctl --user daemon-reload`, `systemctl --user enable --now
invoicebot-webhook-watch.timer`, `loginctl enable-linger $USER` so it runs without a login, and how to read it
  (`journalctl --user -u invoicebot-webhook-watch`). Also: never re-register the webhook to unstick it; run
  `deploy/funnel.sh`.
- On the host: link the units, enable the timer, run the service once and read its journal line.

Test: `systemctl --user list-timers invoicebot-webhook-watch.timer` shows the next run, and the service's journal
shows `webhook ok`.
