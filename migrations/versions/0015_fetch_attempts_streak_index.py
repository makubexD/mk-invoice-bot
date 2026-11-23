"""Add the fetch-attempts streak index (D172).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23

Adding an index is expand-only: the previous image never reads it, so old and new
code can run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the index BR-F03's per-invoice streak orders by (D172)."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fetch_attempts_portal_invoice_at "
        "ON fetch_attempts (portal_key, invoice_id, at DESC, id DESC)"
    )


def downgrade() -> None:
    """Drop the streak index; the streak query falls back to a scan."""
    op.execute("DROP INDEX IF EXISTS ix_fetch_attempts_portal_invoice_at")
