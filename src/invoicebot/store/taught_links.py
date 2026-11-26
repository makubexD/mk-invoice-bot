"""Taught links: the portal a buyer taught for an issuer (BR-F02, BR-S06).

A link is stored per issuer RUC and moves through ``proposed``, ``approved`` or
``rejected``: a sender proposes it (BR-S06) and the owner decides. Every
function takes the caller's connection and never commits: the caller owns the
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from psycopg import AsyncConnection


@dataclass(frozen=True)
class TaughtLink:
    """One issuer's taught portal link and where it is in the owner's review."""

    ruc_emisor: int
    url: str
    profile_id: str
    state: str
    version: int = 1


async def exists(conn: AsyncConnection[Any], ruc_emisor: int) -> bool:
    """Return True when any taught link row exists for ``ruc_emisor`` (BR-F02); does not commit."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM taught_links WHERE ruc_emisor = %s)",
        (ruc_emisor,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT EXISTS over taught_links yielded no row")
    return bool(row[0])


async def get(conn: AsyncConnection[Any], ruc_emisor: int) -> TaughtLink | None:
    """Return the issuer's taught link in any state, or None (BR-S06); does not commit."""
    cur = await conn.execute(
        "SELECT ruc_emisor, url, profile_id, state, version FROM taught_links WHERE ruc_emisor = %s",
        (ruc_emisor,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return TaughtLink(ruc_emisor=row[0], url=row[1], profile_id=row[2], state=row[3], version=row[4])


async def approved_url(conn: AsyncConnection[Any], ruc_emisor: int, version: int) -> str | None:
    """Return the issuer's URL when the link at ``version`` is approved, else None (D193); does not commit."""
    cur = await conn.execute(
        "SELECT url FROM taught_links WHERE ruc_emisor = %s AND version = %s AND state = 'approved'",
        (ruc_emisor, version),
    )
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def approved_hosts(conn: AsyncConnection[Any]) -> frozenset[str]:
    """Return the lowercase hostnames of every approved link (D191); does not commit.

    A URL ``urlsplit`` cannot give a hostname for is skipped.
    """
    cur = await conn.execute("SELECT url FROM taught_links WHERE state = 'approved'")
    rows = await cur.fetchall()
    hosts = (urlsplit(row[0]).hostname for row in rows)
    return frozenset(host for host in hosts if host is not None)


async def propose(
    conn: AsyncConnection[Any],
    ruc_emisor: int,
    url: str,
    profile_id: str,
) -> int | None:
    """Store ``url`` as proposed, or replace a rejected or different proposed link (BR-S06, D192).

    Returns the proposal's version, or None when the same URL is already proposed or
    the link is approved: a newer, different link replaces the one that waits for the
    owner, while an approved link is not replaced. Does not commit.
    """
    cur = await conn.execute(
        """
        INSERT INTO taught_links (ruc_emisor, url, profile_id, state)
        VALUES (%s, %s, %s, 'proposed')
        ON CONFLICT (ruc_emisor) DO UPDATE
        SET url = EXCLUDED.url, profile_id = EXCLUDED.profile_id,
            state = 'proposed', approved_at = NULL,
            version = taught_links.version + 1, at = now()
        WHERE taught_links.state = 'rejected'
           OR (taught_links.state = 'proposed' AND taught_links.url <> EXCLUDED.url)
        RETURNING version
        """,
        (ruc_emisor, url, profile_id),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def decide(
    conn: AsyncConnection[Any],
    ruc_emisor: int,
    version: int,
    approved: bool,
) -> TaughtLink | None:
    """Move proposed ``version`` to approved or rejected (BR-S06); does not commit.

    The proposed row is locked first, so two presses cannot both decide it, and a
    press for an older proposal matches nothing. Returns None when there is no
    proposed link at ``version``.
    """
    cur = await conn.execute(
        """
        SELECT url, profile_id FROM taught_links
        WHERE ruc_emisor = %s AND version = %s AND state = 'proposed'
        FOR UPDATE
        """,
        (ruc_emisor, version),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    state = "approved" if approved else "rejected"
    await conn.execute(
        """
        UPDATE taught_links
        SET state = %s, approved_at = CASE WHEN %s THEN now() ELSE NULL END
        WHERE ruc_emisor = %s
        """,
        (state, approved, ruc_emisor),
    )
    return TaughtLink(ruc_emisor=ruc_emisor, url=row[0], profile_id=row[1], state=state, version=version)
