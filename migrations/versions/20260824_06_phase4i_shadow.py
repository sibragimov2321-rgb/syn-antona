"""prospective live shadow validation

Revision ID: 20260824_06
Revises: 20260824_05
"""

import sqlalchemy as sa
from alembic import op

revision = "20260824_06"
down_revision = "20260824_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospective_protocols",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("warmup_hash", sa.String(64), nullable=False),
        sa.Column("protocol_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("protocol_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "shadow_candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("protocol_id", sa.String(64), sa.ForeignKey("prospective_protocols.id"), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 10), nullable=False),
        sa.Column("high", sa.Numeric(24, 10), nullable=False),
        sa.Column("low", sa.Numeric(24, 10), nullable=False),
        sa.Column("close", sa.Numeric(24, 10), nullable=False),
        sa.Column("volume", sa.Numeric(32, 10), nullable=False),
        sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("protocol_id", "exchange", "symbol", "candle_open_time", name="uq_shadow_candle"),
    )
    op.create_table(
        "shadow_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("protocol_id", sa.String(64), sa.ForeignKey("prospective_protocols.id"), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("bid", sa.Numeric(24, 10), nullable=False),
        sa.Column("ask", sa.Numeric(24, 10), nullable=False),
        sa.Column("last", sa.Numeric(24, 10), nullable=False),
        sa.Column("spread", sa.Numeric(24, 10), nullable=False),
        sa.Column("spread_pct", sa.Numeric(18, 12), nullable=False),
        sa.Column("orderbook_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "shadow_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("protocol_id", sa.String(64), sa.ForeignKey("prospective_protocols.id"), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("signal_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("observed_bid", sa.Numeric(24, 10), nullable=False),
        sa.Column("observed_ask", sa.Numeric(24, 10), nullable=False),
        sa.Column("observed_spread", sa.Numeric(24, 10), nullable=False),
        sa.Column("risk_status", sa.String(16), nullable=False, server_default="NOT_APPLICABLE"),
        sa.Column("risk_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("strategy_hash", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("protocol_id", "exchange", "symbol", "candle_open_time", name="uq_shadow_decision"),
    )
    op.create_table(
        "shadow_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("decision_id", sa.String(64), sa.ForeignKey("shadow_decisions.id"), nullable=False, unique=True),
        sa.Column("protocol_id", sa.String(64), sa.ForeignKey("prospective_protocols.id"), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("signal_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_reference", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("stop_loss", sa.Numeric(24, 10), nullable=False),
        sa.Column("take_profit", sa.Numeric(24, 10), nullable=False),
        sa.Column("leverage", sa.Numeric(8, 4), nullable=False, server_default="1"),
        sa.Column("risk_amount", sa.Numeric(24, 10), nullable=False),
        sa.Column("expected_fees", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_fee", sa.Numeric(24, 10), nullable=False),
        sa.Column("observed_spread", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_spread_cost", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_slippage_cost", sa.Numeric(24, 10), nullable=False),
        sa.Column("strategy_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_reference", sa.Numeric(24, 10), nullable=True),
        sa.Column("exit_price", sa.Numeric(24, 10), nullable=True),
        sa.Column("exit_reason", sa.String(32), nullable=True),
        sa.Column("exit_fee", sa.Numeric(24, 10), nullable=False, server_default="0"),
        sa.Column("exit_spread_cost", sa.Numeric(24, 10), nullable=False, server_default="0"),
        sa.Column("exit_slippage_cost", sa.Numeric(24, 10), nullable=False, server_default="0"),
        sa.Column("gross_pnl", sa.Numeric(24, 10), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(24, 10), nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "shadow_daily_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("protocol_id", sa.String(64), sa.ForeignKey("prospective_protocols.id"), nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("protocol_id", "snapshot_date", name="uq_shadow_daily_snapshot"),
    )
    for table in ("shadow_candles", "shadow_quotes", "shadow_decisions", "shadow_trades"):
        op.create_index(f"ix_{table}_protocol_id", table, ["protocol_id"])
        op.create_index(f"ix_{table}_exchange", table, ["exchange"])
        op.create_index(f"ix_{table}_symbol", table, ["symbol"])
    op.create_index("ix_shadow_candles_candle_open_time", "shadow_candles", ["candle_open_time"])
    op.create_index("ix_shadow_candles_candle_close_time", "shadow_candles", ["candle_close_time"])
    op.create_index("ix_shadow_quotes_received_at", "shadow_quotes", ["received_at"])
    op.create_index("ix_shadow_decisions_candle_open_time", "shadow_decisions", ["candle_open_time"])
    op.create_index("ix_shadow_trades_status", "shadow_trades", ["status"])
    op.create_index("ix_shadow_daily_snapshots_protocol_id", "shadow_daily_snapshots", ["protocol_id"])
    op.create_index("ix_shadow_daily_snapshots_snapshot_date", "shadow_daily_snapshots", ["snapshot_date"])


def downgrade() -> None:
    op.drop_table("shadow_daily_snapshots")
    op.drop_table("shadow_trades")
    op.drop_table("shadow_decisions")
    op.drop_table("shadow_quotes")
    op.drop_table("shadow_candles")
    op.drop_table("prospective_protocols")
