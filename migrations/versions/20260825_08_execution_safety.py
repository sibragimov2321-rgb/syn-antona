"""durable execution idempotency ledger

Revision ID: 20260825_08
Revises: 20260824_07
"""

import sqlalchemy as sa
from alembic import op

revision = "20260825_08"
down_revision = "20260824_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("exchange_order_id", sa.String(128), nullable=True),
        sa.Column("exchange_status", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "exchange", "account_id", "client_order_id", name="uq_execution_client_order"
        ),
    )
    op.create_index("ix_execution_orders_exchange", "execution_orders", ["exchange"])
    op.create_index("ix_execution_orders_account_id", "execution_orders", ["account_id"])
    op.create_index(
        "ix_execution_orders_client_order_id", "execution_orders", ["client_order_id"]
    )
    op.create_index("ix_execution_orders_symbol", "execution_orders", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_execution_orders_symbol", table_name="execution_orders")
    op.drop_index("ix_execution_orders_client_order_id", table_name="execution_orders")
    op.drop_index("ix_execution_orders_account_id", table_name="execution_orders")
    op.drop_index("ix_execution_orders_exchange", table_name="execution_orders")
    op.drop_table("execution_orders")
