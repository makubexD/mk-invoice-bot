---
paths:
  - "**/*.py"
---

# Conventions (Python)

- Annotate every function signature and public variable; `X | None`, not `Optional[X]`.
- Structured data is a `@dataclass` or `TypedDict`, not a dict with string keys.
- No mutable default arguments: `def f(items=None)` and set it inside.
- `with` for anything that needs cleanup; `pathlib.Path`, not `os.path`.
- Raise a specific exception (`ValueError`, a custom subclass), never bare `Exception`.
- PEP 8 names (`snake_case`, `PascalCase` classes, `UPPER_SNAKE` constants, `_private`), enforced by `ruff`'s `N`
  rules; imports sorted stdlib, third-party, local by `ruff`/`isort`.

For a code review in this repo, use the `py-code-reviewer` agent when it is installed, not a general-purpose
reviewer: it reviews against these rules.
