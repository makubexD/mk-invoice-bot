"""Upload rejections: a rejected upload is recorded and answered once (D103).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the table that records an invoice's rejected uploads, keyed on its media ref."""
    op.execute(
        """
        CREATE TABLE upload_rejections (
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            media_ref TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (invoice_id, media_ref)
        )
        """
    )


def downgrade() -> None:
    """Drop the upload rejection table."""
    op.execute("DROP TABLE upload_rejections")
