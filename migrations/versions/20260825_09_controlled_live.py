"""controlled live manual execution state

Revision ID: 20260825_09
Revises: 20260825_08
"""

import sqlalchemy as sa
from alembic import op

revision = "20260825_09"
down_revision = "20260825_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "controlled_live_state",
        sa.Column("profile_name", sa.String(64), primary_key=True),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "first_order_in_progress", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "first_order_executed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "controlled_live_proposals",
        sa.Column("proposal_id", sa.String(64), primary_key=True),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("profile_name", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("preview_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PREVIEWED"),
        sa.Column("client_order_id", sa.String(128), nullable=False, unique=True),
        sa.Column("exchange_order_id", sa.String(128), nullable=True),
        sa.Column("position_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_controlled_live_proposals_proposal_hash",
        "controlled_live_proposals",
        ["proposal_hash"],
        unique=True,
    )
    op.create_index(
        "ix_controlled_live_proposals_profile_name",
        "controlled_live_proposals",
        ["profile_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_controlled_live_proposals_profile_name",
        table_name="controlled_live_proposals",
    )
    op.drop_index(
        "ix_controlled_live_proposals_proposal_hash",
        table_name="controlled_live_proposals",
    )
    op.drop_table("controlled_live_proposals")
    op.drop_table("controlled_live_state")
