"""The ``bounced`` batch state (D207).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-27

Widening a check is expand-only: the previous image never writes ``bounced``.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen the batch state check to accept ``bounced`` (D207)."""
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN (
                'open', 'sending', 'sent', 'failed', 'prepared', 'review', 'bounced'
            ))
        """
    )


def downgrade() -> None:
    """Restore the states 0018 allowed.

    A ``bounced`` batch was sent and the mail came back, which the previous image calls
    ``sent``; mapping it keeps the narrow check valid for every row, so a later downgrade
    that rewrites a batch row cannot trip it.
    """
    op.execute("UPDATE batches SET state = 'sent' WHERE state = 'bounced'")
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN ('open', 'sending', 'sent', 'failed', 'prepared', 'review'))
        """
    )
