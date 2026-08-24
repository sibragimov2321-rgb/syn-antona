"""phase 4I resilient prospective collector

Revision ID: 20260824_07
Revises: 20260824_06
"""

import sqlalchemy as sa
from alembic import op

revision = "20260824_07"
down_revision = "20260824_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_candles") as batch:
        batch.add_column(
            sa.Column(
                "recovered_after_downtime",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("recovery_recorded_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("shadow_decisions") as batch:
        batch.alter_column(
            "observed_bid", existing_type=sa.Numeric(24, 10), nullable=True
        )
        batch.alter_column(
            "observed_ask", existing_type=sa.Numeric(24, 10), nullable=True
        )
        batch.alter_column(
            "observed_spread", existing_type=sa.Numeric(24, 10), nullable=True
        )

    with op.batch_alter_table("shadow_daily_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch.add_column(
            sa.Column("report_sent_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        "shadow_collector_state",
        sa.Column(
            "protocol_id",
            sa.String(64),
            sa.ForeignKey("prospective_protocols.id"),
            primary_key=True,
        ),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_db_write_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "shadow_exchange_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "protocol_id",
            sa.String(64),
            sa.ForeignKey("prospective_protocols.id"),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "protocol_id", "exchange", name="uq_shadow_exchange_health"
        ),
    )
    op.create_index(
        "ix_shadow_exchange_health_protocol_id",
        "shadow_exchange_health",
        ["protocol_id"],
    )
    op.create_index(
        "ix_shadow_exchange_health_exchange",
        "shadow_exchange_health",
        ["exchange"],
    )
    op.create_table(
        "shadow_system_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "protocol_id",
            sa.String(64),
            sa.ForeignKey("prospective_protocols.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_shadow_system_events_protocol_id",
        "shadow_system_events",
        ["protocol_id"],
    )
    op.create_index(
        "ix_shadow_system_events_event_type",
        "shadow_system_events",
        ["event_type"],
    )
    op.create_index(
        "ix_shadow_system_events_exchange",
        "shadow_system_events",
        ["exchange"],
    )
    op.create_index(
        "ix_shadow_system_events_created_at",
        "shadow_system_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("shadow_system_events")
    op.drop_table("shadow_exchange_health")
    op.drop_table("shadow_collector_state")
    with op.batch_alter_table("shadow_daily_snapshots") as batch:
        batch.drop_column("report_sent_at")
        batch.drop_column("updated_at")
    with op.batch_alter_table("shadow_decisions") as batch:
        batch.alter_column(
            "observed_spread", existing_type=sa.Numeric(24, 10), nullable=False
        )
        batch.alter_column(
            "observed_ask", existing_type=sa.Numeric(24, 10), nullable=False
        )
        batch.alter_column(
            "observed_bid", existing_type=sa.Numeric(24, 10), nullable=False
        )
    with op.batch_alter_table("shadow_candles") as batch:
        batch.drop_column("recovery_recorded_at")
        batch.drop_column("recovered_after_downtime")
