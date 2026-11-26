"""Flow B persistence: flow data, sequence claims, batches and the photo purge.

Every function takes the caller's connection and never commits: the caller owns
the transaction (R-7.1, R-7.5, R-7.8, R-9.7, D88).
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast, get_args

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

#: The states a newly opened batch may start in (R-7.5, R-7.8, D194).
BatchState = Literal["prepared", "sending", "review"]


async def load_flow_data(conn: AsyncConnection[Any], invoice_id: int) -> dict[str, Any]:
    """Return the invoice's flow data, or an empty mapping when it has none; does not commit.

    An unknown invoice also yields an empty mapping (D88).
    """
    cur = await conn.execute("SELECT flow_data_json FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None or not isinstance(row[0], dict):
        return {}
    return cast(dict[str, Any], row[0])


async def merge_flow_data(conn: AsyncConnection[Any], invoice_id: int, patch: Mapping[str, Any]) -> None:
    """Merge patch into the invoice's flow data; does not commit (D88)."""
    await conn.execute(
        "UPDATE invoices SET flow_data_json = flow_data_json || %s::jsonb WHERE id = %s",
        (Jsonb(patch), invoice_id),
    )


async def claim_seq(
    conn: AsyncConnection[Any],
    flow_id: str,
    year: int,
    month: int,
    listed_max: int,
    invoice_id: int,
) -> int:
    """Claim the invoice's folder sequence number for flow_id's month (D88).

    An invoice that already claimed a number keeps it. Otherwise the number is
    ``max(listed_max, the month's highest claimed number) + 1``, stored with its
    year and month in the invoice's flow data. The advisory lock keeps claims for
    the same flow and month serialized. Does not commit.
    """
    lock_key = f"seq:{flow_id}:{year}-{month}"
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

    cur = await conn.execute(
        "SELECT flow_data_json->>'seq' FROM invoices WHERE id = %s FOR UPDATE",
        (invoice_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    if row[0] is not None:
        return int(row[0])

    cur = await conn.execute(
        """
        SELECT COALESCE(MAX((flow_data_json->>'seq')::int), 0)
        FROM invoices
        WHERE flow_id = %s
          AND (flow_data_json->>'year')::int = %s
          AND (flow_data_json->>'month')::int = %s
        """,
        (flow_id, year, month),
    )
    row = await cur.fetchone()
    highest = 0 if row is None or row[0] is None else int(row[0])
    seq = max(listed_max, highest) + 1
    await merge_flow_data(conn, invoice_id, {"seq": seq, "year": year, "month": month})
    return seq


async def open_batch(
    conn: AsyncConnection[Any],
    invoice_id: int,
    flow_id: str,
    state: BatchState,
) -> int:
    """Return the invoice's batch, opening a batch of one in state when it has none (R-7.5).

    Does not commit.
    """
    if state not in get_args(BatchState):
        raise ValueError(f"unknown batch state {state!r}")

    cur = await conn.execute("SELECT batch_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    if row[0] is not None:
        return int(row[0])

    cur = await conn.execute(
        "INSERT INTO batches (flow_id, state) VALUES (%s, %s) RETURNING id",
        (flow_id, state),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO batches RETURNING id yielded no row")
    batch_id = int(row[0])
    await conn.execute("UPDATE invoices SET batch_id = %s WHERE id = %s", (batch_id, invoice_id))
    return batch_id


async def get_batch_state(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the state of the invoice's batch, or None when it has none (R-7.8).

    Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT b.state
        FROM invoices AS i
        LEFT JOIN batches AS b ON b.id = i.batch_id
        WHERE i.id = %s
        """,
        (invoice_id,),
    )
    row = await cur.fetchone()
    return None if row is None or row[0] is None else str(row[0])


async def set_batch_state(conn: AsyncConnection[Any], batch_id: int, state: str) -> None:
    """Move the batch to state, stamping sent_at when it becomes sent (R-7.8); does not commit."""
    await conn.execute(
        """
        UPDATE batches
        SET state = %s,
            sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END
        WHERE id = %s
        """,
        (state, state, batch_id),
    )


async def take_photo_paths(conn: AsyncConnection[Any], invoice_id: int) -> list[Path]:
    """Clear the invoice's photo paths and return their old values (R-9.7); does not commit.

    The caller deletes the files after the transaction commits.
    """
    cur = await conn.execute(
        """
        WITH taken AS (
            SELECT id, local_path
            FROM files
            WHERE invoice_id = %s AND kind = 'photo' AND local_path IS NOT NULL
            ORDER BY id
            FOR UPDATE
        )
        UPDATE files AS f
        SET local_path = NULL
        FROM taken
        WHERE f.id = taken.id
        RETURNING taken.local_path
        """,
        (invoice_id,),
    )
    rows = await cur.fetchall()
    return [Path(str(row[0])) for row in rows]
