"""Bybit Unified wallet calculations that vary with account margin mode."""

from decimal import Decimal, InvalidOperation
from typing import Any


def derivatives_available_balance(account: dict[str, Any], coin: str = "USDT") -> Decimal:
    """Return derivatives balance for both cross and isolated margin modes."""

    total_available = account.get("totalAvailableBalance")
    if total_available not in (None, ""):
        return max(Decimal(), _decimal(total_available))

    row = next(
        (item for item in account.get("coin") or [] if item.get("coin") == coin),
        None,
    )
    if row is None or row.get("walletBalance") in (None, ""):
        return Decimal()
    # Bybit's documented isolated-margin formula.
    available = _decimal(row.get("walletBalance")) - sum(
        (
            _decimal(row.get("totalPositionIM")),
            _decimal(row.get("totalOrderIM")),
            _decimal(row.get("locked")),
            _decimal(row.get("bonus")),
        ),
        Decimal(),
    )
    return max(Decimal(), available)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal()
