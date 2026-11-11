---
paths:
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/tests/**/*.py"
  - "**/conftest.py"
---

# Testing (Python)

## Testing (pytest)

- `tests/` mirrors the source tree, one `test_<module>.py` per module; shared fixtures live in `conftest.py`,
  which is never imported.
- Name tests `test_<unit>_<condition>_<outcome>`; one behavior per test, arrange-act-assert.
- Setup is a pytest fixture, not `TestCase.setUp`; `yield` when it needs teardown. Use the narrowest scope that
  works: wider than `function` only for read-only or reset state. `autouse` only for cross-cutting resets.
  A factory fixture when a test needs several instances; `tmp_path` instead of writing to the working tree.
- Same logic with different inputs is one `@pytest.mark.parametrize` test, not copies.
- Mock only boundaries you own (an HTTP client, a repository), never third-party internals or the unit under
  test. `mocker`/`AsyncMock` for patches, `monkeypatch` for env and module settings, a frozen or injected clock
  instead of `datetime.now()`.
- Async tests: `@pytest.mark.asyncio`, or `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`.
- `with pytest.raises(Error, match="...")`, so the wrong message fails too.
- Tests pass in any order and under `pytest -n auto`; order dependence is a shared-state bug.
- Don't test third-party internals, private functions or trivial `__repr__`s.
- Read the coverage floor from `[tool.coverage.report] fail_under`; don't assume one. Use
  `pytest --cov=src --cov-report=term-missing` to find real gaps first.
