"""The one pending captcha of an invoice (D209).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-27

Creating a table is expand-only: the previous image never reads it, so old and new
code can run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the table holding one pending captcha per invoice (D209)."""
    op.execute(
        """
        CREATE TABLE portal_captchas (
            invoice_id BIGINT PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
            portal_key TEXT NOT NULL,
            form_json JSONB NOT NULL,
            image_path TEXT NOT NULL,
            answer TEXT,
            asked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Drop the pending captcha table."""
    op.execute("DROP TABLE IF EXISTS portal_captchas")
