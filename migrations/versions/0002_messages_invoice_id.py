"""Add messages.invoice_id so a quoted message can name its invoice (R-1.5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Link a message to the invoice it quotes and index that lookup."""
    op.execute("ALTER TABLE messages ADD COLUMN invoice_id INTEGER NULL REFERENCES invoices(id)")
    op.execute("CREATE INDEX ix_messages_invoice_id ON messages (invoice_id)")


def downgrade() -> None:
    """Drop the index and the column."""
    op.execute("DROP INDEX IF EXISTS ix_messages_invoice_id")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS invoice_id")
