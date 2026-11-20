"""Allow the dev-only console channel in the schema (D165).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20

Widening a CHECK is expand-only: every existing row still passes and the previous
image never writes ``console``, so old and new code can run against this schema.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen the channel CHECK constraints to accept the console channel (D165)."""
    op.execute(
        """
        ALTER TABLE invoices
            DROP CONSTRAINT invoices_origin_channel_check,
            ADD CONSTRAINT invoices_origin_channel_check
            CHECK (origin_channel IN ('whatsapp', 'telegram', 'console'))
        """
    )
    op.execute(
        """
        ALTER TABLE outbox
            DROP CONSTRAINT outbox_channel_check,
            ADD CONSTRAINT outbox_channel_check
            CHECK (channel IN ('whatsapp', 'telegram', 'github', 'event', 'console'))
        """
    )


def downgrade() -> None:
    """Restore the channel CHECK constraints without the console channel."""
    op.execute(
        """
        ALTER TABLE invoices
            DROP CONSTRAINT invoices_origin_channel_check,
            ADD CONSTRAINT invoices_origin_channel_check
            CHECK (origin_channel IN ('whatsapp', 'telegram'))
        """
    )
    op.execute(
        """
        ALTER TABLE outbox
            DROP CONSTRAINT outbox_channel_check,
            ADD CONSTRAINT outbox_channel_check
            CHECK (channel IN ('whatsapp', 'telegram', 'github', 'event'))
        """
    )
