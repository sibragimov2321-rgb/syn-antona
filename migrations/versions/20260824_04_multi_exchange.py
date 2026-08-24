"""Multi-exchange accounts and health persistence."""

import sqlalchemy as sa
from alembic import op

revision = "20260824_04"
down_revision = "20260824_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("exchange", sa.String(32), nullable=False, server_default="paper"))
    op.add_column("trades", sa.Column("account_id", sa.String(64), nullable=True))
    op.add_column("trades", sa.Column("position_id", sa.String(128), nullable=True))
    op.add_column("trades", sa.Column("strategy_version", sa.String(64), nullable=False, server_default="unknown"))
    op.create_index("ix_trades_exchange", "trades", ["exchange"])
    op.create_index("ix_trades_account_id", "trades", ["account_id"])
    op.create_table(
        "exchange_accounts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("account_name", sa.String(80), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("encrypted_passphrase", sa.Text(), nullable=True),
        sa.Column("permissions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("account_status", sa.String(24), nullable=False, server_default="DISCONNECTED"),
        sa.Column("sandbox", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "exchange", "account_name", name="uq_exchange_account_name"),
    )
    op.create_index("ix_exchange_accounts_user_id", "exchange_accounts", ["user_id"])
    op.create_index("ix_exchange_accounts_exchange", "exchange_accounts", ["exchange"])
    op.create_table(
        "exchange_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(64), sa.ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("latency_ms", sa.Numeric(18, 6), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_exchange_health_account_id", "exchange_health", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_exchange_health_account_id", table_name="exchange_health")
    op.drop_table("exchange_health")
    op.drop_index("ix_exchange_accounts_exchange", table_name="exchange_accounts")
    op.drop_index("ix_exchange_accounts_user_id", table_name="exchange_accounts")
    op.drop_table("exchange_accounts")
    op.drop_index("ix_trades_account_id", table_name="trades")
    op.drop_index("ix_trades_exchange", table_name="trades")
    op.drop_column("trades", "strategy_version")
    op.drop_column("trades", "position_id")
    op.drop_column("trades", "account_id")
    op.drop_column("trades", "exchange")
