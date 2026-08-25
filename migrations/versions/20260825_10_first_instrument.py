"""persist immutable controlled-live first instrument

Revision ID: 20260825_10
Revises: 20260825_09
"""

import sqlalchemy as sa
from alembic import op

revision = "20260825_10"
down_revision = "20260825_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controlled_live_state",
        sa.Column("first_symbol", sa.String(32), nullable=True),
    )
    op.add_column(
        "controlled_live_state",
        sa.Column("selection_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "controlled_live_proposals",
        sa.Column("selection_hash", sa.String(64), nullable=True),
    )
    op.execute(
        "UPDATE controlled_live_state SET first_symbol = 'UNSET_PRE_PHASE5B', "
        "selection_hash = 'UNSET_PRE_PHASE5B' WHERE first_symbol IS NULL"
    )
    op.execute(
        "UPDATE controlled_live_proposals SET selection_hash = "
        "'UNSET_PRE_PHASE5B' WHERE selection_hash IS NULL"
    )
    with op.batch_alter_table("controlled_live_state") as batch:
        batch.alter_column("first_symbol", nullable=False)
        batch.alter_column("selection_hash", nullable=False)
    with op.batch_alter_table("controlled_live_proposals") as batch:
        batch.alter_column("selection_hash", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("controlled_live_proposals") as batch:
        batch.drop_column("selection_hash")
    with op.batch_alter_table("controlled_live_state") as batch:
        batch.drop_column("selection_hash")
        batch.drop_column("first_symbol")
