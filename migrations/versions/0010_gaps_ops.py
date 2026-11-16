"""Ops state, gap raising and job retention columns (D114, D115, R-9.12).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the ops state table, the gap and job timestamps and the portal index (D114, D115)."""
    op.execute("CREATE TABLE ops_state (key TEXT PRIMARY KEY, last_at TIMESTAMPTZ NOT NULL)")
    op.execute("ALTER TABLE gaps ADD COLUMN raised_at TIMESTAMPTZ")
    op.execute("ALTER TABLE jobs ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute(
        "CREATE INDEX ix_fetch_attempts_portal_at ON fetch_attempts (portal_key, at DESC) "
        "WHERE portal_key IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the portal index, the job and gap timestamps and the ops state table."""
    op.execute("DROP INDEX IF EXISTS ix_fetch_attempts_portal_at")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS created_at")
    op.execute("ALTER TABLE gaps DROP COLUMN IF EXISTS raised_at")
    op.execute("DROP TABLE IF EXISTS ops_state")
