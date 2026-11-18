# Local MVP host (9c `ops-host`)

First deploy of the live stack on the owner's Ubuntu machine (D141, D146–D148). This records the 9c part of M7.
The procedure is in `deploy/README.md` § "Local MVP host".

- Date: 2026-11-17
- Host: Ubuntu 26.04.1 LTS, rootless Podman 5.7.0, Tailscale 1.104.7
- Image: `localhost/invoicebot-app:9bd2f29d981a`, deployed with `./deploy/deploy-live.sh 9bd2f29d981a`
- Public URL: `https://<machine>.<tailnet>.ts.net`

## Results

| Check | Result |
|---|---|
| `check_config.py` on the live config | pass (warnings only: portals with status `todo`) |
| `deploy-live.sh <tag>` | exit 0, `/health` 200 with `db: ok` |
| Database secrets (D148) | `postgres-password` and `pgpass` mode 600, `pgpass` owned by the container's `bot` user; the app connects through the passfile |
| Live Postgres ports | none published; the app listens on `127.0.0.1:8080` only |
| Helper sandbox (D147) | `HELPER_SANDBOX=1`; startup ran `check_sandbox` (it raises when the namespaces are missing) and the app came up; nested `unshare` works in the container |
| Start on boot | linger on, `podman-restart.service` enabled; a restart of the unit brought the app back healthy in about 4 s |
| Funnel `/health` | 200 |
| Funnel `/`, `/docs` | 404 from Funnel |
| Funnel `/webhook/whatsapp` (plain GET) | 403: no verify token, as expected |
| Funnel from outside the tailnet | to check by the owner (phone off Wi-Fi); public DNS already resolves to Funnel relay addresses |

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
