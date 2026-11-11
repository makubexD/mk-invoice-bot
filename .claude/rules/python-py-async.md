---
paths:
  - "**/*.py"
---

# Async (Python)

## Async

- No blocking call in a coroutine (`requests`, `time.sleep`, `subprocess.run`, large sync file reads, CPU-bound work).
  Use an async client (one `httpx.AsyncClient` per app) or `await asyncio.to_thread(fn, arg)`.
- Run concurrent work in `asyncio.TaskGroup` (3.11+; `gather` before that). `return_exceptions=True` only when
  partial failure is a result you inspect. Cap a large fan-out with an `asyncio.Semaphore`.
- Never leave a bare `asyncio.create_task(...)` unreferenced: keep it in a `TaskGroup`, an attribute, or a set
  cleared by `add_done_callback`.
- Let `asyncio.CancelledError` propagate; clean up in `finally`, never `except CancelledError: pass`.
- Give every I/O await a deadline: `async with asyncio.timeout(s)` (3.11+) over `wait_for`.
- `async with` / `async for` for async resources; never call `__aenter__`/`__aexit__` by hand.
- `asyncio.Lock`/`Semaphore` between coroutines; `threading.Lock` only for state shared with a real thread.
