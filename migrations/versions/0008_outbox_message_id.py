"""Outbox channel message id: the id a channel assigns to a delivered row (D102).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the channel's message id to outbox, indexed for quote lookups (D102)."""
    op.execute("ALTER TABLE outbox ADD COLUMN channel_message_id TEXT")
    op.execute(
        """
        CREATE INDEX ix_outbox_channel_message
        ON outbox (channel, channel_message_id)
        WHERE channel_message_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the index and the channel message id column."""
    op.execute("DROP INDEX IF EXISTS ix_outbox_channel_message")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS channel_message_id")
