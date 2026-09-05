"""add hybrid AI scanner runtime and Hermes call counters

Revision ID: 20260906_23
Revises: 20260905_22
"""

import sqlalchemy as sa
from alembic import op


revision = "20260906_23"
down_revision = "20260905_22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_live_runtime",
        sa.Column("core_hermes_call_day", sa.Date(), nullable=True),
    )
    op.add_column(
        "ai_live_runtime",
        sa.Column(
            "core_hermes_calls_today",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "ai_market_discovery_runtime",
        sa.Column("runtime_name", sa.String(48), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_local_slot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_local_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("symbols_scanned", sa.Integer(), nullable=False),
        sa.Column("eligible_symbols", sa.Integer(), nullable=False),
        sa.Column("top_candidates_json", sa.Text(), nullable=False),
        sa.Column("last_candidate_signature", sa.String(64), nullable=True),
        sa.Column("last_candidate_score", sa.Numeric(24, 10), nullable=True),
        sa.Column("last_hermes_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hermes_call_day", sa.Date(), nullable=True),
        sa.Column("hermes_calls_today", sa.Integer(), nullable=False),
        sa.Column("last_decisions_json", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ai_market_discovery_runtime")
    op.drop_column("ai_live_runtime", "core_hermes_calls_today")
    op.drop_column("ai_live_runtime", "core_hermes_call_day")
