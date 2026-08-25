"""phase 5e durable first controlled-live proposal

Revision ID: 20260825_11
Revises: 20260825_10
"""

import sqlalchemy as sa
from alembic import op

revision = "20260825_11"
down_revision = "20260825_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "first_live_proposal_state",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_candle_open", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scanned_decision_id", sa.String(64), nullable=True),
        sa.Column("source_decision_id", sa.String(64), nullable=True, unique=True),
        sa.Column("proposal_id", sa.String(64), nullable=True, unique=True),
        sa.Column("available_equity", sa.Numeric(24, 10), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="WAITING_FOR_SIGNAL",
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("first_live_proposal_state")
