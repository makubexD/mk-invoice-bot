"""Taught-link version column (D137).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the taught-link version, defaulting existing rows to 1 (D137)."""
    op.execute("ALTER TABLE taught_links ADD COLUMN version INT NOT NULL DEFAULT 1")


def downgrade() -> None:
    """Drop the taught-link version column."""
    op.execute("ALTER TABLE taught_links DROP COLUMN IF EXISTS version")
