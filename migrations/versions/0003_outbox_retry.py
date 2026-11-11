"""Outbox retry schedule, dead-letter state and pending index (R-11.5).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the retry schedule, last error and delivery time to outbox."""
    op.execute(
        """
        ALTER TABLE outbox
            ADD COLUMN run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
            ADD COLUMN last_error TEXT NULL,
            ADD COLUMN sent_at TIMESTAMPTZ NULL
        """
    )
    op.execute(
        """
        ALTER TABLE outbox
            ADD CONSTRAINT outbox_state_check
            CHECK (state IN ('pending', 'sent', 'dead'))
        """
    )
    op.execute(
        """
        CREATE INDEX ix_outbox_pending_run_after
        ON outbox (run_after)
        WHERE state = 'pending'
        """
    )


def downgrade() -> None:
    """Drop the index, the state check and the added columns."""
    op.execute("DROP INDEX IF EXISTS ix_outbox_pending_run_after")
    op.execute("ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_state_check")
    op.execute(
        """
        ALTER TABLE outbox
            DROP COLUMN IF EXISTS sent_at,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS run_after
        """
    )
