# Windows through WSL2 (9f `windows`)

Trial of the live stack on a Windows machine through WSL2 and rootless Podman (D141, D163). The procedure is in
`deploy/README.md` § "Windows through WSL2".

- Date: 2026-11-20
- Machine: the owner's work laptop, 20 logical cores, 63.7 GB RAM; Windows 11 24H2 (build 26100), no admin rights used
- WSL 2.7.3.0, kernel 6.6.114.1; a fresh `Ubuntu-24.04` distro with systemd, next to other distros
- Podman 4.9.3 (Ubuntu apt), `podman-compose` 1.6.0 (pipx)
- Image: `localhost/invoicebot-app:da79f1d1b79b`, deployed with `./deploy/deploy-live.sh da79f1d1b79b` and no live
  config, so the app served only `/health` and no secrets left the owner's Ubuntu host

## Results

| #   | Check                                   | Result                                                                                                     |
| --- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 1   | `tests/deploy/test_helper_sandbox.sh`   | pass, 16 `ok`, after the OpenBLAS fix below (before it: `extract-helper` failed with `MemoryError`)        |
| 2   | `deploy-live.sh <tag>`, `/health`       | pass: `{"status":"ok","db":"ok"}` from the distro and from Windows PowerShell (`localhost` is forwarded)   |
| 3   | `tests/deploy/test_deploy.sh`           | pass, after `podman-compose` 1.6.0 replaced the apt 1.0.6 (see below)                                      |
| 4   | Funnel `/health` from outside           | not run: a company laptop, where Tailscale and a public URL may break IT policy; passed on the Ubuntu host |
| 5   | Back after a Windows restart and log-on | pass: the Startup batch file ran about 6 min after boot, WSL started 30 s later, `/health` ok              |
| 6   | WSL VM memory with the stack idle       | `vmmemWSL` 1819 MB                                                                                         |

Once started, the distro stayed `Running` with the stack healthy for at least 15 minutes, held up by the keep-alive
process.

## Found during the trial

- **OpenBLAS and the helper's address-space cap.** numpy's OpenBLAS reserves about 160 MB of address space per core
  when it loads. On 20 cores the extract helper reached 3.74 GB before reading a byte, past its 3 GiB `RLIMIT_AS`, so
  every photo failed with `MemoryError`. Any host with many cores would hit it. Fixed in `da79f1d`: the helper runs
  with `OPENBLAS_NUM_THREADS=1` (0.31 GB after imports), from `extract_helper_env`, with a regression test.
- **`podman-compose` 1.0.6 cannot read `compose.live.yml`.** Ubuntu 24.04's apt package does not know the `!reset`
  tag the live override uses to drop the dev password and the published Postgres port. `pipx install
podman-compose==1.6.0`, the version on the Ubuntu host, works.
- **A log-on scheduled task needs admin.** `schtasks /sc onlogon` was refused without it. A batch file in the user's
  Startup folder works without admin. It waits 30 s, then runs `wsl.exe -d Ubuntu-24.04 --exec sleep infinity`, because
  WSL stops the distro once no Windows process is attached to it; `/bin/true` would exit at once. Windows Terminal
  ignores the shortcut's "minimized" setting, so a blank window stays open; closing it stops the bot.
- **Startup programs run late.** On a busy log-on, Windows ran the Startup folder about 6 minutes after boot.
