"""update current controlled-live limits and durable loss anchors

Revision ID: 20260827_14
Revises: 20260826_13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_14"
down_revision = "20260826_13"
branch_labels = None
depends_on = None

OLD_PROFILE_HASH = "f9aef880cc9ac20b80d6db01adf8c0dab6e6085d84fd872889611013b1e69079"
NEW_PROFILE_HASH = "f0e6296f82534071947ac0f7095abc64338a824286dde11c2d9e0d59749d7913"
OLD_SELECTION_HASH = "63a3b52a6aecc19202d778ba6a50885eb9f8db9707bfc9aec5defc358e08a73b"
NEW_SELECTION_HASH = "58a2fb93d6bc6baaf138403bb75eb8300fcdf40811f51bc71ef7a55969e4262e"
OLD_SCANNER_HASH = "cb5b7cf5f2fedb07dbafbcb97bbf30fc99638cb9022f752a554a96e2e397f116"
NEW_SCANNER_HASH = "e04de86c0182ff633b218cd8a1d4503c1d4b4a63a7caa929c724d99a01413838"


def upgrade() -> None:
    op.add_column(
        "controlled_live_state",
        sa.Column("experiment_start_equity", sa.Numeric(24, 10), nullable=True),
    )
    op.add_column(
        "controlled_live_state",
        sa.Column("starting_day_equity", sa.Numeric(24, 10), nullable=True),
    )
    op.add_column(
        "controlled_live_state",
        sa.Column("starting_day_utc", sa.Date(), nullable=True),
    )
    op.add_column(
        "controlled_live_state",
        sa.Column(
            "automatic_execution_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    controlled = sa.table(
        "controlled_live_state",
        sa.column("profile_hash", sa.String()),
        sa.column("selection_hash", sa.String()),
    )
    scanner = sa.table(
        "multi_symbol_scanner_state",
        sa.column("config_hash", sa.String()),
    )
    op.execute(
        controlled.update()
        .where(controlled.c.profile_hash == OLD_PROFILE_HASH)
        .values(profile_hash=NEW_PROFILE_HASH, selection_hash=NEW_SELECTION_HASH)
    )
    op.execute(
        scanner.update()
        .where(scanner.c.config_hash == OLD_SCANNER_HASH)
        .values(config_hash=NEW_SCANNER_HASH)
    )


def downgrade() -> None:
    controlled = sa.table(
        "controlled_live_state",
        sa.column("profile_hash", sa.String()),
        sa.column("selection_hash", sa.String()),
    )
    scanner = sa.table(
        "multi_symbol_scanner_state",
        sa.column("config_hash", sa.String()),
    )
    op.execute(
        controlled.update()
        .where(controlled.c.profile_hash == NEW_PROFILE_HASH)
        .values(profile_hash=OLD_PROFILE_HASH, selection_hash=OLD_SELECTION_HASH)
    )
    op.execute(
        scanner.update()
        .where(scanner.c.config_hash == NEW_SCANNER_HASH)
        .values(config_hash=OLD_SCANNER_HASH)
    )
    op.drop_column("controlled_live_state", "starting_day_utc")
    op.drop_column("controlled_live_state", "starting_day_equity")
    op.drop_column("controlled_live_state", "experiment_start_equity")
    op.drop_column("controlled_live_state", "automatic_execution_enabled")
