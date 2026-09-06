"""allow Bybit position slots to be reused by later entries

Revision ID: 20260906_24
Revises: 20260906_23
"""

from alembic import op


revision = "20260906_24"
down_revision = "20260906_23"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_position_profit_states_position_key"
TABLE_NAME = "position_profit_states"


def upgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(INDEX_NAME, TABLE_NAME, ["position_key"], unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(INDEX_NAME, TABLE_NAME, ["position_key"], unique=True)
