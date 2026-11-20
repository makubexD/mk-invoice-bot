# Plan: MVP phase 9d2 · live-chat

Roadmap: `docs/plan-mvp.md` row 9d2 (D154). The live runs that need a chat reply, moved out of `docs/archive/plan-mvp-ops-live.md` so that plan could finish while Meta business verification is pending. The steps are unchanged; step numbers in their text that point at `ops-live` steps refer to that plan.

**Needs from the owner first:** step 1 needs only the owner's pinned single-account config (D164). Steps 2 to 6 need Meta business verification approved. Step 4 also needs S0.11 and the personal Microsoft account signed in (step 1). Step 5 needs S0.10 (ready). Step 6 needs S0.1 (≥ 30 pairs) and S0.5, and records what is missing if they are not ready.

Flow B uses one personal Microsoft account (D164); the work tenant is out of scope and the work address only receives mail.

## Step 1: Deploy the D164 image `[inline: the owner's private config, secrets and live host]`

From the `harden-later` review (`docs/plan-mvp.md` § "Carried over from the `harden-later` review"). Does not wait for Meta.

1. Build and deploy the current `main` image with the owner's pinned single-account config: one Microsoft account `personal`, its mailbox pinned, Graph send (`modes: [graph]`), no `bot` account and no `inbox_rule`.
2. Reload the service and check `/health`.
3. Run `auth-microsoft --account personal` and confirm the pin accepts the signed-in mailbox.
4. Remove the unused `bot.msal.age` from `/data/tokens`.

Test: `/health` is ok after the reload, `auth-microsoft --account personal` succeeds, and `/data/tokens` has no `bot.msal.age`.

## Step 2: Meta goes live `[inline: Meta business settings and secrets]`

Waits for Meta business verification. Until then, stop here and push-notify the owner that step 1 is done and the plan waits for Meta.

The steps follow the `live-trial-meta-setup` memory. Do not create a second app or portfolio.

1. A permanent System User token with `whatsapp_business_messaging` and `whatsapp_business_management`, stored in the age store as `whatsapp_token` (replaces the 24 h one). Reset the app secret (it was pasted in chat on 2026-11-16) and store the new one.
2. Register the bot number with the Cloud API (it leaves the WhatsApp Business app), and put its `phone_number_id` in the private config.
3. Submit the utility templates (S0.7) and wait for approval.
4. Webhook callback `https://<machine>.<tailnet>.ts.net/webhook/whatsapp` with the verify token, subscribed to `messages`. Redeploy.
5. The owner sends "Hola" and gets the bot's answer.

Test: one inbound and one outbound message in the live log; no 131031.

## Step 3: Flow A live (M6) `[inline: the owner's real invoices and Google account]`

1. **M6:** a personal invoice photo leads to files in `{root}/{YYYY}/{MM}/`, one Sheet row, and BR-D16 with working links. Send it again after `/reprocesar`: the row is updated, not duplicated.
2. **PDF only (D142):** an invoice from a store that gives only the PDF completes.
3. **DNI boleta (D143):** a real boleta to the owner's DNI routes to `personal_dni` and writes one row with BR-D18. Record how many boleta photos carry a QR with the DNI.
4. **Gmail:** forward a store's invoice mail to the dedicated Gmail; `email_inbox` finds its XML.
5. **WhatsApp PDF (read-more):** a store PDF sent as a document is read from its text layer.
6. **SOL:** deferred with S0.4 (D153); skip it.
7. **BR-D01:** remove the app's access in the Google account, send a photo, and confirm `PENDING_AUTH` plus the admin alert. Sign in again and confirm the invoice finishes.
8. Record M6 in `specs/04-tasks.md`.

Test: the checks above, by hand.

## Step 4: Flow B live (M3, 6d) `[inline: the owner's reimbursement invoices, personal OneDrive and mailbox]`

Follow `docs/archive/plan-mvp-file-b.md` § "Deferred: live run (M3)" and then `docs/archive/plan-mvp-widen-b-deliver.md` § "Deferred: live check": Graph send from the personal account (`inbox_rule` is unused, D164), the laptop agent, `/pagado` and the go-live import dry run. The import's real run waits for the owner's go. Set `mail.send_enabled` back to `false` after each run, and record M3 and the 6d part of M4 in `specs/04-tasks.md`.

Test: the checks in those two sections, by hand.

## Step 5: Gaps and self-alerts live (6e) `[inline: GitHub App on the ops repo and the owner's chat]`

Follow `docs/archive/plan-mvp-widen-b-gaps-ops.md` § "Deferred: live check" (GitHub and self-alerts; the backup part was step 9). Also check this plan's fixes:

- `/gap ignorar` on a gap whose issue is queued: the gap stays ignored and gives no `GAP_admin`.
- `/portal` for a gap with no issue yet: no comment row waits.

Record M4 in `specs/04-tasks.md`.

Test: the checks above, by hand.

## Step 6: Read checks and the M7 record (M5, M7) `[inline: the owner's private samples and SUNAT credentials]`

1. **S0.5:** with the SUNAT API credentials in the age store and `validez_api.enabled: true`, send a work invoice. Confirm the token URL and request fields against the real API (fix the config defaults if they differ) and that `sunat_status_json` holds `estado_cp` 1.
2. **S0.1:** with ≥ 30 private pairs, run `scripts/accuracy_report.py --set <private dir> --write` and record the numbers. Check `_fix_serie` against any serie with D, O, Q, I or L in its last three characters.
3. If S0.1 or S0.5 is not ready, record what is missing. v1 cannot go live without S0.1 (D56).
4. Record M5 and the 9d part of M7 in `specs/04-tasks.md`.

Test: the records exist.
