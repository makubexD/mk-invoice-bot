"""The portal probe's state per issuer (D193).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25

Creating a table is expand-only: the previous image never reads it, so old and new
code can run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the table holding one probe per issuer and taught-link version (D193)."""
    op.execute(
        """
        CREATE TABLE portal_probes (
            ruc_emisor BIGINT PRIMARY KEY,
            link_version INTEGER NOT NULL,
            invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
            state TEXT NOT NULL CHECK (state IN ('queued', 'done')),
            tries INTEGER NOT NULL DEFAULT 0,
            outcome TEXT,
            report_json JSONB,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop the portal probe table."""
    op.execute("DROP TABLE IF EXISTS portal_probes")
