"""persist controlled-live account status for credential-free Telegram

Revision ID: 20260827_15
Revises: 20260827_14
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_15"
down_revision = "20260827_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_wait_runtime",
        sa.Column("trades_today", sa.Integer(), nullable=True),
    )
    op.add_column(
        "signal_wait_runtime",
        sa.Column("daily_realized_pnl", sa.Numeric(24, 10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signal_wait_runtime", "daily_realized_pnl")
    op.drop_column("signal_wait_runtime", "trades_today")
