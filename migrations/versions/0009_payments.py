"""Payments, payment proposals and mail jobs (D107, D108).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-15

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the payments, payment proposals and mail jobs tables (D107, D108)."""
    op.execute(
        """
        CREATE TABLE payments (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            flow_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            paid_on DATE NOT NULL,
            amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE invoices ADD COLUMN payment_id BIGINT REFERENCES payments(id)")
    op.execute(
        """
        CREATE TABLE payment_proposals (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            profile_id TEXT NOT NULL,
            flow_id TEXT NOT NULL,
            paid_on DATE NOT NULL,
            amount NUMERIC(12,2) NOT NULL,
            sets_json JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'confirmed', 'rejected', 'expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mail_jobs (
            job_id TEXT PRIMARY KEY,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            receipt_at TIMESTAMPTZ,
            receipt_error TEXT,
            stale_alerted_at TIMESTAMPTZ,
            overdue_alerted_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX ix_mail_jobs_invoice ON mail_jobs (invoice_id)")


def downgrade() -> None:
    """Drop the mail jobs, payment proposals and payments tables behind them."""
    op.execute("DROP INDEX IF EXISTS ix_mail_jobs_invoice")
    op.execute("DROP TABLE IF EXISTS mail_jobs")
    op.execute("DROP TABLE IF EXISTS payment_proposals")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS payment_id")
    op.execute("DROP TABLE IF EXISTS payments")
