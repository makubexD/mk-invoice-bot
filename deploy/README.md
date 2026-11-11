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

## WhatsApp channel

The app serves `/health` only until `CONFIG_DIR` holds both `config.yaml` and
`secrets.age`. With both present it mounts `/webhook/whatsapp` and starts the job
worker and the outbox sender. Downloaded media goes to the `appdata` volume at
`/data`.

The channel needs three secrets in the encrypted file (never in the environment
or `.env`; startup refuses secret-looking variables, R-9.9):

| Key | What it is |
|---|---|
| `whatsapp_token` | Graph API access token of the business number |
| `whatsapp_app_secret` | Meta app secret, used to check `X-Hub-Signature-256` |
| `whatsapp_verify_token` | Generate with `openssl rand -hex 32`; Meta echoes it in the handshake |

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
callback each time; quick tunnels are for trials only.

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
