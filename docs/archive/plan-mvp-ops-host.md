# Plan: MVP phase 9c · ops-host

Roadmap: `docs/plan-mvp.md` row 9c (D136, D141). It prepares this local Ubuntu machine as the MVP host. The helpers that read untrusted photos and pages get locked down, the database gets a real password, and the bot gets its first live deploy, reachable by Meta over a free HTTPS tunnel.

**Goal:**

- In the image, the extract helper runs with no network, an empty `/proc` and the runtime config hidden.
- The browser helpers run Chromium with its sandbox on, in a pid namespace, with a size-capped temp folder and the runtime config hidden.
- Killing a helper leaves no process behind, and the kernel kills a helper before the bot when memory runs out.
- The live stack is its own Compose project, apart from the dev database the tests use. Its Postgres password is random and kept outside the repo, and Postgres publishes no port.
- `deploy/deploy-live.sh` deploys it, it survives a reboot, and Tailscale Funnel exposes only `/webhook/whatsapp` and `/health`.

**Needs from the owner first:** nothing for steps 1 to 12. For step 13 (first deploy), Tailscale installed and signed in on this machine, with Funnel allowed for it. Owner, 2026-11-17, on the tunnel: "choose the best free options, since we are in development mode and we want to have results for the MVP then we can review other payment options".

## Spike (2026-11-17, this host, rootless Podman 5.7, image `localhost/invoicebot-app:dev`)

- The host has `kernel.apparmor_restrict_unprivileged_userns = 1`, so `unshare -r` fails natively. Inside the container, as the `bot` user, nested user namespaces work: `unshare -rmnpf --kill-child` runs.
- A new `/proc` cannot be mounted (`mount /proc failed`), but a 4 KiB tmpfs over `/proc` and over `/app/runtime-config` works. Python with `cv2` and `zxingcpp` and `tesseract --list-langs` still run under it, and `socket.create_connection` gets `ENETUNREACH`.
- `/sys/fs/cgroup` is read-only in the container, so the bot cannot create a cgroup per helper. With `--systemd=always` it is writable, but owned by container root.
- `chromium.launch(chromium_sandbox=True)` works in the container. It fails under `unshare -r` (Chromium refuses its sandbox as uid 0). It works when an inner `unshare --user --map-user=1000 --map-group=1000` runs inside a `unshare -rmpf` that first mounted a 128 MiB tmpfs over `$TMPDIR` and hid `/app/runtime-config`.
- In that pid namespace, `kill -9` of the wrapper took the Chromium processes from 6 to 0. Without it, a detached Chromium outlives its helper's process group.
- Writing `1000` to `/proc/self/oom_score_adj` works as `bot`.
- The host is `x86_64` with 15 GiB of RAM. Linger is already on for the owner's user, and `podman-restart.service` is disabled. The compose provider is Docker Compose v5.7.1, so override files and `!reset` work.

## Design choices

Step 1 writes these into spec 05. They are proposals for the owner's review of this plan.

- **D146 · Tailscale Funnel for the local host.**
  - The MVP host on this machine is reached through Tailscale Funnel: free, with a fixed `https://<machine>.<tailnet>.ts.net` URL and no domain. TLS ends on this machine.
  - Funnel serves two paths only: `/webhook/whatsapp` and `/health`, both proxied to `http://127.0.0.1:8080`.
  - `tailscaled` runs on the host, not in Compose, and keeps the serve config across reboots.
  - The Cloudflare tunnel of spec 03 waits for the VPS (D42). The Cloudflare quick tunnel stays for one-off trials (D31).
- **D147 · Helpers run in namespaces, not as separate users or cgroups.**
  - The `ops-host` leftovers asked for separate users, cgroup memory caps and a capped disk. Rootless Podman gives the bot no root to switch users and no writable cgroup. So the image (`HELPER_SANDBOX=1`) wraps each helper in `unshare`:
    - **Extract helper:** a user, mount, network and pid namespace. A 4 KiB tmpfs hides `/proc`, and one hides each runtime-config dir. The network namespace has no interface, so the helper has no network.
    - **Browser helpers** (portal and SOL): a user, mount and pid namespace. Their network stays, because they go out through the guard proxy. A tmpfs capped at `HELPER_TMP_MB` (default 256) covers `$TMPDIR`, the runtime-config dirs are hidden, and an inner user namespace maps the helper to uid 1000 so Chromium runs with `chromium_sandbox=True`.
    - Both wrappers set `oom_score_adj` to 1000. Under the container's `mem_limit` (900m, R-9.1), the kernel then kills the helper, not the bot.
    - `--kill-child` and the pid namespace kill every process the helper started, the detached Chromium included.
  - Natively (dev, CI pytest) the sandbox is off: the host does not allow user namespaces.
  - With `HELPER_SANDBOX=1`, startup runs both wrappers once with `true`. If either fails, the app refuses to start, so a deploy on an engine without nested user namespaces (Docker's default seccomp) rolls back.
- **D148 · The live stack is a separate Compose project with a real database password.**
  - `deploy/compose.live.yml` overrides `deploy/docker-compose.yml`. Project `invoicebot-live`, its own volumes, Postgres without a published port.
  - The password is random (32 bytes, hex). `deploy.sh` makes it once into `DB_SECRETS_DIR` (dir `0700`), outside the repo:
    - `postgres-password` (mode `0600`): Postgres reads it through `POSTGRES_PASSWORD_FILE` as container root before it drops to `postgres`.
    - `pgpass` (`*:*:*:invoicebot:<password>`, mode `0600`, owned by the container's `bot` uid through `podman unshare chown 10001:10001`): the app, Alembic and `pg_dump` read it through `passfile=` in `DATABASE_URL`.
  - The password is never in the environment, on a command line or in the repo. The dev stack keeps its dev password and port 5432 for the tests.
- **D149 · Deferred to the VPS (D42):**
  - the network namespace or nftables rule that lets the browser helper reach only its guard proxy. The guard proxy runs inside the helper, so Chromium cannot be cut off from the network alone. The Chromium sandbox (D147) and the WebRTC flag cover this host.
  - the amd64 digest pin of the Dockerfile base. The host is `x86_64`, so the pinned digest fits.

Conventions:

- Lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.
- Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`).
- Container tests (`tests/deploy/*.sh`) need Podman and build the image, which takes a few minutes.
- The mk-exec worker must write only inside this repo. Stray files have shown up in a sibling `../invoicebot/` before.

Out of scope here: the live runs and the Meta callback (9d `ops-live`), the `ops-live` leftovers (`restore_drill.sh` query keys included, since the drill never targets the live database), config reload (9e).

## Step 1: Record D146–D149 `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Spec 05: rows D146 to D149 with the text from "Design choices", each marked `(9c plan, proposed)`.
- Spec 03:
  - the Deploy row and the diagram line: "Cloudflare Tunnel on the VPS; Tailscale Funnel on the local MVP host (D146)";
  - the Secrets row: the database password lives in a file outside the repo (D148);
  - the helper paragraph: the namespace sandbox (D147).
- Roadmap:
  - row 9c's "Needs from the owner first": "Tailscale signed in, Funnel allowed (first deploy only)";
  - mark the `ops-host` leftovers as moved to this plan, or deferred by D149.

Test: `uv run pytest -q tests/unit/test_spec_parity.py` (if present), and `npx prettier --check docs/plan-mvp.md`.

## Step 2: Helper sandbox prefixes `[worker]`

Files: `src/invoicebot/sandbox.py` (new), `tests/unit/test_sandbox.py` (new).

Module docstring: "Build the `unshare` prefixes that isolate the helpers in the image (D147)."

- `IMAGE_SCRIPT` (a `str` constant, a POSIX `sh` script):

  ```sh
  set -e
  echo 1000 > /proc/self/oom_score_adj
  mount -t tmpfs -o size=4k,mode=0555 none /proc
  while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
  shift
  exec "$@"
  ```

- `BROWSER_SCRIPT` (a `str` constant):

  ```sh
  set -e
  echo 1000 > /proc/self/oom_score_adj
  mount -t tmpfs -o "size=$1m,mode=0700" none "$TMPDIR"
  shift
  while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
  shift
  exec unshare --user --map-user=1000 --map-group=1000 "$@"
  ```

- `image_prefix(hidden_dirs: Sequence[Path]) -> tuple[str, ...]` returns `("unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--kill-child", "sh", "-c", IMAGE_SCRIPT, "sandbox", *map(str, hidden_dirs), "--")`.
- `browser_prefix(hidden_dirs: Sequence[Path], tmp_mb: int) -> tuple[str, ...]` returns the same without `--net`, with `BROWSER_SCRIPT`, and `str(tmp_mb)` after `"sandbox"`, before the dirs. `tmp_mb < 1` raises `ValueError`.
- `hidden_dirs(paths: Iterable[Path | None]) -> tuple[Path, ...]`: the sorted, de-duplicated parents of the non-`None` paths, never `/`.

Test cases (`uv run pytest -q tests/unit/test_sandbox.py`), each docstring citing D147:

1. `image_prefix([Path("/a"), Path("/b")])` ends with `"sandbox", "/a", "/b", "--"` and contains `"--net"`.
2. `browser_prefix([Path("/a")], 256)` has no `"--net"` and ends with `"sandbox", "256", "/a", "--"`.
3. `browser_prefix([], 0)` raises `ValueError`.
4. `hidden_dirs([Path("/x/c.yaml"), None, Path("/x/s.age"), Path("/y/p")])` is `(Path("/x"), Path("/y"))`.
5. `hidden_dirs([Path("/top")])` is `()`, because the parent is `/`.
6. Both scripts start with `set -e` and write `oom_score_adj` before any `mount`.

## Step 3: Sandbox settings and the image switch `[worker]`

Files: `src/invoicebot/settings.py`, `deploy/Dockerfile`, `tests/unit/test_settings.py` (create if missing).

- `Settings` gains `helper_sandbox: bool = False` ("wrap helpers in namespaces, D147; only works in the image") and `helper_tmp_mb: int = 256` ("tmpfs cap of a browser helper's temp folder, D147"). Neither name ends in a secret suffix.
- Dockerfile: add `HELPER_SANDBOX=1` to the final stage's `ENV` block, next to `PYTHONUNBUFFERED=1`.

Test cases (`uv run pytest -q tests/unit/test_settings.py`):

1. D147: with no env vars, `helper_sandbox` is `False` and `helper_tmp_mb` is `256`.
2. D147: with `HELPER_SANDBOX=1` and `HELPER_TMP_MB=64` set through `monkeypatch.setenv`, the values are `True` and `64`.

## Step 4: The extract handler takes a helper prefix `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_extract.py`.

- `make_extract_handler` gains a sixth parameter `helper_prefix: Sequence[str]` (document it: "prepended to the helper's argv; empty outside the image, D147"). It is passed to `_run_helper`, which runs `[*helper_prefix, *helper_argv(...)]`. `helper_argv` itself does not change: the golden-set test and the accuracy report keep the bare argv.
- `app.py` passes `()` for now; step 7 wires the real prefix.

Test case (`uv run pytest -q tests/integration/test_pipeline_extract.py`):

- D147: write an executable fake prefix script to `tmp_path`. It appends its argv to a marker file, then runs `exec "$@"`.
- Build the handler with `helper_prefix=(str(script),)` and run it on a synthetic photo that an existing test in this file already extracts.
- The invoice reaches the same state as in that test, and the marker file holds the helper argv, `-m invoicebot.extract` included.
- Do not monkeypatch `_run_helper` in this test.

## Step 5: Browser helpers take `--chromium-sandbox` `[worker]`

Files: `src/invoicebot/fetchers/browser/protocol.py`, `src/invoicebot/fetchers/browser/session.py`, `src/invoicebot/fetchers/browser/__main__.py`, `src/invoicebot/fetchers/browser/sol.py`, `tests/unit/test_browser_session.py` (or the file that tests `launch_options` today).

- `protocol.py`:
  - `CHROMIUM_SANDBOX_FLAG = "--chromium-sandbox"`;
  - `parse_helper_args(argv: Sequence[str]) -> bool` returns `True` for `[CHROMIUM_SANDBOX_FLAG]`, `False` for `[]`, and raises `BadRequest` for anything else.
- `session.py`:
  - `launch_options(portal, proxy_port, chromium_sandbox: bool)` adds `"chromium_sandbox": chromium_sandbox`;
  - `browse` and every other caller of `launch_options` take a keyword-only `chromium_sandbox: bool` and pass it on.
- Both `main()` functions call `parse_helper_args(sys.argv[1:])` before they read the request. On `BadRequest` they emit `HelperOutput(reason=FetchReason.TECHNICAL)` and return 0, as they do for a bad request. The flag then flows down to `launch_options`.

Test cases (`uv run pytest -q tests/unit/test_browser_session.py`):

1. D147: `launch_options(..., chromium_sandbox=True)["chromium_sandbox"] is True`, and `False` for `False`.
2. D147: `parse_helper_args([])` is `False` and `parse_helper_args(["--chromium-sandbox"])` is `True`.
3. D147: `parse_helper_args(["--x"])` and `parse_helper_args(["--chromium-sandbox", "x"])` raise `BadRequest`.

Existing tests that read the launched Chromium's arguments must still pass. Pass `chromium_sandbox=False` there, since the host forbids it.

## Step 6: Wire the sandbox at startup `[worker]`

Files: `src/invoicebot/sandbox.py`, `src/invoicebot/app.py`, `tests/unit/test_sandbox.py`.

- `sandbox.py`:
  - `HelperPrefixes`, a frozen dataclass with `image: tuple[str, ...]` and `browser: tuple[str, ...]`, both empty when off.
  - `helper_prefixes(enabled: bool, hidden: Sequence[Path], tmp_mb: int) -> HelperPrefixes`.
  - `browser_command(prefixes: HelperPrefixes, base: Sequence[str]) -> tuple[str, ...]` returns `(*prefixes.browser, *base, CHROMIUM_SANDBOX_FLAG)` when `prefixes.browser` is set, else `tuple(base)`.
  - `async def check_sandbox(prefixes: HelperPrefixes, timeout_s: float) -> None`. It runs `[*prefixes.image, "true"]` and `[*prefixes.browser, "true"]` through `invoicebot.helper.run_helper`. The browser run gets an env of `PATH` and a `TMPDIR` from `tempfile.TemporaryDirectory`. Any `RuntimeError` or `TimeoutError` is re-raised as `SandboxUnavailable` (a new exception class) with `from`. It does nothing when the prefixes are empty.
- `app.py` lifespan:
  - build the prefixes from `settings.helper_sandbox`, `hidden_dirs([settings.config_path, settings.secrets_path, settings.age_identity_path, <the passfile path from settings.database_url, if any>])` and `settings.helper_tmp_mb`;
  - `await check_sandbox(prefixes, 10)` before the workers start;
  - pass `prefixes.image` to `make_extract_handler`;
  - pass `browser_command(prefixes, DEFAULT_HELPER_COMMAND)` to `PortalFetcher` and `browser_command(prefixes, SOL_HELPER_COMMAND)` to `SunatSolFetcher`, through their existing `helper_command` parameter. `_sol_fetcher` and the portal fetcher builder take the prefixes as one more argument.
  - Reading `passfile` from the URL: `urllib.parse.parse_qs(urlsplit(url).query).get("passfile")`. Keep it in one small function in `sandbox.py`, `passfile_path(database_url: str | None) -> Path | None`.
- `cli.py` `portal-test` keeps the bare command (it runs on the host).

Test cases (`uv run pytest -q tests/unit/test_sandbox.py`):

1. D147: `helper_prefixes(False, [...], 256)` has empty `image` and `browser`, and `browser_command` returns the base unchanged.
2. D147: `helper_prefixes(True, [Path("/c")], 256)`: `image` contains `--net`, `browser` does not, and `browser_command(..., ("py", "-m", "x"))` ends with `"py", "-m", "x", "--chromium-sandbox"`.
3. D147: `check_sandbox` with empty prefixes returns without running anything.
4. D147: `check_sandbox` with `HelperPrefixes(image=("false",), browser=())` raises `SandboxUnavailable`. A non-empty image prefix whose command fails stands in for a host that forbids namespaces.
5. D148: `passfile_path("postgresql://u@h/db?passfile=/run/x/pgpass") == Path("/run/x/pgpass")`; `passfile_path(None)` and a URL with no `passfile` give `None`.

## Step 7: Container test of the sandbox `[worker]`

Files: `tests/deploy/test_helper_sandbox.sh` (new), `tests/deploy/sandbox_checks.py` (new).

- `test_helper_sandbox.sh` (bash, `set -euo pipefail`):
  - builds `localhost/invoicebot-app:sandbox-test` from `deploy/Dockerfile` with `podman build`;
  - runs `podman run --rm -v "$PWD/tests/deploy:/checks:ro" -v "$tmpcfg:/app/runtime-config:ro" localhost/invoicebot-app:sandbox-test python -P /checks/sandbox_checks.py`, where `$tmpcfg` is a `mktemp -d` holding one file `marker`;
  - exits with the checks' exit code and removes `$tmpcfg` on exit.
- `sandbox_checks.py` builds the real prefixes with `invoicebot.sandbox.helper_prefixes(True, [Path("/app/runtime-config/marker")], 64)`, runs these checks with `subprocess.run` (timeouts set), prints `ok <name>` or `FAIL <name>: <why>`, and exits 1 on any failure:
  1. `image-no-network`: image prefix + `python -P -c` connecting to `1.1.1.1:80` exits non-zero with `Network is unreachable` in stderr.
  2. `image-no-proc`: image prefix + `ls -A /proc` prints nothing.
  3. `image-config-hidden`: image prefix + `ls -A /app/runtime-config` prints nothing.
  4. `image-oom`: image prefix + `sleep 5` started with `Popen`. After 1 s, the process whose `/proc/<pid>/comm` is `sleep` has `oom_score_adj` 1000. Then kill it.
  5. `browser-config-hidden` and `browser-oom`: browser prefix + `sh -c 'ls -A /app/runtime-config; cat /proc/self/oom_score_adj'` prints only `1000`. Env: `PATH`, plus `TMPDIR` set to a fresh `mktemp` dir.
  6. `browser-tmp-cap`: browser prefix + `sh -c 'head -c 80M /dev/zero > "$TMPDIR/big"'` fails (cap 64 MiB).
  7. `chromium-sandbox`: browser prefix + `python -P -c` that launches Playwright Chromium with `chromium_sandbox=True`, sets content `<p>x</p>` and prints the text. It prints `x`.
  8. `no-orphan-chromium`: browser prefix + a Python snippet that launches Chromium and sleeps 60 s, started with `Popen(start_new_session=True)`. Wait until a `/proc/*/comm` contains `chrome`, then `os.killpg(pid, SIGKILL)`. After 2 s, no `/proc/*/comm` contains `chrome`.
  9. `extract-helper`: image prefix + `invoicebot.pipeline.extract.helper_argv(...)` for the first image the golden-set test reads, with the config `tests/unit/test_golden_set.py` uses. It exits 0 and its stdout parses as JSON. Mount whatever that needs read-only, under `/checks`.
- Keep the script and the checks free of real data: synthetic fixtures only.

Test: `bash tests/deploy/test_helper_sandbox.sh` prints nine `ok` lines and exits 0.

## Step 8: Run the sandbox test in CI `[worker:low]`

Files: `.github/workflows/ci.yml`.

- A new job `helper-sandbox` on `ubuntu-24.04`:
  - `actions/checkout` pinned the way the other jobs pin it;
  - one step `bash tests/deploy/test_helper_sandbox.sh`, which uses the runner's preinstalled Podman;
  - `timeout-minutes: 20`;
  - no secrets and no extra permissions.

Test: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` passes, and `actionlint` passes if installed. CI runs it on push.

## Step 9: Backups read the passfile and check the URL at startup `[worker]`

Files: `src/invoicebot/ops/backup.py`, `src/invoicebot/app.py`, `tests/unit/test_backup.py`.

- `PG_ENVIRONMENT_BY_QUERY_KEY` gains `"passfile": "PGPASSFILE"` (D148).
- The `ops-live` leftover: an unknown `DATABASE_URL` query key made the backup fail every night. Make `_pg_environment` public as `pg_environment` and call it once where `app.py` wires the backup job (near the current `database_url = settings.database_url`). A `ValueError` there stops startup with the message naming the key.

Test cases (`uv run pytest -q tests/unit/test_backup.py`):

1. D148: `pg_environment("postgresql://u@h:5432/db?passfile=/run/x/pgpass")` sets `PGPASSFILE=/run/x/pgpass` and no `PGPASSWORD`.
2. R-9.11: `pg_environment("postgresql://u@h/db?foo=1")` raises `ValueError` naming `foo`. Keep the existing case if one exists.
3. Existing tests renamed to the public name still pass.

Then remove that item from the roadmap's "Carried over from the `ops-harden` review" list (it moves here).

## Step 10: Live Compose override and database secrets in `deploy.sh` `[worker:high: reasoning: shell script with engine-specific ownership and an existing rollback flow that must keep passing]`

Files: `deploy/compose.live.yml` (new), `deploy/deploy.sh`, `tests/deploy/test_deploy.sh`.

- `compose.live.yml` (override, D148):

  ```yaml
  name: invoicebot-live
  services:
    app:
      environment:
        DATABASE_URL: postgresql://invoicebot@postgres:5432/invoicebot?passfile=/run/invoicebot/pgpass
      volumes:
        - ${DB_SECRETS_DIR:?}/pgpass:/run/invoicebot/pgpass:ro
    postgres:
      environment:
        POSTGRES_PASSWORD: !reset null
        POSTGRES_PASSWORD_FILE: /run/invoicebot/postgres-password
      volumes:
        - ${DB_SECRETS_DIR:?}/postgres-password:/run/invoicebot/postgres-password:ro
      ports: !reset []
  ```

  Check that `podman compose -f deploy/docker-compose.yml -f deploy/compose.live.yml config` resolves it. Adjust the syntax only if the resolver refuses it, and say so.

- `deploy.sh`:
  - `COMPOSE_PROJECT` defaults to `invoicebot` and may be set from the env.
  - `COMPOSE_OVERRIDE` is optional; when set, `compose()` adds `-f "$COMPOSE_OVERRIDE"`.
  - `compose()` always passes `-p "$COMPOSE_PROJECT"`.
  - `ensure_db_secrets` runs only when `COMPOSE_OVERRIDE` is set, before `ensure_postgres`:
    - it requires `DB_SECRETS_DIR` and creates it with mode `0700`;
    - if `postgres-password` is missing, it writes 64 hex chars from `od -An -tx1 -N32 /dev/urandom | tr -d ' \n'` with `umask 077`, and the matching `pgpass` line `*:*:*:invoicebot:<password>`;
    - with `podman`, it runs `podman unshare chown 10001:10001 "$DB_SECRETS_DIR/pgpass"`; with `docker`, it runs `chown 10001:10001` and on failure exits 1 with "run as root once: chown 10001:10001 <path>";
    - existing files are never rewritten;
    - the password never goes to argv or the log.
  - The usage text and header comment name the new variables.
- `tests/deploy/test_deploy.sh` keeps passing with no override (dev project).
- Add one case: with `COMPOSE_PROJECT=invoicebot-deploytest`, `COMPOSE_OVERRIDE=deploy/compose.live.yml` and a `mktemp -d` `DB_SECRETS_DIR`, a deploy of a test tag:
  - becomes healthy (the app reaches Postgres through the passfile: `/health` runs `SELECT 1`);
  - `postgres-password` is mode 600;
  - the postgres container publishes no port (`podman port` prints nothing).
  - Clean up that project's containers and volumes (`compose down -v`) at the end.

Test: `bash tests/deploy/test_deploy.sh`.

## Step 11: `deploy-live.sh` `[worker:low]`

Files: `deploy/deploy-live.sh` (new, executable), `tests/deploy/test_deploy_live_args.sh` (new).

- The script sets defaults under `LIVE_ROOT="${LIVE_ROOT:-$HOME/.local/share/invoicebot}"`:
  - `CONFIG_DIR=$LIVE_ROOT/config`, `BACKUP_DIR=$LIVE_ROOT/backups`, `STATE_DIR=$LIVE_ROOT/deploy`, `DB_SECRETS_DIR=$LIVE_ROOT/db`;
  - `COMPOSE_PROJECT=invoicebot-live`, `COMPOSE_OVERRIDE=<repo>/deploy/compose.live.yml`.
- Each variable keeps a value already set in the env.
- It creates the four dirs (`mkdir -p`, `chmod 700` on `$LIVE_ROOT`) and execs `deploy.sh "$@"`.
- With `--print-env` as the only argument, it prints the resolved `NAME=value` lines and exits 0 without deploying (for the test).

Test: `bash tests/deploy/test_deploy_live_args.sh`. With `LIVE_ROOT=$(mktemp -d)`, `--print-env` prints the six variables under it. With `CONFIG_DIR=/x` set, it keeps `/x`.

## Step 12: Local host guide `[worker]`

Files: `deploy/README.md`.

Add a section "Local MVP host (D141, D146–D148)" after "VPS run", with these parts in this order. Use plain commands and keep the voice of the rest of the file.

1. **Layout:** the `LIVE_ROOT` dirs from step 11 and what each holds. Live runtime config goes in `$LIVE_ROOT/config`, not in the repo's `config/`. The age identity file must be readable by the container's `bot` user: `podman unshare chown 10001:10001 <file>`.
2. **Deploy:** `podman build -f deploy/Dockerfile -t localhost/invoicebot-app:$(git rev-parse --short=12 HEAD) .`, then `./deploy/deploy-live.sh <tag>` and `./deploy/deploy-live.sh --config <candidate>`.
3. **Start on boot:** `loginctl enable-linger "$USER"` and `systemctl --user enable --now podman-restart.service`. Containers with `restart: unless-stopped` come back after a reboot.
4. **Helper sandbox (D147):** on by default in the image. What it isolates (one line per helper). The app refuses to start if namespaces are unavailable. Docker's default seccomp profile blocks them, so the image needs Podman, or Docker with a profile that allows `unshare`. The check is `bash tests/deploy/test_helper_sandbox.sh`.
5. **Tailscale Funnel (D146):**
   - install from tailscale.com, then `sudo tailscale up`;
   - `sudo tailscale funnel --bg --set-path /webhook/whatsapp http://127.0.0.1:8080/webhook/whatsapp` and the same for `/health`;
   - `tailscale funnel status`;
   - the public URL is `https://<machine>.<tailnet>.ts.net`;
   - `tailscale funnel reset` turns it off;
   - the first `funnel` command asks to allow Funnel in the tailnet policy.
6. **Uptime check (R-9.14):** point the external monitor at `https://<machine>.<tailnet>.ts.net/health`.
7. **Dev and live side by side:** the dev stack (project `invoicebot`, port 5432, dev password) stays for the tests. Never point `TEST_DATABASE_URL` at the live project.

Also change "Local trials through a Cloudflare quick tunnel (D31)" to say it is for one-off trials, and that the local host uses Funnel (D146).

Test: `npx prettier --check deploy/README.md` if the repo formats Markdown, otherwise none. The reviewer checks every command against steps 10 and 11.

## Step 13: First deploy on this machine `[inline: uses the owner's private config and changes this host]`

Main session only. The worker never sees the real config or the secrets.

1. Check `tailscale status`. If Tailscale is not installed or signed in, push-notify the owner with the two commands from step 12 part 5 and stop here. Steps 2–6 can still run, and the Funnel part waits.
2. Make `$LIVE_ROOT/config` and copy in the owner's `config/config.yaml`, `secrets.age` and age identity:
   - modes `0644` for the yaml and the age file, `0600` for the identity, then `podman unshare chown 10001:10001` on the identity;
   - run `scripts/check_config.py` on the copied config.
3. Build the image at `HEAD` and run `./deploy/deploy-live.sh <tag>`. It must exit 0, and `curl -s localhost:8080/health` must give 200.
4. `podman exec` into the live app: run `python -P -c "import invoicebot.sandbox"` and read the app log for the passed sandbox check. Confirm Postgres has no published port.
5. `systemctl --user enable --now podman-restart.service`. Then `systemctl --user restart podman-restart.service`, and the app is healthy again within 60 s.
6. Funnel for both paths:
   - `curl -s https://<machine>.<tailnet>.ts.net/health` gives 200 from outside the tailnet (check with a phone off Wi-Fi, or record that only the owner can check that);
   - any other path (e.g. `/`) gives 404 from Funnel.
7. Record the results (no secrets, no hostnames beyond `<machine>.<tailnet>.ts.net`) in `specs/spikes/host.md` (new). This records the 9c part of M7.

Meta's callback URL is set in 9d `ops-live`, once the WABA leaves restriction.
