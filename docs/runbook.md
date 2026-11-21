# Runbook

Short procedures for the live host (S12.5). The host layout, deploys, rollback and the uptime check are in
[`deploy/README.md`](../deploy/README.md); this page links there instead of repeating it.

Every procedure runs from the repo checkout on the live host. Two shell variables are used throughout:

```sh
LIVE_ROOT="$HOME/.local/share/invoicebot"
APP=$(podman ps -q --filter label=com.docker.compose.project=invoicebot-live \
    --filter label=com.docker.compose.service=app)
```

**Reload** means `./deploy/deploy-live.sh --config "$LIVE_ROOT/config/config.yaml"`. It checks the config, installs
it and sends `SIGHUP`. The app then loads the whole config again: `config.yaml`, `stores/`, `portals/` and the
secrets. A bad config is refused: the app keeps running on the old one and the admins get BR-O05 with the reason. A
good one restarts the app on the new config (D155). Every start rechecks the open gaps (D156, D157).

## 1. Re-authorize Google and Microsoft

The bot asks for this with BR-D01: a revoked or expired token parks the invoice in `PENDING_AUTH`.

**Microsoft** (device code, runs inside the live app):

1. `podman exec -it "$APP" invoicebot auth-microsoft --account personal`
2. Open the URL it prints, enter the code and sign in with the owner's account.
3. The command moves every `PENDING_AUTH` invoice back to `DELIVERING` and prints how many.

**Google** (loopback sign-in, which the container cannot receive; recipe from `specs/spikes/host.md`):

1. Make a staging dir: `mkdir -m 700 /tmp/gauth`.
2. On the host, point the CLI at the live config (its defaults are the container's paths) and the dev database (the
   live one has no published port; it is used only for the resume query):

   ```sh
   export CONFIG_PATH="$LIVE_ROOT/config/config.yaml" SECRETS_PATH="$LIVE_ROOT/config/secrets.age" \
       AGE_IDENTITY_PATH="$LIVE_ROOT/config/age-identity.txt" DATA_DIR=/tmp/gauth \
       DATABASE_URL=postgresql://invoicebot:invoicebot@127.0.0.1:5432/invoicebot
   uv run invoicebot auth-google --account owner   # then --account invoices_a
   ```

   Approve every scope checkbox: an unticked scope is refused.

3. Copy the tokens into the live app, then restart it:

   ```sh
   podman cp /tmp/gauth/tokens/. "$APP":/data/tokens/
   podman exec -u 0 "$APP" sh -c 'chown bot /data/tokens/*.google.age && chmod 600 /data/tokens/*.google.age'
   podman restart "$APP"
   rm -rf /tmp/gauth
   ```

4. Shortcut: this recipe does not resume live invoices parked in `PENDING_AUTH`, because the host cannot reach the
   live database. Check with `/estado`, and resend any that are still waiting.

## 2. Add a portal and a store

Use this for a gap issue of kind `PORTAL_MISSING`, or for a new issuer.

1. In the repo, add `config/stores/<ruc>.yaml` and, for a new portal, `config/portals/<key>.yaml` with
   `status: active`. Start from an existing file.
2. Run `uv run python scripts/check_config.py config/config.yaml`. It must exit 0.
3. Open a PR. CI runs the same check with `--pr` and blocks a portal that is not `active`.
4. After the merge, copy the two files to `$LIVE_ROOT/config/stores/` and `$LIVE_ROOT/config/portals/` (mode
   `0644`), then **reload**.
5. What happens next: on the restart, each `PORTAL_MISSING` gap whose RUC now has an `active` portal is fixed. Its
   invoices go back to `FETCHING_DOCS`, and each sender gets BR-F12. Once an invoice gets past the fetch, the gap
   closes and its GitHub issue is commented and closed. If the fetch fails again, the gap goes back to `reported`,
   without a second issue.

## 3. Set a portal to `broken`

Use this when a portal's site changed and every fetch fails.

1. Set `status: broken` in `$LIVE_ROOT/config/portals/<key>.yaml`, then **reload**. The bot stops calling that
   portal (`PORTAL_INACTIVE`), and its invoices go to the next fetcher or to `MANUAL_REQUIRED`.
2. Don't commit the change to the repo: a PR that sets a portal to `broken` fails CI on purpose (R-11.6).
3. To fix it, update the portal's file in a PR, set it back to `active`, copy it to `$LIVE_ROOT/config/portals/`
   and **reload**. A `PORTAL_BROKEN` gap is fixed only when the portal is `active` and its entry changed (D157).

## 4. Rotate secrets and the GitHub App key

1. Edit the age store. It decrypts to a private temp file and re-encrypts on save:

   ```sh
   uv run python scripts/secrets_edit.py "$LIVE_ROOT/config/secrets.age" \
       --identity "$LIVE_ROOT/config/age-identity.txt"
   ```

2. Type each new value yourself. Never paste one into a chat.
3. **Reload.** Secrets are read again on the restart.
4. **GitHub App key:** in the App's settings, generate a new private key and put the whole PEM in
   `github_app_private_key` (steps 1–3). Delete the old key in GitHub only after the next daily GitHub App check
   passes (it runs at `gaps.digest_at`).
5. For a channel token (WhatsApp), first rotate it with the provider, then update the store and reload.

## 5. Handle a gap issue

The bot opens one issue per gap in the ops repo and sends a daily digest. `/gaps` in chat lists the open ones.

1. Read the issue's kind and subject.
2. Fix it by kind:
   - `PORTAL_MISSING`: [section 2](#2-add-a-portal-and-a-store).
   - `PORTAL_BROKEN`: [section 3](#3-set-a-portal-to-broken).
   - `TEMPLATE_INVALID`: fix the flow's sink entry in `config.yaml`, then **reload**.
   - `PARSER_MISS` and `UNROUTED_BUYER`: these never resume on their own. Fix the parsing rules or the routing
     rules, then handle the parked invoices by hand.
3. A fix you don't want to make: send `/gap ignorar <id>` in chat. The bot stops reporting that gap.

## 6. Restore a backup

1. Download the latest `db-<date>.dump.age` from the OneDrive backup folder. The matching age identity is kept off
   the bot host.
2. First prove the dump restores: run the [restore drill](../deploy/README.md#restore-drill-r-911) into an empty
   database on the **dev** Postgres. Record the run in its table.
3. Shortcut: a restore into the live database is not scripted yet. If it is ever needed, stop the live app first,
   restore the drilled dump into an empty live database with `pg_restore --no-owner`, then run
   `./deploy/deploy-live.sh <current tag>`. Rehearse it on dev before the first real use.

## 7. Postgres is down

1. `/health` answers 503 `{"db": "down"}`, and the uptime monitor alerts.
2. The webhook answers 503 too, so Meta keeps retrying. Leave the webhook on: a 200 would drop the message (R-9.15,
   [details](../deploy/README.md#postgres-down-the-webhook-answers-503-r-915)).
3. Check the database: `podman ps --filter label=com.docker.compose.project=invoicebot-live`, and
   `podman logs <postgres id>`.
4. Start it again: `podman start <postgres id>`. If the app is unhealthy after that, run `podman restart "$APP"`.
5. Messages Meta retried in the meantime are stored once. Jobs that were running are picked up again after their
   lease expires.

## 8. Refresh the padrón by hand

The app refreshes it weekly, at `sunat.padron.refresh_cron`. A failed refresh shows up as a `padron refresh failed`
warning in the logs, with its reason.

1. `podman exec "$APP" python scripts/import_padron.py --download`
2. It prints `rows: <n> skipped: <m>`. `error: padron refresh failed: size` means the ZIP is bigger than
   `sunat.padron.max_mb`. `status` means SUNAT did not answer 200.
3. For a ZIP you downloaded yourself, copy it in with `podman cp`, then run
   `podman exec "$APP" python scripts/import_padron.py /data/<file>.zip`.

## 9. Rehearse without WhatsApp (D165)

While Meta verification is pending, the dev-only `console` channel runs the same intake as the webhook. Its replies
stay in the outbox and are never sent anywhere. Config load refuses it with `env: production`.

1. Enable it: add `- { kind: console, profile: <owner profile> }` under `channels` in the live config, then
   **reload**.
2. Send a file. Its bytes come from stdin, and the extension picks the kind (`.jpg`, `.jpeg`, `.png`, `.pdf`,
   `.xml`):

   ```sh
   podman exec -i "$APP" invoicebot rehearse send --name f.jpg < f.jpg
   ```

   It prints `intake: <result>` and the invoice id. A text or command: `rehearse send --text "/ayuda"`.

3. Read the bot's replies. The last line is `next: --after <id>`; pass it back to see only newer rows:

   ```sh
   podman exec "$APP" invoicebot rehearse replies --after <id>
   ```

   A reply with buttons lists them as `[<id>] <title>`.

4. Press a button: `podman exec "$APP" invoicebot rehearse send --button <id>`.
5. When 9d2 `live-chat` starts, close the rehearsal first, then remove the `console` line from the live config and
   **reload**. With the line gone, a pending console reply has no transport and ends `dead` with an error per row,
   and a console download or attach job has no media source and parks.
   - Cancel each console invoice that is still open: `rehearse send --text "/cancelar <id>"`, then check its reply.
   - Retire the console replies nobody will read, in the live database:

     ```sh
     PG=$(podman ps -q --filter label=com.docker.compose.project=invoicebot-live \
         --filter label=com.docker.compose.service=postgres)
     podman exec "$PG" psql -U invoicebot invoicebot \
         -c "UPDATE outbox SET state = 'dead' WHERE channel = 'console' AND state = 'pending'"
     ```

**Known limit:** the console cannot reply to a message, so a PDF or XML sent while the profile has more than one open
invoice always gets BR-D14 ("reply to the right photo"). Rehearse a manual upload with a single open invoice: cancel
or finish the others first.
