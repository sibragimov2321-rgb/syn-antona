"""Sanitized read-only production status for deployment verification."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db import (
    ControlledLiveRuntimeRecord,
    ControlledLiveSignalRecord,
    ExecutionOrderRecord,
    SessionLocal,
    ShadowCollectorStateRecord,
)
from app.shadow.engine import PROTOCOL_ID
from app.trading.controlled_live import CONTROLLED_LIVE_V1
from app.trading.controlled_universe import PROFILE_NAME


def main() -> None:
    with SessionLocal() as session:
        shadow = session.get(ShadowCollectorStateRecord, PROTOCOL_ID)
        controlled = session.get(ControlledLiveRuntimeRecord, PROFILE_NAME)
        lease_active = bool(
            shadow
            and (
                shadow.lease_expires_at.replace(tzinfo=UTC)
                if shadow.lease_expires_at.tzinfo is None
                else shadow.lease_expires_at
            )
            > datetime.now(UTC)
        )
        values = {
            "SHADOW_STATUS": shadow.status if shadow else "MISSING",
            "SHADOW_LEASE_ACTIVE": lease_active,
            "CONTROLLED_STATUS": controlled.status if controlled else "MISSING",
            "CONTROLLED_HEARTBEAT": controlled.heartbeat_at if controlled else None,
            "DRY_RUN": controlled.dry_run if controlled else None,
            "LIVE_TRADING_ENABLED": (
                controlled.live_trading_enabled if controlled else None
            ),
            "CONTROLLED_LIVE_ENABLED": (
                controlled.controlled_live_enabled if controlled else None
            ),
            "REAL_ORDER_EXECUTION": (
                controlled.real_order_execution_enabled if controlled else None
            ),
            "CONTROLLED_SIGNALS": session.scalar(
                select(func.count()).select_from(ControlledLiveSignalRecord)
            ),
            "EXECUTION_LEDGER_ROWS": session.scalar(
                select(func.count()).select_from(ExecutionOrderRecord)
            ),
            "THRESHOLD": CONTROLLED_LIVE_V1.signal_threshold,
            "RISK_PCT": CONTROLLED_LIVE_V1.risk_per_trade_pct,
            "LEVERAGE": CONTROLLED_LIVE_V1.leverage,
            "MAX_POSITIONS": CONTROLLED_LIVE_V1.max_positions,
            "MAX_TRADES_PER_DAY": (
                CONTROLLED_LIVE_V1.max_trades_per_day or "UNLIMITED"
            ),
            "DAILY_MAX_LOSS": CONTROLLED_LIVE_V1.daily_max_loss_usdt,
            "TOTAL_LOSS_LIMIT": CONTROLLED_LIVE_V1.total_experiment_loss_limit,
        }
        for key, value in values.items():
            print(f"{key}={value}")


if __name__ == "__main__":
    main()
