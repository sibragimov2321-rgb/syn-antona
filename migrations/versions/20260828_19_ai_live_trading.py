"""durable autonomous AI live scanner state

Revision ID: 20260828_19
Revises: 20260827_18
"""

import sqlalchemy as sa
from alembic import op

revision = "20260828_19"
down_revision = "20260827_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_live_runtime",
        sa.Column("runtime_name", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(24), nullable=False, server_default="DISABLED"),
        sa.Column("model", sa.String(128), nullable=False, server_default="NOT_CONFIGURED"),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_market_data_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("equity", sa.Numeric(24, 10), nullable=True),
        sa.Column("available_balance", sa.Numeric(24, 10), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_positions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("open_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_scans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ai_live_scans",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="RUNNING"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("account_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ai_live_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scan_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("stop_loss", sa.Numeric(24, 10), nullable=True),
        sa.Column("take_profit", sa.Numeric(24, 10), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False, server_default="WAIT"),
        sa.Column("proposal_id", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scan_id", "symbol", name="uq_ai_live_scan_symbol"),
    )
    op.create_index("ix_ai_live_decisions_scan_id", "ai_live_decisions", ["scan_id"])
    op.create_index("ix_ai_live_decisions_symbol", "ai_live_decisions", ["symbol"])
    op.create_index("ix_ai_live_decisions_created_at", "ai_live_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_live_decisions_created_at", table_name="ai_live_decisions")
    op.drop_index("ix_ai_live_decisions_symbol", table_name="ai_live_decisions")
    op.drop_index("ix_ai_live_decisions_scan_id", table_name="ai_live_decisions")
    op.drop_table("ai_live_decisions")
    op.drop_table("ai_live_scans")
    op.drop_table("ai_live_runtime")
