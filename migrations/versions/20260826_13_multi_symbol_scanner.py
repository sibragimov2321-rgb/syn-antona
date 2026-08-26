"""durable controlled-live multi-symbol scanner

Revision ID: 20260826_13
Revises: 20260826_12
"""

import sqlalchemy as sa
from alembic import op

revision = "20260826_13"
down_revision = "20260826_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "multi_symbol_scanner_state",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_candle_open", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "multi_symbol_scanner_instruments",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("internal_symbol", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.Text(), nullable=False),
        sa.Column("instrument_status", sa.String(32), nullable=False),
        sa.Column("contract_type", sa.String(32), nullable=False),
        sa.Column("bid_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("ask_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("tick_size", sa.Numeric(24, 10), nullable=False),
        sa.Column("minimum_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("quantity_step", sa.Numeric(24, 10), nullable=False),
        sa.Column("minimum_notional", sa.Numeric(24, 10), nullable=False),
        sa.Column("actual_minimum_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("actual_minimum_notional", sa.Numeric(24, 10), nullable=False),
        sa.Column("spread_pct", sa.Numeric(18, 12), nullable=False),
        sa.Column("turnover_24h", sa.Numeric(30, 10), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("multi_symbol_scanner_instruments")
    op.drop_table("multi_symbol_scanner_state")
