from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import ClosedPosition, Position, Side
from app.statistics import calculate_statistics


def closed(pnl: Decimal, side: Side) -> ClosedPosition:
    position = Position(
        "id", 1, "BTCUSDT", side, Decimal("1"), Decimal("100"), Decimal("95"),
        Decimal("110"), Decimal("1"), Decimal("0"), datetime.now(UTC), Decimal("5"),
    )
    return ClosedPosition(position, Decimal("100"), Decimal("0"), pnl, "TEST", datetime.now(UTC))


def test_statistics_calculates_profit_factor_and_directional_pnl() -> None:
    stats = calculate_statistics([closed(Decimal("10"), Side.LONG), closed(Decimal("-4"), Side.SHORT)])
    assert stats.trades == 2
    assert stats.win_rate == Decimal("50")
    assert stats.profit_factor == Decimal("2.5")
    assert stats.long_pnl == Decimal("10")
    assert stats.short_pnl == Decimal("-4")
