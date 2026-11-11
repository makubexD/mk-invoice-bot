---
paths:
  - "**/*.py"
---

# Security (Python)

## Security

- No secrets in source, committed config, logs, exceptions or `__repr__`. Read them from the environment
  (`pydantic-settings` or `os.environ["NAME"]`) and fail fast; no `os.environ.get("TOKEN", "dev-token")` fallback.
  Commit `.env.example` with placeholders, never `.env`.
- `subprocess.run([...])` with an argument list; never `shell=True` with interpolated input.
- Parameterized SQL (`execute(sql, (user_id,))`, SQLAlchemy bound parameters), never f-strings or concatenation.
- No `pickle` from outside your control, `yaml.safe_load` (or `Loader=yaml.SafeLoader`), never plain `yaml.load`, no `eval`/`exec` on external data
  (`ast.literal_eval` for literals).
- A user-supplied path is resolved against an allowed root and checked with `is_relative_to` before use.
- Tokens and IDs come from `secrets`, never `random`; compare secrets with `hmac.compare_digest`, never `==`.
- Every outbound HTTP call sets a timeout; never `verify=False` outside local tests.
