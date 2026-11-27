# Plan: MVP phase 9z7 · webhook-watch

Roadmap: `docs/plan-mvp.md`, row 9z7. Branch `feat/webhook-watch`, from `main` once 9z6 `ocr-buyer` is merged.

What the live run showed (#41, 2026-11-26): twice in one evening Telegram held the bot's updates (a photo, then the
owner's [Cancelar] and `/cancelar 41`) with `pending` above 0 and no `last_error`, while the app, the local proxy and
`tailscale funnel status` all looked healthy. A request to the Funnel's public addresses reached Tailscale's relay and
then hung; `tailscaled` had logged `invalid-packet-filter … rejecting all packets for safety` at its start. Checking
from the host itself proved nothing: MagicDNS resolves the funnel name to the tailnet address, which bypasses the public
relay. Re-registering the webhook got deliveries going once, but `telegram webhook --url` sends
`drop_pending_updates: true`, so the second time it threw the owner's three updates away.

Then the Funnel broke outright (#43, same night): its public addresses hung from outside, and the owner moved the
live bot to a Cloudflare quick tunnel started by hand (`systemd-run --user --unit invoicebot-cftunnel cloudflared
tunnel --url http://127.0.0.1:8080`). A quick tunnel's URL changes on every start, and cloudflared serves the current
hostname on its local metrics port (`curl -s 127.0.0.1:20241/quicktunnel` gives `{"hostname":"….trycloudflare.com"}`).
So one tunnel is not enough: the owner asked for a mechanism so this does not happen again, with the tunnel swappable.

Out of scope: a long-polling fallback for Telegram (a second receive path; worth it only if the watch below proves too
slow); WhatsApp's webhook, which is off in the live config (Funnel and Cloudflare both carry its path, so re-enabling
it needs only Meta's URL updated); a named Cloudflare tunnel with a fixed hostname (needs a domain); a VPS (D141).

Decision this plan proposes (step 1), **D201 The webhook route is watched from Telegram's side, repaired, and the
tunnel swapped when one fails**:

- Two tunnels, one in use: `funnel` (Tailscale Funnel, fixed URL) and `cloudflare` (a quick tunnel in the user unit
  `invoicebot-cftunnel`, URL read from cloudflared's metrics port 20241). `deploy/tunnel.sh up <name>` stops the
  other, starts this one, records the name in `${XDG_STATE_HOME:-~/.local/state}/invoicebot/tunnel` and prints the
  public base URL; with no record the tunnel is `funnel`.
- The signals are Telegram's own view: the webhook URL differs from the current tunnel's, or `getWebhookInfo`
  reports `pending` above 0 twice, 60 seconds apart. A check from the host is not used, since the host reaches its
  own funnel name through the tailnet.
- A user systemd timer runs the watch every 5 minutes. Tunnel down: start it. URL differs: re-point the webhook.
  Stuck: restart the tunnel and re-point; still stuck, switch to the other tunnel and re-point; still stuck, a
  desktop notification and a journal line. The webhook is never deleted.
- Re-registering the webhook keeps Telegram's pending updates (`drop_pending_updates` false), so re-pointing to a
  new URL loses nothing; only `--delete` and a by-hand drop decide their fate.
- The owner's user is the Tailscale operator (`tailscale set --operator`, done 2026-11-26), so the watch and the
  deploy need no sudo.

## Step 1: D201 and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D201 after D200 with the decision above, marked "(9z7 plan, proposed
  2026-11-26, from live #41 and #43; owner: a mechanism so this does not happen again, tunnel swappable)".
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

## Step 4: `deploy/tunnel.sh` and the Cloudflare unit `[worker]`

Files: `deploy/tunnel.sh` (new), `deploy/systemd/invoicebot-cftunnel.service` (new), `tests/deploy/test_tunnel.sh`
(new).

- `tunnel.sh`: bash, `set -euo pipefail`, header comment (D146, D201). Every external command (`tailscale`,
  `systemctl`, `curl`, `jq`, `sleep`) is called by name from `PATH`, so the test stubs them. State file
  `${XDG_STATE_HOME:-$HOME/.local/state}/invoicebot/tunnel`. Commands:
  - `up funnel`: `systemctl --user stop invoicebot-cftunnel` (its failure ignored), `tailscale funnel reset`, then
    `tailscale funnel --bg --set-path <path> http://127.0.0.1:8080<path>` for each of `/health`,
    `/webhook/telegram`, `/webhook/whatsapp` (one array), write `funnel` to the state file, print the URL.
  - `up cloudflare`: `tailscale funnel reset`, `systemctl --user restart invoicebot-cftunnel`, then poll the URL up
    to 30 times with `sleep 1` between; write `cloudflare` and print it, or exit 1 with `tunnel: no cloudflare
hostname after 30s` on stderr.
  - `url`: print the current tunnel's URL, exit 1 when it can't be read. Funnel: `https://` plus
    `tailscale status --json | jq -r .Self.DNSName` without its trailing dot. Cloudflare: `https://` plus
    `curl -fsS -m 3 http://127.0.0.1:20241/quicktunnel | jq -r .hostname`, empty counts as unreadable.
  - `current`: print the recorded name, `funnel` when there is none.
  - Anything else: a usage line on stderr, exit 2.
- `invoicebot-cftunnel.service`: `Description`, `ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate
--metrics 127.0.0.1:20241 --url http://127.0.0.1:8080`, `Restart=on-failure`, `RestartSec=10`. No `[Install]`:
  only `tunnel.sh` starts it.
- Test (bash, in the style of `tests/deploy/test_deploy.sh`): stubs on `PATH` that append their argv to a log;
  `curl` answers from a queue file, `tailscale status --json` prints a fixed DNS name with its dot, `XDG_STATE_HOME`
  is a temp dir. Cases: (1) `up funnel`: cftunnel stopped, reset, then the three `--set-path` calls in order, prints
  `https://host.tailnet.ts.net`, state `funnel`; (2) `up cloudflare`: funnel reset, cftunnel restarted, `curl`
  answers empty once then a hostname: prints `https://x.trycloudflare.com` after one sleep, state `cloudflare`; (3)
  `up cloudflare` with `curl` never answering: exit 1, the message on stderr, state unchanged; (4) `current` with no
  state file prints `funnel`; (5) `url` with state `cloudflare` and `curl` failing exits 1; (6) a failing `funnel
reset` in `up funnel` exits non-zero with no `--set-path` call; (7) `up nope` exits 2.

Test: `bash tests/deploy/test_tunnel.sh`

## Step 5: the watch script and its timer `[worker]`

Files: `deploy/webhook-watch.sh` (new), `deploy/systemd/invoicebot-webhook-watch.service` (new),
`deploy/systemd/invoicebot-webhook-watch.timer` (new), `tests/deploy/test_webhook_watch.sh` (new).

- `webhook-watch.sh`: bash, `set -euo pipefail`, header comment (D201). `PODMAN` (default `podman`), `TUNNEL`
  (default `tunnel.sh` beside the script) and `NOTIFY` (default `notify-send`) are overridable. The bot CLI runs as
  `$PODMAN exec invoicebot-live-app-1 invoicebot telegram webhook …`. Logs go to stdout (the journal). In order:
  1. `name=$($TUNNEL current)`; `base=$($TUNNEL url)`, or when that fails log `tunnel <name> down, starting` and
     `base=$($TUNNEL up <name>)`.
  2. Read the registered URL from `--info` (its `url: ` line). When it is not `$base/webhook/telegram`, run
     `--url "$base/webhook/telegram"` and log `webhook re-pointed to <name>`.
  3. Run `--check`. Exit 0: log `webhook ok`, exit 0. Any exit but 0 or 1 (app down): log it and exit with it,
     touching nothing.
  4. Exit 1: log `webhook stuck, restarting <name>`, `up <name>`, re-point, check; ok: log `webhook recovered`, exit 0.
  5. Still stuck: log `switching to <other>`, `up <other>`, re-point, check; ok: log `webhook recovered on <other>`,
     exit 0.
  6. Still stuck: `$NOTIFY "invoicebot: Telegram webhook stuck" "<hint: tailscale status, journalctl --user -u
invoicebot-cftunnel>"`, log it, exit 1.
     A failing `--info`, `--url` or `up` exits with its code (set -e), after the log line of the step it was in.
- `.service`: `Type=oneshot`, `Environment=INVOICEBOT_REPO=%h/WorkspaceMaku/Development/invoice-bot` on its own line
  (the one line another checkout edits) and `ExecStart=/usr/bin/env bash -c '"$INVOICEBOT_REPO"/deploy/webhook-watch.sh'`.
  `.timer`: `OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`, `WantedBy=timers.target`.
- Test (bash): a `podman` stub that answers `--info` from a file, pops `--check` exit codes from a queue file and logs
  `--url` calls; a `TUNNEL` stub that logs its argv, answers `current` and `url` from files (`url` exit code from a
  file too) and prints `https://<name>.example` for `up <name>`; a `NOTIFY` stub that logs. Cases: (1) URL matches,
  check ok: exit 0, no `up`, no `--url`, no notify; (2) URL differs: one `--url` with the tunnel's URL, exit 0; (3)
  `url` fails: `up funnel` then `--url`, exit 0; (4) stuck then ok: `up funnel` once, exit 0, no notify; (5) stuck,
  stuck, ok with current `funnel`: `up funnel` then `up cloudflare`, the last `--url` is
  `https://cloudflare.example/webhook/telegram`, exit 0, no notify; (6) stuck three times: one notify, exit 1; (7)
  check exits 125: no `up`, no notify, exit 125. Plus `systemd-analyze verify` on all three units under
  `deploy/systemd/` when the command exists (skip that case otherwise).

Test: `bash tests/deploy/test_webhook_watch.sh`

## Step 6: install on the live host `[inline: needs the owner's shell and the live host]`

Files: `deploy/README.md`.

- README, section 5 becomes "Public tunnel (D146, D201)": the two tunnels and when the watch swaps them; the
  operator line (`sudo tailscale set --operator=$USER`, once); `deploy/tunnel.sh up <name>` instead of the three
  `sudo tailscale funnel` lines; linking the three units from `deploy/systemd/` into `~/.config/systemd/user/`,
  `systemctl --user daemon-reload`, `systemctl --user enable --now invoicebot-webhook-watch.timer`, `loginctl
enable-linger $USER` so it runs without a login; how to read it (`journalctl --user -u invoicebot-webhook-watch`).
  Also: never delete or drop the webhook to unstick it; run the watch (`systemctl --user start
invoicebot-webhook-watch`).
- On the host (the owner runs the starts; the classifier refused my starting a tunnel on 2026-11-26): stop the
  transient `invoicebot-cftunnel`, link the units, daemon-reload, `deploy/tunnel.sh up cloudflare`, enable the timer,
  run the service once and read its journal.

Test: `systemctl --user list-timers invoicebot-webhook-watch.timer` shows the next run, and the service's journal
shows `webhook re-pointed to cloudflare` or `webhook ok`, then `webhook ok` on the next run.
