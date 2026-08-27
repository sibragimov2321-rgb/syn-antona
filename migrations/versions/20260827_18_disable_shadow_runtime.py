"""separate controlled-live runtime from disabled shadow

Revision ID: 20260827_18
Revises: 20260827_17
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_18"
down_revision = "20260827_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "controlled_live_signals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("profile_name", sa.String(64), nullable=False),
        sa.Column("strategy_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("signal_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("stop_loss", sa.Numeric(24, 10), nullable=True),
        sa.Column("take_profit", sa.Numeric(24, 10), nullable=True),
        sa.Column("risk_status", sa.String(16), nullable=False, server_default="NOT_APPLICABLE"),
        sa.Column("risk_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "profile_name", "symbol", "candle_open_time",
            name="uq_controlled_live_signal",
        ),
    )
    op.create_index("ix_controlled_live_signals_profile_name", "controlled_live_signals", ["profile_name"])
    op.create_index("ix_controlled_live_signals_symbol", "controlled_live_signals", ["symbol"])
    op.create_index("ix_controlled_live_signals_candle_open_time", "controlled_live_signals", ["candle_open_time"])
    op.create_index("ix_controlled_live_signals_created_at", "controlled_live_signals", ["created_at"])
    op.create_table(
        "controlled_live_runtime",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("live_trading_enabled", sa.Boolean(), nullable=False),
        sa.Column("controlled_live_enabled", sa.Boolean(), nullable=False),
        sa.Column("manual_first_order_approved", sa.Boolean(), nullable=False),
        sa.Column("real_order_execution_enabled", sa.Boolean(), nullable=False),
        sa.Column("deployment_id", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Preserve all Shadow rows while explicitly releasing the old production
    # execution lease.  The dedicated worker never acquires it again.
    shadow_state = sa.table(
        "shadow_collector_state",
        sa.column("status", sa.String()),
        sa.column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = sa.func.now()
    op.execute(
        shadow_state.update().values(
            status="DISABLED", lease_expires_at=now, updated_at=now
        )
    )


def downgrade() -> None:
    op.drop_table("controlled_live_runtime")
    op.drop_index("ix_controlled_live_signals_created_at", table_name="controlled_live_signals")
    op.drop_index("ix_controlled_live_signals_candle_open_time", table_name="controlled_live_signals")
    op.drop_index("ix_controlled_live_signals_symbol", table_name="controlled_live_signals")
    op.drop_index("ix_controlled_live_signals_profile_name", table_name="controlled_live_signals")
    op.drop_table("controlled_live_signals")
