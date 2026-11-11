---
paths:
  - "**/pyproject.toml"
  - "**/requirements*.txt"
---

# Dependencies (Python)

## Prefer the Standard Library Before Adding a Package

Before adding a dependency, check whether the stdlib already covers the need: `pathlib`, `json`,
`dataclasses`, `functools`, `itertools`, `datetime` (with `zoneinfo` for timezones, 3.9+),
`urllib.request` for simple HTTP, `unittest.mock`, `argparse`. A dependency adds supply-chain risk
and install-time cost the stdlib does not.

## Pin Versions and Commit the Lock File

Declare dependencies in `pyproject.toml` under `[project.dependencies]` / `[dependency-groups]`.
Use `uv` or `poetry` to generate and commit a lock file (`uv.lock` / `poetry.lock`) — it pins the
full transitive graph, not just the direct dependencies a loose `requirements.txt` records.

```bash
uv add httpx              # adds + resolves + updates uv.lock
uv sync --locked          # CI: install exactly what the lock file specifies, fail on drift
```

If the project still uses bare `requirements.txt`, pin exact versions (`httpx==0.27.0`, not
`httpx>=0.27`) and generate it with `pip-compile` (from `pip-tools`) rather than hand-editing, so
transitive pins stay reproducible.

## Separate Runtime from Dev Dependencies

Use `pyproject.toml`'s `[dependency-groups]` (PEP 735) or `[project.optional-dependencies]` to keep
test/lint/type-check tooling out of the production install:

```toml
[project]
dependencies = ["httpx>=0.27", "pydantic>=2.0"]

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.6", "mypy>=1.11"]
```

Install dev tools with `uv sync --group dev`; production/container installs should use
`uv sync --no-group dev` (or `pip install .` against a wheel with only `dependencies`).

## Vet Before Adding

When adding a new dependency:
1. Confirm it is actively maintained (recent PyPI release, issues addressed, not archived).
2. Check for known CVEs: `pip-audit` (or `uv pip audit` once available) against the resolved set.
3. Review its transitive footprint — a small helper that pulls in a large dependency tree is a cost.
4. Confirm the license is compatible with the project (MIT/BSD/Apache typically fine; GPL/AGPL needs
   review for commercial distribution).
5. Prefer packages that ship type stubs (`py.typed` marker) so `mypy` can check call sites.

See `py-add-package` for an automated vet-and-wire workflow.

## Remove Unused Dependencies

Unused entries in `[project.dependencies]` are dead weight and a supply-chain risk. Audit with
`deptry` (cross-references imports against declared dependencies) periodically, and re-run
`uv sync`/`uv lock` after removing an entry to refresh the lock file.

## Dependency Updates

Keep dependencies reasonably current — unmaintained pins accumulate CVEs. Prefer automated update
PRs (Dependabot, Renovate) that run the full test suite before merging. Never merge a dependency
update without running the quality gate (`ruff check . && mypy . && pytest`) first. A major-version
bump is a code-change opportunity — read the changelog before merging, not just the diff.
