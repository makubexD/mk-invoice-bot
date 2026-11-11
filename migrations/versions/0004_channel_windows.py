"""Channel service windows for the WhatsApp 24 h rule (R-1.10).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the per-recipient last-inbound window table."""
    op.execute(
        """
        CREATE TABLE channel_windows (
            channel TEXT NOT NULL,
            recipient TEXT NOT NULL,
            last_inbound_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (channel, recipient)
        )
        """
    )


def downgrade() -> None:
    """Drop the channel windows table."""
    op.execute("DROP TABLE IF EXISTS channel_windows")
