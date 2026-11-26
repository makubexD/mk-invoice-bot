"""The ``review`` batch state (D194).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-26

Widening a check is expand-only: the previous image never writes ``review``.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen the batch state check to accept ``review`` (D194)."""
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN ('open', 'sending', 'sent', 'failed', 'prepared', 'review'))
        """
    )


def downgrade() -> None:
    """Restore the states 0005 allowed.

    The narrow check comes back ``NOT VALID``: it holds for new rows, while existing
    ``review`` rows survive.
    """
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN ('open', 'sending', 'sent', 'failed', 'prepared')) NOT VALID
        """
    )
