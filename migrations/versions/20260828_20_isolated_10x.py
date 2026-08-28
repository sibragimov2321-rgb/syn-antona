"""move existing controlled-live state to isolated 10x configuration

Revision ID: 20260828_20
Revises: 20260828_19
"""

import sqlalchemy as sa
from alembic import op

revision = "20260828_20"
down_revision = "20260828_19"
branch_labels = None
depends_on = None

OLD_PROFILE_HASH = "b382d2251bed6558bb14cc17e5ee411f76428c111e0fa5c26868b75cc739136f"
NEW_PROFILE_HASH = "e92b8414d9a1d7fb5d6aa0406d59a6fb0e0408f602635d4a5ec4be99eb231c31"
OLD_SELECTION_HASH = "8a453a1c1dd9b274d14b7dec2dc0e61adf3e79d56cf860194cc2e01fbcbb2938"
NEW_SELECTION_HASH = "04345c31c8775369efa3d5ea5e2f41a3bb19ff50802af676210046a8f1acda40"
OLD_SCANNER_HASH = "dca304642a055cea080adf7da2e74104195e8c54aacd6864a55e41ecc91eab6d"
NEW_SCANNER_HASH = "590077ef41c746aad6d0094a845b3fa0b6f744181e63d1b41c3f6c573a8dc507"


def upgrade() -> None:
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
