"""Run one helper subprocess safely: own session, timeout, capped output (R-9.2).

The helper's pipes are drained and its whole process group is killed and reaped on every exit —
timeout, crash or cancellation — so no child it started outlives its job.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping, Sequence
from typing import cast


async def run_helper(
    argv: Sequence[str],
    stdin: bytes,
    env: Mapping[str, str],
    timeout_s: float,
    max_output_bytes: int,
) -> bytes:
    """Run ``argv`` and return its stdout, which is capped at ``max_output_bytes``.

    ``stdin`` is written in full and its pipe closed before stdout is read, so the helper must
    read all of its input before it prints much; the helpers here read one small request first.
    The process runs with ``env`` only, in a new session.

    Raises:
        TimeoutError: when the helper outlives ``timeout_s``; it is killed and reaped first.
        RuntimeError: when the helper prints more than ``max_output_bytes`` or exits non-zero.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            if stdin:
                stdin_stream = cast(asyncio.StreamWriter, process.stdin)
                stdin_stream.write(stdin)
                await stdin_stream.drain()
                stdin_stream.close()
            stdout_stream = cast(asyncio.StreamReader, process.stdout)
            chunks: list[bytes] = []
            remaining = max_output_bytes + 1
            while remaining > 0:
                chunk = await stdout_stream.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            stdout = b"".join(chunks)
            if len(stdout) > max_output_bytes:
                raise RuntimeError("helper output exceeds the cap")
            await process.wait()
    finally:
        # Kill the group in every case, so a child of a helper that already exited dies too.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.returncode is None:
            await process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"helper exited with code {process.returncode}")
    return stdout
