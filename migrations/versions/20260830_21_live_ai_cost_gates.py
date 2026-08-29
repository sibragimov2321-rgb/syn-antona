"""persist account-specific Bybit fee rates

Revision ID: 20260830_21
Revises: 20260828_20
"""

import sqlalchemy as sa
from alembic import op


revision = "20260830_21"
down_revision = "20260828_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bybit_fee_rate_cache",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("maker_fee_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("taker_fee_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bybit_fee_rate_cache")
