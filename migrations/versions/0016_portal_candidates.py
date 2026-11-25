"""The portal candidate BR-F14 last offered per invoice (D191).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-25

Creating a table is expand-only: the previous image never reads it, so old and new
code can run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the table holding the URL BR-F14 last offered for an invoice (D191)."""
    op.execute(
        """
        CREATE TABLE portal_candidates (
            invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
            url TEXT NOT NULL CHECK (char_length(url) <= 300),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop the portal candidate table."""
    op.execute("DROP TABLE IF EXISTS portal_candidates")
