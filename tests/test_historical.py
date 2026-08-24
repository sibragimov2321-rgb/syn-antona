from datetime import UTC, datetime
from decimal import Decimal

from app.backtest.core import Candle
from app.backtest.historical import _dedupe


def test_cache_normalizer_deduplicates_and_sorts() -> None:
    first=Candle(datetime(2024,1,1,tzinfo=UTC),Decimal("1"),Decimal("2"),Decimal("1"),Decimal("2"),Decimal("1"))
    second=Candle(datetime(2024,1,2,tzinfo=UTC),Decimal("2"),Decimal("3"),Decimal("2"),Decimal("3"),Decimal("1"))
    assert _dedupe([second,first,first]) == [first,second]
