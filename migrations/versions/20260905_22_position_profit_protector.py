"""add durable local position profit protector state

Revision ID: 20260905_22
Revises: 20260830_21
"""

import sqlalchemy as sa
from alembic import op


revision = "20260905_22"
down_revision = "20260830_21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_profit_states",
        sa.Column("entry_client_order_id", sa.String(128), primary_key=True),
        sa.Column("position_key", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("initial_stop_loss", sa.Numeric(24, 10), nullable=False),
        sa.Column("initial_take_profit", sa.Numeric(24, 10), nullable=False),
        sa.Column("initial_risk_usdt", sa.Numeric(24, 10), nullable=False),
        sa.Column("entry_fee_usdt", sa.Numeric(24, 10), nullable=False),
        sa.Column("estimated_exit_cost_usdt", sa.Numeric(24, 10), nullable=False),
        sa.Column("current_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("current_net_pnl", sa.Numeric(24, 10), nullable=False),
        sa.Column("max_favorable_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("max_favorable_excursion_usdt", sa.Numeric(24, 10), nullable=False),
        sa.Column("max_favorable_r", sa.Numeric(24, 10), nullable=False),
        sa.Column("confirmed_stop_loss", sa.Numeric(24, 10), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_position_profit_states_position_key", "position_profit_states", ["position_key"], unique=True)
    op.create_index("ix_position_profit_states_symbol", "position_profit_states", ["symbol"])
    op.create_table(
        "position_protection_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("entry_client_order_id", sa.String(128), sa.ForeignKey("position_profit_states.entry_client_order_id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("requested_stop_loss", sa.Numeric(24, 10), nullable=True),
        sa.Column("preserved_take_profit", sa.Numeric(24, 10), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entry_client_order_id", "action", "requested_stop_loss", name="uq_position_protection_action_stop"),
    )
    op.create_index("ix_position_protection_events_entry_client_order_id", "position_protection_events", ["entry_client_order_id"])
    op.create_index("ix_position_protection_events_action", "position_protection_events", ["action"])
    op.create_index("ix_position_protection_events_status", "position_protection_events", ["status"])


def downgrade() -> None:
    op.drop_table("position_protection_events")
    op.drop_table("position_profit_states")
