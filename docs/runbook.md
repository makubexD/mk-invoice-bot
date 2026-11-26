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

## 3. Install or replace the Excel template (D86, R-7.10)

Use this for a gap issue of kind `TEMPLATE_INVALID`, or when the reimbursement form changes. The template is the
owner's own file: it never goes in git (D86).

1. Build a blank template from a filled form: clear the values of the cells the `excel_template` sink writes
   (`cells:` in `config.yaml`) and the cached values of the formulas that read them. Keep the formulas, styles and
   labels. The sheet name must match the sink's `sheet`, and the file must have no macros.
2. Copy it into the live `appdata` volume:

   ```sh
   VOL=$(podman volume inspect invoicebot-live_appdata --format '{{.Mountpoint}}')
   podman unshare sh -c "mkdir -p '$VOL/templates' && cp reembolsos.xlsx '$VOL/templates/' \
       && chown -R 10001:10001 '$VOL/templates' && chmod 0640 '$VOL/templates/reembolsos.xlsx'"
   podman exec "$APP" test -r /data/templates/reembolsos.xlsx && echo readable
   ```

3. Put the output of `sha256sum reembolsos.xlsx` in the sink's `template_sha256` in
   `$LIVE_ROOT/config/config.yaml`, then **reload**.
4. What happens next: on the restart, the `TEMPLATE_INVALID` gap for that sink is fixed because its entry changed, and
   each invoice it parked goes back to delivery (D157). A template whose hash does not match parks them again with
   BR-D02.

## 4. Set a portal to `broken`

Use this when a portal's site changed and every fetch fails.

1. Set `status: broken` in `$LIVE_ROOT/config/portals/<key>.yaml`, then **reload**. The bot stops calling that
   portal (`PORTAL_INACTIVE`), and its invoices go to the next fetcher or to `MANUAL_REQUIRED`.
2. Don't commit the change to the repo: a PR that sets a portal to `broken` fails CI on purpose (R-11.6).
3. To fix it, update the portal's file in a PR, set it back to `active`, copy it to `$LIVE_ROOT/config/portals/`
   and **reload**. A `PORTAL_BROKEN` gap is fixed only when the portal is `active` and its entry changed (D157).

## 5. Rotate secrets and the GitHub App key

1. Edit the age store. It decrypts to a private temp file and re-encrypts on save. The identity file belongs to the
   container's user, so the edit runs inside `podman unshare`, and the store goes back to mode 644 afterwards (the
   container reads it through that bit):

   ```sh
   podman unshare "$(command -v uv)" run python scripts/secrets_edit.py "$LIVE_ROOT/config/secrets.age" \
       --identity "$LIVE_ROOT/config/age-identity.txt"
   chmod 644 "$LIVE_ROOT/config/secrets.age"
   ```

   A Telegram token copied from BotFather can pick up a space where its message wraps: the token never has one.

2. Type each new value yourself. Never paste one into a chat.
3. **Reload.** Secrets are read again on the restart.
4. **GitHub App key:** in the App's settings, generate a new private key and put the whole PEM in
   `github_app_private_key` (steps 1–3). Delete the old key in GitHub only after the next daily GitHub App check
   passes (it runs at `gaps.digest_at`).
5. For a channel token (WhatsApp or Telegram), first rotate it with the provider, then update the store and reload.
   Telegram also needs §11 step 7, and so does a new `telegram_webhook_secret`: until `setWebhook` runs again,
   every update is refused with 401 and lost.

## 6. Handle a gap issue

The bot opens one issue per gap in the ops repo and sends a daily digest. `/gaps` in chat lists the open ones.

1. Read the issue's kind and subject.
2. Fix it by kind:
   - `PORTAL_MISSING`: [section 2](#2-add-a-portal-and-a-store).
   - `PORTAL_BROKEN`: [section 4](#4-set-a-portal-to-broken).
   - `TEMPLATE_INVALID`: [section 3](#3-install-or-replace-the-excel-template-d86-r-710).
   - `PARSER_MISS` and `UNROUTED_BUYER`: these never resume on their own. Fix the parsing rules or the routing
     rules, then handle the parked invoices by hand.
3. A fix you don't want to make: send `/gap ignorar <id>` in chat. The bot stops reporting that gap.

## 7. Restore a backup

1. Download the latest `db-<date>.dump.age` from the OneDrive backup folder. The matching age identity is kept off
   the bot host.
2. First prove the dump restores: run the [restore drill](../deploy/README.md#restore-drill-r-911) into an empty
   database on the **dev** Postgres. Record the run in its table.
3. Shortcut: a restore into the live database is not scripted yet. If it is ever needed, stop the live app first,
   restore the drilled dump into an empty live database with `pg_restore --no-owner`, then run
   `./deploy/deploy-live.sh <current tag>`. Rehearse it on dev before the first real use.

## 8. Postgres is down

1. `/health` answers 503 `{"db": "down"}`, and the uptime monitor alerts.
2. The webhook answers 503 too, so Meta keeps retrying. Leave the webhook on: a 200 would drop the message (R-9.15,
   [details](../deploy/README.md#postgres-down-the-webhook-answers-503-r-915)).
3. Check the database: `podman ps --filter label=com.docker.compose.project=invoicebot-live`, and
   `podman logs <postgres id>`.
4. Start it again: `podman start <postgres id>`. If the app is unhealthy after that, run `podman restart "$APP"`.
5. Messages Meta retried in the meantime are stored once. Jobs that were running are picked up again after their
   lease expires.

## 9. Refresh the padrón by hand

The app refreshes it weekly, at `sunat.padron.refresh_cron`. A failed refresh shows up as a `padron refresh failed`
warning in the logs, with its reason.

1. `podman exec "$APP" python scripts/import_padron.py --download`
2. It prints `rows: <n> skipped: <m>`. `error: padron refresh failed: size` means the ZIP is bigger than
   `sunat.padron.max_mb`. `status` means SUNAT did not answer 200.
3. For a ZIP you downloaded yourself, copy it in with `podman cp`, then run
   `podman exec "$APP" python scripts/import_padron.py /data/<file>.zip`.

## 10. Rehearse without WhatsApp (D165)

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
5. Quote a reply: `podman exec -i "$APP" invoicebot rehearse send --reply-to <id> --name f.pdf < f.pdf`, with the
   `#<id>` of the bot's message about that invoice; wait until `rehearse replies` shows it `sent`.
6. When 9d2 `live-chat` starts, close the rehearsal first, then remove the `console` line from the live config and
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

**Known limit:** a reply still `pending` has no channel message id yet, so a quote of it does not resolve and the
upload is handled as if unquoted.

## 11. Telegram bot (D179, D186)

The bot belongs to the owner's personal Telegram account. Senders see only the bot, never the owner's number. The
roadmap's "T2 owner setup" (`docs/plan-telegram.md`) has the reasons for each step.

1. **Protect the owning account (recommended).** In Telegram: Settings → Privacy and Security → Two-Step Verification,
   with a password and a recovery email.
2. **Create the bot.** In `@BotFather` send `/newbot`, a display name and a username ending in `bot`. The username is
   public and cannot be changed.
3. **Store the secrets (§5).** Put BotFather's token in the age store as `telegram_bot_token`, then delete
   BotFather's message. Make `telegram_webhook_secret` with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`. The app refuses a secret shorter than 32
   characters or outside `A-Z a-z 0-9 _ -` (D184). Never paste either value into a chat, the repo or a shell variable.
4. **Close the bot to groups.** In BotFather: `/setjoingroups` → Disable. Optional: `/setdescription` and
   `/setcommands`.
5. **Every sender presses Start.** Each sender opens `t.me/<username>` and presses **Start**. A bot cannot write
   first: until a sender has pressed Start, a send to them fails for good (403).
6. **Collect the sender ids** before any webhook is set (Telegram keeps the messages until then). The command reads
   the live config, so first set the `telegram` channel to `enabled: true` there and **reload**; no profile needs a
   `telegram_user_id` yet. Then:

   ```sh
   podman exec "$APP" invoicebot telegram ids
   ```

   It prints `<id> @<username>` per sender. Put each id in its profile as `telegram_user_id` in the live config,
   then **reload**. `a webhook is set` means step 7 already ran: run
   `telegram webhook --delete`, send a message to the bot, and try again.

7. **Register the webhook.** The Funnel must serve `/webhook/telegram` (`deploy/README.md` § 5). Then:

   ```sh
   podman exec "$APP" invoicebot telegram webhook --url https://<machine>.<tailnet>.ts.net/webhook/telegram
   podman exec "$APP" invoicebot telegram webhook --info
   ```

   `--info` should show the URL, `pending: 0` and `last_error_message: -`. Send `/ayuda` to the bot to confirm.

8. **Rotate the token.** In BotFather: `/revoke`. Store the new token (§5), **reload**, and run step 7 again.
9. **Lost owning account.** Create a new bot (steps 2–7) and tell every sender the new username. The old bot is no
   longer reachable.

**Photos that read well (BR-E01, D198).** A receipt photographed on a plain, dark background reads best; wood grain,
patterned cloth or other papers around it make OCR harder. In Telegram a photo sent as a file (📎 → File) keeps its
full size, while a normal photo is resized to about 1080 px, which can leave the QR too small to read.

## 12. Switch a flow's mail between review and auto (D194)

A flow's mailer sink has `send: review` or `send: auto` (the default). With `review`, `deliver` files the folder and
the Excel as usual, then posts the attachments as documents and BR-D19 with the mail's To, Cc, subject and body.
Nothing is sent until the owner taps **[Enviar]**; **[Cancelar]** only holds the mail, and the invoice stays filed. A
held mail has no reminder and no timeout; `/estado` shows it. With `auto`, the mail goes out at once, as before.

1. **Switch.** Set `send: review` or `send: auto` under the flow's `kind: mailer` sink in the live config, then
   **reload**. The change applies to the next delivery; a mail already waiting for review keeps waiting.
2. **Review a batch prepared earlier.** A flow B invoice that is `BATCHED` with a `prepared` batch, no laptop job and
   a flow now on `review` (for example a dry_run draft written before the switch) goes through review with
   `/revisar <n>`, sent by an admin. The bot answers at once, then posts the review as in step 1. Anything else
   answers BR-D22. `/revisar` is in the admin's Telegram menu only after `invoicebot telegram commands` runs again
   (§11) once the new image is live.
3. **Going back to `auto`** does not send a mail that is waiting: tap [Enviar] on it first, or it keeps waiting.

## 13. Forget an invoice (D196)

For an emergency, or to send the same invoice again from a clean slate. It runs on the host, never from the chat.
Only a `DONE`, `CANCELLED` or `FAILED` invoice can be forgotten; cancel a running one with `/cancelar` first.

```sh
podman exec "$APP" invoicebot invoice forget <id>                       # dry run: prints the plan, changes nothing
podman exec "$APP" invoicebot invoice forget <id> --yes                 # release its key only
podman exec "$APP" invoicebot invoice forget <id> --drive --purge --yes # delete its folder, files and rows
```

- **No flag** releases the invoice's natural key (BR-E04), so the same invoice is accepted again. The row, its
  history and its folder stay.
- **`--drive`** deletes the invoice's OneDrive folder (`flow_data.folder_path`, `_Sent` included) through the bot's
  own Graph access. Flow A (Sheets) is not covered: delete its row and folder by hand.
- **`--purge`** removes its host files (`tmp/<id>/`, `mail/<id>/`, `mail/<id>.eml`, its photos) and deletes its
  database rows.
- The drive goes first, then the files, then the database. A drive failure stops before anything else changes, and
  a rerun is safe.
- With `--drive --purge`, the month's next filing reuses the freed sequence number (D88); with `--drive` alone it
  does not, since the invoice still holds its number.
- A mail that was sent stays sent: nothing recalls it.
