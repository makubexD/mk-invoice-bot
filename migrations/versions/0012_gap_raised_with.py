"""Gap raised-with digest column (D157).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the digest of the config entry the gap failed on (D157)."""
    op.execute("ALTER TABLE gaps ADD COLUMN raised_with TEXT")


def downgrade() -> None:
    """Drop the raised-with digest column."""
    op.execute("ALTER TABLE gaps DROP COLUMN IF EXISTS raised_with")
