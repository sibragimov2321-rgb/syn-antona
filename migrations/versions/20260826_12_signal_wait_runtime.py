"""persist GET-only signal-wait account status

Revision ID: 20260826_12
Revises: 20260825_11
"""

import sqlalchemy as sa
from alembic import op

revision = "20260826_12"
down_revision = "20260825_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_wait_runtime",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("equity", sa.Numeric(24, 10), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=True),
        sa.Column("open_orders", sa.Integer(), nullable=True),
        sa.Column("account_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("signal_wait_runtime")
