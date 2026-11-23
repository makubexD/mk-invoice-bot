"""Add the fetch round column to invoices (D166).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23

Adding a nullable column is expand-only: every existing row stays valid and the
previous image never reads or writes ``fetch_round_at``, so old and new code can
run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``invoices.fetch_round_at``, the start of the current fetch round (D166)."""
    op.execute("ALTER TABLE invoices ADD COLUMN fetch_round_at TIMESTAMPTZ NULL")


def downgrade() -> None:
    """Drop ``invoices.fetch_round_at``; the round stamps are lost and every attempt counts again."""
    op.execute("ALTER TABLE invoices DROP COLUMN fetch_round_at")
