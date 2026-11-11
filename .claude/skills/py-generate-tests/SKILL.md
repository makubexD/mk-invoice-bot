---
name: py-generate-tests
description: "Use when adding or reviewing unit and integration tests in a Python project. Covers project layout, fixtures, parametrize, mocking, and async test patterns."
when_to_use: "Use when adding, updating, or reviewing tests in a Python project — e.g. \"write tests for this service\", \"add pytest fixtures\", \"review my test coverage\". Covers project layout, fixtures, parametrize, mocking, and async test patterns."
allowed-tools: Read, Edit, Write, Bash, Grep, Glob
argument-hint: "[file-or-module] (optional)"
---

# Generate Tests (Python)

**Target:** $ARGUMENTS

## Step 1 — Resolve target

If `$ARGUMENTS` is provided, use it as the target module or package. If empty, find source
modules under `src/` (or the package root in `pyproject.toml`) that have no matching
`tests/**/test_<module>.py`; present the top candidates and ask the user to choose before proceeding.

## Step 2 — Discover the runner and layout

Read what the project uses rather than assuming: `[tool.pytest.ini_options]` in `pyproject.toml`,
`pytest.ini` or `setup.cfg`, an existing `conftest.py`, and 2–3 existing test files for the real
layout, fixture style and assertion idioms.

## Step 3 — Read the target

Identify every public function, class and method (the test subjects), the external dependencies
(network, filesystem, clock, environment — the mock boundaries), and the branches, edge cases and
error paths that need coverage.

## Step 4 — Write tests

Follow the pytest conventions in `py-testing` (layout, fixtures and scopes, Arrange / Act / Assert,
parametrize, mocking, async tests, expected exceptions), adapted to what Step 2 found in the repo.

- Write to the mirrored path (`src/myapp/services/user_service.py` →
  `tests/services/test_user_service.py`); create the containing directory if it does not exist.
- Give each public subject from Step 3 a happy-path test, then one test (or `parametrize` case) per
  branch, edge case and error path.
- Mock only the boundaries Step 3 identified; reuse fixtures from an existing `conftest.py` before
  adding new ones, and add a new fixture to `conftest.py` only when a second test file needs it.
- Skip private helpers, trivial properties and third-party internals.

```python
# Example — mirror the fixture and assertion style Step 2 found.
@pytest.fixture
def user_service(user_repo: FakeUserRepository) -> UserService:
    return UserService(repo=user_repo)

def test_get_user_returns_user_when_found(user_service, user_repo):
    # Arrange
    user_repo.add(User(id=42, name="Alice"))

    # Act
    result = user_service.get_user(42)

    # Assert
    assert result.name == "Alice"
```

## Step 5 — Run and report

Run the discovered test command for the new file (`pytest <path> -q`, or the project's own script).
If a new test fails, decide whether the test or the code is wrong: fix a wrong test, but never weaken an assertion to make it pass; report a suspected bug instead. Then emit:

```
## Test Generation Report

Target: <source module>
Tests written to: <test file>
Tests written: <N>
Result: ✅ <N> passed  /  ❌ <detail>
```
