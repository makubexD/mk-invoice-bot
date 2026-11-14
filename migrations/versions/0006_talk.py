"""Talk schema: the parked question's rule and the invoice an outbox row talks about (R-3.7, R-11.1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the waiting rule, link outbox rows to their invoice and index that lookup."""
    op.execute("ALTER TABLE invoices ADD COLUMN waiting_rule_id TEXT")
    op.execute("ALTER TABLE outbox ADD COLUMN invoice_id INTEGER REFERENCES invoices(id)")
    op.execute("CREATE INDEX ix_outbox_invoice ON outbox (invoice_id, id) WHERE invoice_id IS NOT NULL")


def downgrade() -> None:
    """Drop the index and both columns."""
    op.execute("DROP INDEX IF EXISTS ix_outbox_invoice")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS invoice_id")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS waiting_rule_id")
