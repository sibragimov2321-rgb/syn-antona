"""Phase 4A historical cache and backtest persistence."""

import sqlalchemy as sa
from alembic import op

revision = "20260824_02"
down_revision = "20260824_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("historical_candles", sa.Column("id",sa.Integer(),primary_key=True),sa.Column("exchange",sa.String(20),nullable=False),sa.Column("symbol",sa.String(20),nullable=False),sa.Column("timeframe",sa.String(8),nullable=False),sa.Column("timestamp",sa.DateTime(timezone=True),nullable=False),sa.Column("open",sa.Numeric(24,10),nullable=False),sa.Column("high",sa.Numeric(24,10),nullable=False),sa.Column("low",sa.Numeric(24,10),nullable=False),sa.Column("close",sa.Numeric(24,10),nullable=False),sa.Column("volume",sa.Numeric(32,10),nullable=False),sa.UniqueConstraint("exchange","symbol","timeframe","timestamp",name="uq_candle_key"))
    for column in ("exchange","symbol","timeframe","timestamp"): op.create_index(f"ix_historical_candles_{column}","historical_candles",[column])
    op.create_table("backtest_runs",sa.Column("id",sa.String(64),primary_key=True),sa.Column("exchange",sa.String(20),nullable=False),sa.Column("symbol",sa.String(20),nullable=False),sa.Column("timeframe",sa.String(8),nullable=False),sa.Column("started_at",sa.DateTime(timezone=True),nullable=False),sa.Column("ended_at",sa.DateTime(timezone=True),nullable=False),sa.Column("starting_balance",sa.Numeric(24,10),nullable=False),sa.Column("final_equity",sa.Numeric(24,10),nullable=False),sa.Column("risk_profile",sa.String(20),nullable=False),sa.Column("validation_status",sa.String(32),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_table("backtest_trades",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("run_id",sa.String(64),sa.ForeignKey("backtest_runs.id",ondelete="CASCADE"),nullable=False),sa.Column("side",sa.String(8),nullable=False),sa.Column("entry_time",sa.DateTime(timezone=True),nullable=False),sa.Column("exit_time",sa.DateTime(timezone=True),nullable=False),sa.Column("entry",sa.Numeric(24,10),nullable=False),sa.Column("exit",sa.Numeric(24,10),nullable=False),sa.Column("quantity",sa.Numeric(24,10),nullable=False),sa.Column("pnl",sa.Numeric(24,10),nullable=False),sa.Column("fees",sa.Numeric(24,10),nullable=False),sa.Column("slippage_cost",sa.Numeric(24,10),nullable=False),sa.Column("risk_amount",sa.Numeric(24,10),nullable=False),sa.Column("signal_score",sa.Integer(),nullable=False),sa.Column("regime",sa.String(32),nullable=False),sa.Column("reason",sa.String(32),nullable=False))
    op.create_index("ix_backtest_trades_run_id","backtest_trades",["run_id"])
    op.create_table("backtest_equity",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("run_id",sa.String(64),sa.ForeignKey("backtest_runs.id",ondelete="CASCADE"),nullable=False),sa.Column("timestamp",sa.DateTime(timezone=True),nullable=False),sa.Column("balance",sa.Numeric(24,10),nullable=False),sa.Column("equity",sa.Numeric(24,10),nullable=False),sa.Column("drawdown",sa.Numeric(24,10),nullable=False),sa.Column("realized_pnl",sa.Numeric(24,10),nullable=False),sa.Column("unrealized_pnl",sa.Numeric(24,10),nullable=False))
    op.create_index("ix_backtest_equity_run_id","backtest_equity",["run_id"])
    op.create_table("backtest_metrics",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("run_id",sa.String(64),sa.ForeignKey("backtest_runs.id",ondelete="CASCADE"),nullable=False),sa.Column("section",sa.String(32),nullable=False),sa.Column("name",sa.String(64),nullable=False),sa.Column("value",sa.Text(),nullable=False),sa.UniqueConstraint("run_id","section","name",name="uq_metric_key"))
    op.create_index("ix_backtest_metrics_run_id","backtest_metrics",["run_id"])
    op.create_table("market_regimes",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("run_id",sa.String(64),sa.ForeignKey("backtest_runs.id",ondelete="CASCADE"),nullable=False),sa.Column("timestamp",sa.DateTime(timezone=True),nullable=False),sa.Column("regime",sa.String(32),nullable=False))
    op.create_index("ix_market_regimes_run_id","market_regimes",["run_id"])
    op.create_table("monte_carlo_results",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("run_id",sa.String(64),sa.ForeignKey("backtest_runs.id",ondelete="CASCADE"),nullable=False,unique=True),sa.Column("simulations",sa.Integer(),nullable=False),sa.Column("median_final_equity",sa.Numeric(24,10),nullable=False),sa.Column("worst_5pct",sa.Numeric(24,10),nullable=False),sa.Column("best_5pct",sa.Numeric(24,10),nullable=False),sa.Column("expected_max_drawdown",sa.Numeric(24,10),nullable=False),sa.Column("probability_dd_10",sa.Numeric(12,8),nullable=False),sa.Column("probability_dd_20",sa.Numeric(12,8),nullable=False))


def downgrade() -> None:
    for table in ("monte_carlo_results","market_regimes","backtest_metrics","backtest_equity","backtest_trades","backtest_runs","historical_candles"): op.drop_table(table)
