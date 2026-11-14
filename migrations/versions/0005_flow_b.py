"""Flow B schema: flow data, the prepared batch state and the eml/manifest file kinds (R-7.2, R-7.8).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the flow data column and widen the batch state and file kind checks."""
    op.execute("ALTER TABLE invoices ADD COLUMN flow_data_json JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN ('open', 'sending', 'sent', 'failed', 'prepared'))
        """
    )
    op.execute("ALTER TABLE files DROP CONSTRAINT files_kind_check")
    op.execute(
        """
        ALTER TABLE files ADD CONSTRAINT files_kind_check
            CHECK (kind IN ('photo', 'pdf', 'xml', 'xlsx', 'captcha', 'eml', 'manifest'))
        """
    )


def downgrade() -> None:
    """Restore the original checks and drop the flow data column.

    The narrow checks come back ``NOT VALID``: they hold for new rows, while existing ``eml``,
    ``manifest`` and ``prepared`` rows survive. Dropping ``flow_data_json`` does lose each
    invoice's folder, sequence claim, comercio and overrides.
    """
    op.execute("ALTER TABLE files DROP CONSTRAINT files_kind_check")
    op.execute(
        """
        ALTER TABLE files ADD CONSTRAINT files_kind_check
            CHECK (kind IN ('photo', 'pdf', 'xml', 'xlsx', 'captcha')) NOT VALID
        """
    )
    op.execute("ALTER TABLE batches DROP CONSTRAINT batches_state_check")
    op.execute(
        """
        ALTER TABLE batches ADD CONSTRAINT batches_state_check
            CHECK (state IN ('open', 'sending', 'sent', 'failed')) NOT VALID
        """
    )
    op.execute("ALTER TABLE invoices DROP COLUMN flow_data_json")
