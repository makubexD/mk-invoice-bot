---
name: py-add-package
description: "Vet and wire a new PyPI package via uv/pip — checks CVEs, maintenance, license, and py.typed before adding (Python)"
when_to_use: "Use any time you need to add a new PyPI dependency — fires for \"add the httpx package\", \"install requests\", or \"add pytest-mock as a dev dependency\". Pass the package name; optionally pin a version and add --dev for test/lint-only packages (dependency-groups). Confirms before adding if vetting flags risk. Not for updating an already-installed package's version — see py-audit-deps for checking existing dependencies."
allowed-tools: Read, Bash, Grep, Glob, Edit, Write
argument-hint: "<package-name> [version] [--dev]"
---

# Add Package

**Package:** $ARGUMENTS

Parse `$ARGUMENTS`:
- First token → `<package-name>`
- If a second token looks like a version (e.g. `2.31.0`, `>=2.0`) → `<version>`; else auto-discover
- If `--dev` is present → treat as a dev-only dependency (added to `[dependency-groups] dev`)

## Step 1 — Discover repo layout

Locate `pyproject.toml`, determine the package manager in use (`uv.lock` present → `uv`;
`poetry.lock` → `poetry`; neither → plain `pip` + `requirements.txt`), and check whether the
package is already declared (`grep -n "<package-name>" pyproject.toml requirements*.txt`) — if
found, report the existing constraint and ask whether to update instead of add.

## Step 2 — Vet the package

**CVE check:**
```bash
pip index versions <package-name> 2>&1   # confirm it exists and see available versions
```
Then, after tentatively adding (see Step 4), run `pip-audit` against the resolved environment.

Check https://pypi.org/project/<package-name>/ for: **maintenance status** (last release date,
open issues — flag if last release > 18 months ago); **license** (MIT/BSD/Apache typically fine,
GPL/AGPL needs review); **stdlib alternative** (does `pathlib`/`json`/`dataclasses`/etc. already
cover this? If yes, recommend it and stop); **type support** (`py.typed` marker present, or a
`types-<package>` stub package available — flag if neither exists and the project runs `mypy`).

**If vetting flags a risk:** present the finding and ask for explicit confirmation before proceeding.

## Step 3 — Determine version

**If `<version>` was provided:** use it exactly.

**If not provided:** fetch the latest stable version (`pip index versions <package-name>`); prefer
the latest stable, avoid pre-release unless explicitly requested.

## Step 4 — Add the package

Add through the project's package manager.


**uv (preferred if `uv.lock` present or no lock file exists yet):**
```bash
uv add "<package-name>==<version>"                    # runtime dependency
uv add --group dev "<package-name>==<version>"        # --dev
```

**poetry:**
```bash
poetry add "<package-name>@<version>"
poetry add --group dev "<package-name>@<version>"     # --dev
```

**plain pip + requirements.txt:** append the pinned line to `requirements.txt` (or
`requirements-dev.txt` for `--dev`), then `pip install -r requirements.txt`.

## Step 5 — Verify the install

### Lock file

```bash
git diff uv.lock  # or poetry.lock — confirm the new package + its transitive deps appear
```

### Quality gate

```bash
ruff check . && mypy . && pytest   # through the project's runner if it has one: uv run, poetry run
```

If the gate fails, **undo the addition** (revert `pyproject.toml` and the lock file) and report the
failure.

## Step 6 — Report

```
## Add Package Report

Package: <package-name> v<version>
Manager: <uv / poetry / pip>
Dev-only: <yes / no>

### Vetting
- CVEs: <none detected / ⚠ flagged — detail>
- License: <MIT / Apache-2.0 / ⚠ flagged — detail>
- Maintenance: <active / ⚠ last release: <date>>
- Types: <py.typed / types-<pkg> available / ⚠ untyped>
- Built-in alternative: <none / ⚠ stdlib can replace — recommendation>

### Gate
✅ lint, type-check, and tests passed  /  ❌ <failure detail>

### Next steps
<Any recommended follow-up.>
```
