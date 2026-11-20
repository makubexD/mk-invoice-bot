# Plan: MVP phase 9f · windows

Roadmap: `docs/plan-mvp.md` row 9f (D141). Run the bot on a Windows machine through WSL2 and document it. No native
Windows port: OCR and the browser helpers use Linux namespaces (D147).

**Needs from the owner first:** a Windows 10 22H2+ or Windows 11 machine with virtualization on, for step 3 only.

**Decision proposed here (D163):** on Windows the same Linux stack runs inside a WSL2 Ubuntu 24.04 distro with
rootless Podman, exactly as on the Ubuntu host. Docker Desktop is not supported: Docker's default seccomp profile
blocks `unshare`, so the helper sandbox (D147) refuses to start. Tailscale runs inside the distro, so the Funnel steps
stay the same. The repo is cloned inside the distro's filesystem (`~/`), never under `/mnt/c`, so Linux git keeps LF
line endings and file modes. The trial in step 3 runs without a live config: the app serves only `/health`, so no
secrets leave the owner's Ubuntu host.

## Step 1: Pin LF line endings `[worker:low]`

Files: `.gitattributes` (new).

A checkout made with Git for Windows (`core.autocrlf=true`) turns `deploy/*.sh` into CRLF files that bash cannot run
(`$'\r': command not found`), and the image would copy them. Add `.gitattributes` with exactly:

```
# Every text file is LF in the work tree on every OS (D163); git detects binaries itself.
* text=auto eol=lf
```

Every tracked text file is already LF (`git ls-files --eol`), so no file changes besides the new one.

Test:

```bash
git add --renormalize . && git status --porcelain        # only "A  .gitattributes"
git check-attr eol -- deploy/deploy.sh scripts/laptop_agent.ps1   # both "eol: lf"
```

## Step 2: Windows section in the deploy guide `[inline: user-facing setup docs]`

Files: `deploy/README.md`, `README.md`, `specs/05-open-questions.md`.

1. `deploy/README.md`: a new section `## Windows through WSL2 (D141, D163)` right before `## What the deploy does`,
   with these parts, each a short paragraph plus commands:
   - **Why WSL2 and Podman:** no native port; Docker Desktop's seccomp blocks the helper sandbox; link to
     "Helper sandbox (D147)".
   - **Install:** `wsl --install -d Ubuntu-24.04` from an admin PowerShell, reboot, create the Linux user. Turn
     on systemd in `/etc/wsl.conf` (`[boot]` `systemd=true`), then `wsl --shutdown` and reopen.
   - **Inside the distro:** `sudo apt install podman podman-compose git curl`, then `uv` as in the main README. Clone
     into `~/`, not `/mnt/c` (D163). From here the "Local MVP host" steps 1 to 6 apply unchanged.
   - **Start on boot:** linger and `podman-restart.service` as in step 3, plus a Windows scheduled task at log-on
     that runs `wsl.exe -d Ubuntu-24.04 --exec /bin/true`, since Windows starts the WSL VM only on demand. Mark
     whether the stack survives with no WSL window open as "checked in 9f step 3" until that step records it.
   - **Memory:** the stack's limits (app 900m, Postgres 300m) fit WSL2's default of half the host RAM; a
     `%UserProfile%\.wslconfig` `memory=` line is needed only on a machine with less than 4 GB.
2. `README.md`: in the Quickstart paragraph that links the deploy guide, one sentence: on Windows, run everything
   inside WSL2 as the guide's Windows section says.
3. `specs/05-open-questions.md`: add row D163 with the decision text above, `(9f plan, proposed)`.

Test: `npx prettier --check deploy/README.md README.md specs/05-open-questions.md`, and every command in the new
section is copied from a section that already works on the Ubuntu host or from Microsoft's WSL docs.

## Step 3: Trial on a Windows machine `[inline: the owner's Windows machine]`

On the owner's Windows machine, follow the new section from a fresh WSL2 distro and record each check in
`specs/spikes/windows.md` (new; same shape as `specs/spikes/host.md`: date, machine, Windows and WSL versions, one
line per check with pass or fail and the fix):

1. `bash tests/deploy/test_helper_sandbox.sh` passes inside the distro.
2. The image builds and `./deploy/deploy-live.sh <tag>` with an empty `$LIVE_ROOT/config` ends healthy;
   `curl -s localhost:8080/health` answers from the distro and from Windows PowerShell.
3. `bash tests/deploy/test_deploy.sh` passes.
4. Tailscale Funnel serves `https://<machine>.<tailnet>.ts.net/health` from a phone off the tailnet.
5. After a Windows reboot and log-on, with no WSL window opened, `/health` answers again (records whether the
   scheduled task is enough).
6. Peak memory of the WSL VM while idle (`vmmem` in Task Manager).

Fix the deploy guide wherever the trial differs from it, replace the "checked in 9f step 3" note with the result,
and mark D163 accepted. Then `tailscale funnel reset` and stop the stack on that machine.

Test: `specs/spikes/windows.md` has a pass or fail line for each of the six checks, and
`npx prettier --check deploy/README.md specs/spikes/windows.md specs/05-open-questions.md` passes.
