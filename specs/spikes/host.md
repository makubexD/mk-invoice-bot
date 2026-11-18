# Local MVP host (9c `ops-host`)

First deploy of the live stack on the owner's Ubuntu machine (D141, D146–D148). This records the 9c part of M7.
The procedure is in `deploy/README.md` § "Local MVP host".

- Date: 2026-11-17
- Host: Ubuntu 26.04.1 LTS, rootless Podman 5.7.0, Tailscale 1.104.7
- Image: `localhost/invoicebot-app:9bd2f29d981a`, deployed with `./deploy/deploy-live.sh 9bd2f29d981a`
- Public URL: `https://<machine>.<tailnet>.ts.net`

## Results

| Check                                  | Result                                                                                                                                                   |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check_config.py` on the live config   | pass (warnings only: portals with status `todo`)                                                                                                         |
| `deploy-live.sh <tag>`                 | exit 0, `/health` 200 with `db: ok`                                                                                                                      |
| Database secrets (D148)                | `postgres-password` and `pgpass` mode 600, `pgpass` owned by the container's `bot` user; the app connects through the passfile                           |
| Live Postgres ports                    | none published; the app listens on `127.0.0.1:8080` only                                                                                                 |
| Helper sandbox (D147)                  | `HELPER_SANDBOX=1`; startup ran `check_sandbox` (it raises when the namespaces are missing) and the app came up; nested `unshare` works in the container |
| Start on boot                          | linger on, `podman-restart.service` enabled; a restart of the unit brought the app back healthy in about 4 s                                             |
| Funnel `/health`                       | 200                                                                                                                                                      |
| Funnel `/`, `/docs`                    | 404 from Funnel                                                                                                                                          |
| Funnel `/webhook/whatsapp` (plain GET) | 403: no verify token, as expected                                                                                                                        |
| Funnel from outside the tailnet        | to check by the owner (phone off Wi-Fi); public DNS already resolves to Funnel relay addresses                                                           |

## Found during the deploy

- `podman-restart.service` starts only containers with `restart: always`. `unless-stopped` containers stay down after
  a reboot, so `deploy/compose.live.yml` now sets `restart: always` on the live app and Postgres.
- The store directory (`stores/`, `portals/`, `sunat_sol.yaml`) is read from the config folder and is not in the
  image, so the live config folder needs it beside `config.yaml`.
- Startup refused the first two runs for missing secrets, as designed (R-9.9): `google_client_secret` (Google OAuth
  client created, still in Testing) and `github_app_private_key` (GitHub App created, Issues only, installed on the
  private ops repo only, D44, D45).
- On this machine `podman compose` now runs docker-compose, which does not adopt the dev Postgres that podman-compose
  created, so `tests/deploy/test_deploy.sh` needs that container recreated (or stopped) to get port 5432.

## Open for 9d `ops-live`

- Google OAuth app is in Testing: refresh tokens expire after 7 days until it is published with a homepage and a
  privacy policy URL. The client secret was shared in a chat session; rotate it when publishing.
- `invoicebot auth-google` signs in through a loopback redirect on a random port, which the live container cannot
  receive, and the live Postgres has no published port: the sign-in needs its own recipe.
- Meta callback URL is set once the WABA leaves restriction.

## Google sign-in (9d `ops-live`, step 8, 2026-11-18)

- Branding completed (home page and privacy policy on the owner's GitHub Pages site, authorized domain added, no
  logo so no verification review) and the app published to production. The consent screen warns that the app is
  unverified; "Advanced → Go to invoicebot" continues.
- Google shows each scope as its own checkbox and may leave it unticked; an unticked `drive.file` is refused with
  "Google did not grant …". Granted: `owner` = `openid email drive.file`, `invoices_a` = `openid email
gmail.readonly`, both pinned to the same mailbox (D138).
- Sign-in recipe for the local host: run `auth-google` and `google-setup` on the host with `DATA_DIR` set to a staging
  dir (mode `0700`) and `DATABASE_URL` set to the dev database (only for the resume query; nothing was waiting).
  Then `podman cp` the `tokens/*.google.age` files into the live app's `/data/tokens` (owner `bot`, mode `0600`),
  copy the config, and restart the app.
- `google-setup` created the Drive root folder and both sheets (`personal_google`, `personal_dni`); their ids are in
  the private config, and `scripts/check_config.py` passes. The live app restarted healthy.
- Still open: rotate the Google client secret (it was shared in a chat session).
