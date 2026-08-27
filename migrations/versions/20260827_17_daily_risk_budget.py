"""apply absolute daily risk budget and persist open planned risk

Revision ID: 20260827_17
Revises: 20260827_16
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_17"
down_revision = "20260827_16"
branch_labels = None
depends_on = None

OLD_PROFILE_HASH = "f0e6296f82534071947ac0f7095abc64338a824286dde11c2d9e0d59749d7913"
NEW_PROFILE_HASH = "b382d2251bed6558bb14cc17e5ee411f76428c111e0fa5c26868b75cc739136f"
OLD_SELECTION_HASH = "58a2fb93d6bc6baaf138403bb75eb8300fcdf40811f51bc71ef7a55969e4262e"
NEW_SELECTION_HASH = "8a453a1c1dd9b274d14b7dec2dc0e61adf3e79d56cf860194cc2e01fbcbb2938"
OLD_SCANNER_HASH = "e04de86c0182ff633b218cd8a1d4503c1d4b4a63a7caa929c724d99a01413838"
NEW_SCANNER_HASH = "dca304642a055cea080adf7da2e74104195e8c54aacd6864a55e41ecc91eab6d"


def upgrade() -> None:
    op.add_column(
        "signal_wait_runtime",
        sa.Column("open_planned_risk", sa.Numeric(24, 10), nullable=True),
    )
    controlled = sa.table(
        "controlled_live_state",
        sa.column("profile_hash", sa.String()),
        sa.column("selection_hash", sa.String()),
    )
    proposals = sa.table(
        "controlled_live_proposals",
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
        proposals.update()
        .where(proposals.c.profile_hash == OLD_PROFILE_HASH)
        .values(profile_hash=NEW_PROFILE_HASH)
    )
    op.execute(
        proposals.update()
        .where(proposals.c.selection_hash == OLD_SELECTION_HASH)
        .values(selection_hash=NEW_SELECTION_HASH)
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
    proposals = sa.table(
        "controlled_live_proposals",
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
        proposals.update()
        .where(proposals.c.profile_hash == NEW_PROFILE_HASH)
        .values(profile_hash=OLD_PROFILE_HASH)
    )
    op.execute(
        proposals.update()
        .where(proposals.c.selection_hash == NEW_SELECTION_HASH)
        .values(selection_hash=OLD_SELECTION_HASH)
    )
    op.execute(
        scanner.update()
        .where(scanner.c.config_hash == NEW_SCANNER_HASH)
        .values(config_hash=OLD_SCANNER_HASH)
    )
    op.drop_column("signal_wait_runtime", "open_planned_risk")
