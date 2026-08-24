"""Initial paper-trading journal schema."""

import sqlalchemy as sa
from alembic import op

revision = "20260824_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("stop_loss", sa.Numeric(24, 10), nullable=False),
        sa.Column("take_profit", sa.Numeric(24, 10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trades_trade_id", "trades", ["trade_id"])
    op.create_index("ix_trades_user_id", "trades", ["user_id"])
    op.create_table(
        "trade_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trade_events_trade_id", "trade_events", ["trade_id"])


def downgrade() -> None:
    op.drop_index("ix_trade_events_trade_id", table_name="trade_events")
    op.drop_table("trade_events")
    op.drop_index("ix_trades_user_id", table_name="trades")
    op.drop_index("ix_trades_trade_id", table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")
