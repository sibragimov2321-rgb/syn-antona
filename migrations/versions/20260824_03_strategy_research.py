"""Immutable strategy research versions and trade contexts."""

import sqlalchemy as sa
from alembic import op

revision = "20260824_03"
down_revision = "20260824_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backtest_runs",sa.Column("strategy_version",sa.String(64),nullable=False,server_default="baseline_v1"))
    op.add_column("backtest_trades",sa.Column("context_json",sa.Text(),nullable=False,server_default="{}"))
    op.create_table("strategy_versions",sa.Column("version",sa.String(64),primary_key=True),sa.Column("config_json",sa.Text(),nullable=False),sa.Column("config_hash",sa.String(64),nullable=False,unique=True),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_table("strategy_experiments",sa.Column("id",sa.String(64),primary_key=True),sa.Column("strategy_version",sa.String(64),sa.ForeignKey("strategy_versions.version"),nullable=False),sa.Column("symbol",sa.String(20),nullable=False),sa.Column("data_split",sa.String(32),nullable=False),sa.Column("cost_scenario",sa.String(32),nullable=False),sa.Column("metrics_json",sa.Text(),nullable=False),sa.Column("selected",sa.Integer(),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))


def downgrade() -> None:
    op.drop_table("strategy_experiments"); op.drop_table("strategy_versions")
    op.drop_column("backtest_trades","context_json"); op.drop_column("backtest_runs","strategy_version")
