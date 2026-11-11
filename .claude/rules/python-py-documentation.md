---
paths:
  - "**/*.py"
---

# Documentation (Python)

## Docstrings

- Match the project's docstring style (Google, NumPy or reST); never mix them. New projects: Google style.
- Every public module, class and function (no `_` prefix) has a docstring. One line for an obvious function;
  `Args`/`Returns`/`Raises` when behavior, side effects or the caller's contract is not obvious.
- A `Protocol` or ABC documents the invariant implementers must keep, not just the signature.
- Don't restate the type hints, the name (`"""Get the user."""`), or implementation steps that will drift.
- A module with non-obvious scope gets a one- or two-sentence module docstring.
- Deprecate a public symbol with `@warnings.deprecated` (3.13+, `typing_extensions` before) or
  `warnings.warn(..., DeprecationWarning, stacklevel=2)`, and keep it working for at least one release.
- A package README covers what it does, install and quickstart, required environment variable names (never
  values), and the test and quality-gate commands.
- When a signature, return type or raised exception changes, update the docstring in the same commit; update the
  README when install steps, CLI flags or required environment variables change.
