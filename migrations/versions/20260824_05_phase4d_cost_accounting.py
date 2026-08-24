"""separate funding and spread cost accounting

Revision ID: 20260824_05
Revises: 20260824_04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260824_05"
down_revision = "20260824_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backtest_trades", sa.Column("funding", sa.Numeric(24, 10), nullable=False, server_default="0"))
    op.add_column("backtest_trades", sa.Column("spread_cost", sa.Numeric(24, 10), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("backtest_trades", "spread_cost")
    op.drop_column("backtest_trades", "funding")
