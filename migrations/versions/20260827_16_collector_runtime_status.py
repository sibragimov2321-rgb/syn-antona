"""persist execution runtime flags on the active collector lease

Revision ID: 20260827_16
Revises: 20260827_15
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_16"
down_revision = "20260827_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "dry_run",
        "live_trading_enabled",
        "controlled_live_enabled",
        "manual_first_order_approved",
        "real_order_execution_enabled",
    ):
        op.add_column(
            "shadow_collector_state",
            sa.Column(name, sa.Boolean(), nullable=True),
        )
    op.add_column(
        "shadow_collector_state",
        sa.Column("deployment_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "shadow_collector_state",
        sa.Column("replica_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "shadow_collector_state",
        sa.Column("last_start_cause", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    for name in (
        "last_start_cause",
        "replica_id",
        "deployment_id",
        "real_order_execution_enabled",
        "manual_first_order_approved",
        "controlled_live_enabled",
        "live_trading_enabled",
        "dry_run",
    ):
        op.drop_column("shadow_collector_state", name)
