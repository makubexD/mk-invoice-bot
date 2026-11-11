---
paths:
  - "**/*.py"
  - "**/pyproject.toml"
---

# Project Layout (Python)

## Project Layout

- A package uses the `src/` layout (`src/<pkg>/`, `tests/` beside it); a flat root is only for a small script.
- `pyproject.toml` is the one manifest: metadata, dependencies and `[tool.*]` config. Don't add `setup.py`,
  `setup.cfg`, `.flake8`, `mypy.ini` or `pytest.ini`.
- Dependencies point inward: `domain/` never imports `api/` or `infrastructure/`; only `core/` (config, wiring)
  is imported everywhere.
- Configuration is one typed `pydantic-settings` object (or Django settings) validated at startup, not
  `os.environ[...]` read inside business logic.
- Console scripts go in `[project.scripts]`.
- One virtual environment per project (`uv venv` + `uv sync` from `uv.lock`); never the system Python.
- `tests/` mirrors the package: `src/<pkg>/domain/pricing.py` is tested in `tests/unit/domain/test_pricing.py`.
