# Deploying the invoice bot

`deploy/deploy.sh` switches the app image with a database backup, migrations and a
health check, rolls back to the previous image when the new one does not become
healthy, and installs config-only changes with a `SIGHUP` (R-9.13, D46).

Everything works the same with Podman and Docker. The script picks the engine from
`CONTAINER_ENGINE`; when it is unset it uses `podman` if present, else `docker`.

## Prerequisites

- Bash, `curl` and `gzip`.
- Podman with `podman-compose` installed (the `podman compose` provider), or
  Docker with the `compose` plugin.
- The image tag being deployed must exist locally or be pullable: build it first
  or let CI push it.
- The Compose stack (`deploy/docker-compose.yml`) must be usable by the same user
  that runs the script.

## OCR engine (D81)

The bot reads every photo with Tesseract 5 and its Spanish model. The image
installs the Debian packages `tesseract-ocr` and `tesseract-ocr-spa`; CI installs
the same packages before pytest. On a development machine:

```sh
sudo apt install --no-install-recommends tesseract-ocr tesseract-ocr-spa
tesseract --version                      # 5.x
tesseract --list-langs | grep -x spa     # the Spanish model is present
```

## Browser for portals (D82)

Portal fetchers drive Chromium's headless shell through Playwright, in a helper
process started per fetch. The image installs it into `/opt/ms-playwright` with its
system libraries; CI installs it before pytest. On a development machine:

```sh
uv run playwright install --only-shell --with-deps chromium
uv run python -c "import playwright.async_api"   # the Python package loads
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CONTAINER_ENGINE` | `podman` if on `PATH`, else `docker` | Container engine for every call |
| `APP_IMAGE_REPO` | `localhost/invoicebot-app` | Image repository; the tag is appended |
| `BACKUP_DIR` | `/data/backups` | `pg_dump` output, `pre-<tag>.sql.gz` |
| `STATE_DIR` | `/data/deploy` | Holds `current_tag`, the last healthy tag |
| `CONFIG_DIR` | `<repo>/config` | Runtime config directory mounted into `app` |
| `HEALTH_URL` | `http://localhost:8080/health` | Endpoint polled after deploy |
| `HEALTH_TIMEOUT_S` | `60` | Health wait for postgres and the app |

Exit codes: `0` success, `1` deploy or validation failure, `2` usage error.

## Local run

Build the tag first (the deploy script never builds):

```sh
podman build -f deploy/Dockerfile -t localhost/invoicebot-app:v1 .
```

Then deploy it. The defaults write to `/data`, so point the state and backup
directories at a local path:

```sh
BACKUP_DIR="$PWD/data/backups" STATE_DIR="$PWD/data/deploy" \
    ./deploy/deploy.sh v1
```

Check it:

```sh
curl -s localhost:8080/health
cat data/deploy/current_tag
```

With Docker, same commands; only the engine changes:

```sh
docker build -f deploy/Dockerfile -t localhost/invoicebot-app:v1 .
CONTAINER_ENGINE=docker BACKUP_DIR="$PWD/data/backups" STATE_DIR="$PWD/data/deploy" \
    ./deploy/deploy.sh v1
```

The script itself always calls `"$CONTAINER_ENGINE" compose -f deploy/docker-compose.yml`,
so there is no separate Compose command to remember.

### Local checks

```sh
bash tests/deploy/test_deploy.sh     # good deploy, rollback, invalid and valid config
```

The test builds throwaway tags from the same Dockerfile and leaves postgres and
its volume running.

## VPS run

Run from a checkout of the repository so `deploy/docker-compose.yml`,
`deploy/deploy.sh` and `config/` are present. Keep state outside the checkout:

```sh
CONFIG_DIR=/srv/invoicebot/config \
BACKUP_DIR=/srv/invoicebot/backups \
STATE_DIR=/srv/invoicebot/deploy \
    ./deploy/deploy.sh <image_tag>
```

`<image_tag>` is normally the commit SHA pushed by CI. The user running the
script needs access to the engine socket (or rootless Podman) and to
`CONFIG_DIR`, `BACKUP_DIR` and `STATE_DIR`. Rootless Podman on an SELinux host
(see below) needs the SELinux labels fixed before the app can read its config.

## Local MVP host (D141, D146–D148)

The MVP runs on the owner's Ubuntu machine as a second Compose project,
`invoicebot-live`, reached through Tailscale Funnel. `deploy/deploy-live.sh`
wraps `deploy/deploy.sh`: it resolves the live directories, exports them and
passes the arguments through unchanged.

### 1. Layout

Everything lives under `LIVE_ROOT`, `$HOME/.local/share/invoicebot` by default
(mode `700`). Each variable keeps a value already set in the environment:

| Directory | Default | Holds |
|---|---|---|
| `CONFIG_DIR` | `$LIVE_ROOT/config` | `config.yaml`, `secrets.age`, `age-identity.txt` |
| `BACKUP_DIR` | `$LIVE_ROOT/backups` | `pre-<tag>.sql.gz` dumps |
| `STATE_DIR` | `$LIVE_ROOT/deploy` | `current_tag`, the last healthy tag |
| `DB_SECRETS_DIR` | `$LIVE_ROOT/db` | `postgres-password` and `pgpass` |

Live runtime config goes in `$LIVE_ROOT/config`, not in the repo's `config/`.
`DB_SECRETS_DIR` is created by the first `deploy-live.sh` run, which generates
the database password once into `postgres-password` and `pgpass` (both mode
`600`, `pgpass` owned by uid `10001`). Never commit these files or copy them
into the repo.

The age identity file must be readable by the container's `bot` user (uid
`10001`):

```sh
podman unshare chown 10001:10001 "$HOME/.local/share/invoicebot/config/age-identity.txt"
```

### 2. Deploy

Build the tag from the checkout, then deploy it. `deploy-live.sh <tag>` backs up,
migrates, switches the image and health-checks; with `--config` it validates and
installs a config file and sends `SIGHUP`:

```sh
podman build -f deploy/Dockerfile -t localhost/invoicebot-app:$(git rev-parse --short=12 HEAD) .
./deploy/deploy-live.sh <tag>
./deploy/deploy-live.sh --config <candidate>
```

### 3. Start on boot

Let the user manager start without a login session, and enable the Podman
restart unit. That unit starts only containers with `restart: always`, so
`compose.live.yml` sets it on the live app and Postgres; `unless-stopped`
containers stay down after a reboot:

```sh
loginctl enable-linger "$USER"
systemctl --user enable --now podman-restart.service
```

### 4. Helper sandbox (D147)

The sandbox is on by default in the image (`HELPER_SANDBOX=1`). Each helper runs
in `unshare` namespaces:

- the extract (OCR) helper runs without network, with an empty `/proc` and the
  runtime config and secrets directories hidden;
- the browser helpers (portal, SUNAT SOL) keep the network, and get a `$TMPDIR`
  capped at `HELPER_TMP_MB` (default 256), the config and secrets directories
  hidden and Chromium's own sandbox.

Both set `oom_score_adj` to 1000, run the helper without the privileges to
unmount a mask, and kill every process it started when the job ends. The app refuses to start if the namespaces are
unavailable. Docker's default seccomp profile blocks `unshare`, so the image
needs Podman, or Docker with a profile that allows it. The check is:

```sh
bash tests/deploy/test_helper_sandbox.sh
```

### 5. Tailscale Funnel (D146)

Install Tailscale from tailscale.com, then sign in:

```sh
sudo tailscale up
```

Expose only the webhook and the health endpoint, both proxied to the app's
loopback port:

```sh
sudo tailscale funnel --bg --set-path /webhook/whatsapp http://127.0.0.1:8080/webhook/whatsapp
sudo tailscale funnel --bg --set-path /health http://127.0.0.1:8080/health
tailscale funnel status
```

The public URL is `https://<machine>.<tailnet>.ts.net`. The first `funnel`
command asks to allow Funnel in the tailnet policy. Turn it off with:

```sh
tailscale funnel reset
```

### 6. Uptime check (R-9.14)

Point the external uptime monitor at `https://<machine>.<tailnet>.ts.net/health`
and alert on any non-200.

### 7. Dev and live side by side

Both stacks publish the app on `127.0.0.1:8080`, so only one app runs at a time.
The dev stack (project `invoicebot`, port `5432`, dev password) stays for its
Postgres and the tests. The live Postgres publishes no port. Never point
`TEST_DATABASE_URL` at the live project.

## What the deploy does

1. Starts `postgres` and waits until it is healthy.
2. `pg_dump --exclude-table-data=kv` through `compose exec` into
   `BACKUP_DIR/pre-<tag>.sql.gz` (mode `600`). A failed dump aborts the deploy.
3. Runs `alembic upgrade head` in a one-off container of the **new** image.
4. Starts `app` with the new tag and recreates its container.
5. Polls `HEALTH_URL` every 2 s until 200 or `HEALTH_TIMEOUT_S`.
6. On success, writes the tag to `STATE_DIR/current_tag` atomically and exits 0.

## Rollback (R-9.13)

On a health failure the script prints the last app logs, redeploys the tag stored
in `STATE_DIR/current_tag`, waits for its health, and exits 1.

What rollback **does**:

- restart the previous image against the migrated database;
- leave `current_tag` pointing at the last healthy tag.

What rollback **does not** do:

- run `alembic downgrade`: migrations follow expand/contract, so the previous
  image works on the new schema; there is no down migration;
- restore the pre-deploy database backup automatically. The dump is at
  `BACKUP_DIR/pre-<tag>.sql.gz` if a manual restore is ever needed;
- rebuild or pull images. Both tags must already exist.

If no previous tag is recorded, the failure is reported and nothing is restarted.

## Restore drill (R-9.11)

Run the drill before go-live and after every schema change. It decrypts one
nightly dump, restores it into an **empty** database and runs smoke queries: the
`invoices`, `outbox` and `gaps` tables must exist, and it prints
`invoices: <n>`.

Download the latest `db-<date>.dump.age` from the OneDrive backup folder, use
the age identity that matches the backup recipient (kept off the bot host and
never under `config/`, which the backup tars), and never target the live
database:

```sh
createdb -U invoicebot invoicebot_drill
RESTORE_DATABASE_URL="postgresql://invoicebot:invoicebot@localhost:5432/invoicebot_drill" \
    ./scripts/restore_drill.sh ~/age-backup-identity.txt /path/to/db-2026-11-16.dump.age
```

The age CLI is not installed on the development host or in CI, so the script
decrypts with `pyrage` inside the repository's `uv` environment: it reads the
`AGE-SECRET-KEY-` line of the identity file and clears the plaintext dump on
exit. It needs `psql` and `pg_restore`. The drill refuses a target that already
has an `invoices` table, never echoes the database URL, and fails on any restore
error other than the `transaction_timeout` setting that a `pg_restore` newer
than the Postgres 16 server always adds.

Record each run:

| Date | Dump | Result |
|---|---|---|
| _before go-live_ | | |

## Uptime check (R-9.14)

`/health` runs `SELECT 1` and answers `200 {"status": "ok", "db": "ok"}`; when
Postgres is unreachable it answers `503 {"status": "degraded", "db": "down"}`.
Point an external uptime monitor (any free service) at the public tunnel URL's
`/health` and alert on any non-200.

## Postgres down: the webhook answers 503 (R-9.15)

If Postgres is unreachable, the webhook returns 503 without acknowledging the
delivery. Meta (and Telegram) retry, so no message is lost while the outage is
shorter than their retry window. Leave the endpoint enabled: answering 200
during an outage makes Meta treat the delivery as accepted and drop it. After
Postgres is back, a redelivery is stored once.

## WhatsApp channel

The app serves `/health` only until `CONFIG_DIR` holds both `config.yaml` and
`secrets.age`. With both present it mounts `/webhook/whatsapp` and starts the job
worker and the outbox sender. Downloaded media goes to the `appdata` volume at
`/data`.

The channel needs four secrets in the encrypted file (never in the environment
or `.env`; startup refuses secret-looking variables, R-9.9):

| Key | What it is |
|---|---|
| `whatsapp_token` | Graph API access token of the business number |
| `whatsapp_app_secret` | Meta app secret, used to check `X-Hub-Signature-256` |
| `whatsapp_verify_token` | Generate with `openssl rand -hex 32`; Meta echoes it in the handshake |
| `sender_hash_key` | Generate with `openssl rand -hex 32`; keys the hash under which unknown senders are logged (R-9.12). Changing it changes every logged digest |

If any of them is missing the webhook is not mounted and one error is logged.

Encrypt the file with `age` and keep its identity beside it as
`age-identity.txt` (the Compose defaults; change `SECRETS_PATH` or
`AGE_IDENTITY_PATH` to move them):

```sh
age-keygen -o config/age-identity.txt
chmod 600 config/age-identity.txt
age -r "$(age-keygen -y config/age-identity.txt)" -o config/secrets.age secrets.yaml
shred -u secrets.yaml
```

### Local trials through a Cloudflare quick tunnel (D31)

Meta needs a public HTTPS URL. For a local trial, a quick tunnel gives one with
no account and no open port:

```sh
cloudflared tunnel --url http://localhost:8080
```

It prints a `https://<random>.trycloudflare.com` URL. In the Meta app's WhatsApp
configuration set the callback URL to `<that URL>/webhook/whatsapp` and the
verify token to the value of `whatsapp_verify_token`, then subscribe to the
`messages` field. The URL changes every time the tunnel restarts, so update the
callback each time; quick tunnels are for one-off trials only. The local MVP
host uses Tailscale Funnel instead (D146).

## Laptop agent

The agent sends `laptop_outlook` jobs through Outlook classic on the owner's
laptop and mirrors invoice folders into the work OneDrive (R-7.14). **Outlook
classic is required**: the new Outlook for Windows has no COM automation, so the
agent cannot send (S0.11).

Copy to the laptop, for example into `C:\invoicebot`:

- `scripts/laptop_agent.ps1`;
- the key file, holding only the `laptop_agent_key` value read from
  `config/secrets.age` with `scripts/secrets_edit.py`;
- the allowlist file, one address per line. Its only entry is HR's address,
  `rrhh@example.com`; blank lines and lines starting with `#` are ignored.

Run it once by hand with `-DryRun`; it logs what it would send or copy and
changes nothing else:

```powershell
pwsh.exe -NoProfile -File C:\invoicebot\laptop_agent.ps1 `
    -OneDriveRoot "$env:USERPROFILE\OneDrive" `
    -WorkRoot "$env:USERPROFILE\OneDrive - Trabajo" `
    -KeyFile C:\invoicebot\laptop-agent.key `
    -AllowlistFile C:\invoicebot\allowlist.txt `
    -DryRun
```

Then schedule it every 5 minutes. The task runs as the signed-in user, in that
user's session, so Outlook classic is available:

```powershell
$agentScript = 'C:\invoicebot\laptop_agent.ps1'
$arguments = @(
    '-NoProfile'
    '-ExecutionPolicy Bypass'
    "-File `"$agentScript`""
    "-OneDriveRoot `"$env:USERPROFILE\OneDrive`""
    "-WorkRoot `"$env:USERPROFILE\OneDrive - Trabajo`""
    "-KeyFile `"C:\invoicebot\laptop-agent.key`""
    "-AllowlistFile `"C:\invoicebot\allowlist.txt`""
) -join ' '
$action = New-ScheduledTaskAction -Execute 'pwsh.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'InvoiceBotLaptopAgent' -Action $action -Trigger $trigger -RunLevel Limited
```

The log is `agent.log` beside the script (`C:\invoicebot\agent.log`; it holds folder names, so it stays off OneDrive): it appends every run, including the reason a
job was refused. `sent-jobs.txt` sits beside the script
(`C:\invoicebot\sent-jobs.txt`, one sent job id per line); an id is written
before Outlook sends, so a crash cannot send a job twice. Receipts go to the
personal OneDrive `_outbox/receipts/`, where the bot reads them and completes
the batch. The bot warns once when a job stays in `_outbox/` for 4 hours
(BR-D12) and when a receipt is overdue by 24 hours (D109).

## Outlook rule for `inbox_rule`

The bot sends from its own Outlook.com mailbox to the owner's work address. On
the work mailbox, create one rule in Outlook:

- **from** the bot's mailbox;
- **subject starts with** `Support release`;
- **forward to** HR (`rrhh@example.com`).

The bot cannot see the forward (auto-forwards don't appear in Sent Items, and
Microsoft 365 usually blocks forwarding to outside addresses), so the rule is
trusted (D67). The batch is `sent` when Graph accepts the bot's email, and the
completion message carries a **[No llegó]** button (BR-D13).

The rule depends on S0.11 tests (1) and (2): a test mail from the bot's mailbox
is forwarded to an internal address, and the same mail with three attachments
lands in the Inbox, not Junk.

## Config-only mode

The app reloads its config on `SIGHUP`, so a config change does not need a new
image:

```sh
./deploy/deploy.sh --config /path/to/candidate.yaml
```

The candidate is validated with `scripts/check_config.py` inside the currently
deployed image (read-only mount). If it is invalid the script exits 1 and leaves
`CONFIG_DIR/config.yaml` untouched. If it is valid, the file is copied into place
atomically (temp file + `mv`) and the running app container gets `SIGHUP`; the
script exits 0.

## Podman on SELinux hosts

Rootless Podman on an SELinux host (Fedora, RHEL and derivatives) labels bind
mounts so the container cannot read them unless asked to relabel. Add `:Z` to the
bind mount in `deploy/docker-compose.yml`:

```yaml
volumes:
  - ${CONFIG_DIR:-../config}:/app/runtime-config:ro,Z
```

The `--config` validation mount inside `deploy.sh` has the same limitation; use a
non-SELinux host or add the label there if your setup requires it. Also remember
that Podman needs `podman-compose` installed — `podman compose` delegates to it.
