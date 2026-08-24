from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BacktestCostProfile:
    exchange: str
    market_type: str
    maker_fee: Decimal
    taker_fee: Decimal
    slippage: Decimal
    spread: Decimal = Decimal()
    tier: str = "non_vip"
    source: str = "configured assumption"


# Bybit VIP 0 crypto spot, verified against the official fee table on 2026-08-24.
# Slippage remains an explicit conservative model assumption because OHLCV has no order book.
BYBIT_SPOT_NON_VIP = BacktestCostProfile(
    exchange="bybit",
    market_type="spot",
    maker_fee=Decimal("0.001"),
    taker_fee=Decimal("0.001"),
    slippage=Decimal("0.0002"),
    spread=Decimal(),
    source="https://www.bybit.com/en/help-center/article/Trading-Fee-Structure",
)


# Phase 4E research profiles use base/non-VIP spot fees without token discounts.
# Spread and slippage are declared modeling assumptions, not historical order-book observations.
PHASE4E_SPOT_PROFILES = {
    "bybit": BacktestCostProfile(
        "bybit", "spot", Decimal("0.001"), Decimal("0.001"), Decimal("0.00020"), Decimal("0.00010"),
        source="https://www.bybit.com/en/help-center/article/Trading-Fee-Structure",
    ),
    "binance": BacktestCostProfile(
        "binance", "spot", Decimal("0.001"), Decimal("0.001"), Decimal("0.00015"), Decimal("0.00008"),
        source="https://academy.binance.com/articles/how-to-calculate-transaction-fees-on-binance",
    ),
    "okx": BacktestCostProfile(
        "okx", "spot", Decimal("0.0008"), Decimal("0.001"), Decimal("0.00025"), Decimal("0.00012"),
        source="https://www.okx.com/en-gb/help/advance-notice-spot-and-futures-trading-fee-adjustment",
    ),
    "bitget": BacktestCostProfile(
        "bitget", "spot", Decimal("0.001"), Decimal("0.001"), Decimal("0.00030"), Decimal("0.00015"),
        source="https://www.bitget.com/en-CA/support/articles/12560603820584",
    ),
}
