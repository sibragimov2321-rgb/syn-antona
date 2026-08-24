from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.core import BacktestResult, BacktestTrade
from app.backtest.historical import CCXTHistoricalDataProvider
from app.domain.models import Side
from app.strategy_lab.phase4g import (
    FROZEN_CONFIG_HASH,
    TaggedResult,
    frozen_hypothesis,
    leave_one_asset_out,
    period_attribution,
    regime_attribution,
    statistical_validation,
    trade_metrics,
    verify_frozen_implementation,
)
from app.strategy_lab.run_phase4g import _period_boundaries


def _trade(timestamp: datetime, pnl: str, regime: str = "BREAKOUT") -> BacktestTrade:
    return BacktestTrade(
        Side.LONG,
        timestamp,
        timestamp + timedelta(hours=2),
        Decimal("100"),
        Decimal("101"),
        Decimal("1"),
        Decimal(pnl),
        Decimal("0.20"),
        "TP",
        regime=regime,
        risk_amount=Decimal("1"),
        slippage_cost=Decimal("0.04"),
        spread_cost=Decimal("0.02"),
    )


def _result(trades: list[BacktestTrade]) -> BacktestResult:
    return BacktestResult(
        "test",
        Decimal("1000"),
        Decimal("1000") + sum((trade.pnl for trade in trades), Decimal()),
        trades,
        [],
        {"max_drawdown": Decimal(), "max_drawdown_pct": Decimal(), "sharpe": 0, "sortino": 0},
    )


def test_frozen_strategy_hash_is_snapshot_and_source_is_unchanged() -> None:
    assert FROZEN_CONFIG_HASH == "1fc165201603485c20ebc9e4709e710fc9192fb25679f8b0dab730c8cd8301be"
    verify_frozen_implementation()
    hypothesis = frozen_hypothesis()
    assert hypothesis.family == "VOLATILITY_EXPANSION"
    assert hypothesis.timeframe == "1h"


def test_trade_metrics_are_cost_and_drawdown_aware() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    metrics = trade_metrics([_trade(start, "10"), _trade(start + timedelta(days=1), "-4")])
    assert metrics["net_pnl"] == Decimal("6")
    assert metrics["net_pf"] == Decimal("2.5")
    assert metrics["max_drawdown"] == Decimal("4")
    assert metrics["fees"] == Decimal("0.40")


def test_regime_attribution_reports_all_regimes_with_drawdown() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    tagged = [TaggedResult("binance", "BTC/USDT", "normal", _result([_trade(start, "-5")]))]
    regimes = regime_attribution(tagged)
    assert set(regimes) == {"TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY", "BREAKOUT"}
    assert regimes["BREAKOUT"]["max_drawdown"] == Decimal("5")


def test_period_attribution_has_quarters_and_half_years() -> None:
    trades = [
        _trade(datetime(2025, 1, 1, tzinfo=UTC), "2"),
        _trade(datetime(2025, 7, 1, tzinfo=UTC), "3"),
    ]
    tagged = [TaggedResult("binance", "BTC/USDT", "normal", _result(trades))]
    assert set(period_attribution(tagged)) == {"2025-Q1", "2025-Q3"}
    assert set(period_attribution(tagged, half_year=True)) == {"2025-H1", "2025-H2"}


def test_leave_one_asset_out_detects_concentration() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    tagged = [
        TaggedResult("binance", "BTC/USDT", "normal", _result([_trade(start, "10")])),
        TaggedResult("binance", "ETH/USDT", "normal", _result([_trade(start, "-3")])),
        TaggedResult("binance", "SOL/USDT", "normal", _result([_trade(start, "-2")])),
    ]
    diagnostic = leave_one_asset_out(tagged)
    assert diagnostic["diagnosis"] == "CONCENTRATED EDGE"
    assert diagnostic["results"]["WITHOUT BEST PERFORMING ASSET"]["removed_asset"] == "BTC/USDT"


def test_cluster_bootstrap_is_reproducible_and_counts_inherited_tests() -> None:
    tagged = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(20):
        trades = [
            _trade(start + timedelta(days=60 * trade_index), "1" if trade_index % 3 else "-0.5")
            for trade_index in range(10)
        ]
        tagged.append(TaggedResult(f"exchange-{index}", f"ASSET-{index}", "normal", _result(trades)))
    first = statistical_validation(tagged, simulations=100)
    second = statistical_validation(tagged, simulations=100)
    assert first == second
    assert first["inherited_multiple_tests"] == 36
    assert first["trade_count_adequacy"] == "ADEQUATE"


def test_confirmation_boundary_cannot_open_old_holdout() -> None:
    protocol = {
        "boundaries": {
            "start": "2024-01-01T00:00:00+00:00",
            "walk_forward_end": "2025-01-01T00:00:00+00:00",
            "holdout_start": "2025-01-08T00:00:00+00:00",
        }
    }
    _, end, holdout = _period_boundaries(protocol)
    assert end < holdout


@pytest.mark.asyncio
async def test_ccxt_historical_provider_paginates_short_pages() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    base = int(start.timestamp() * 1000)

    class Client:
        calls = []

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            self.calls.append(since)
            assert symbol == "BTC/USDT"
            assert timeframe == "1h"
            if since <= base:
                return [[base, 100, 101, 99, 100, 1], [base + 3_600_000, 100, 102, 99, 101, 2]]
            if since <= base + 7_200_000:
                return [[base + 7_200_000, 101, 103, 100, 102, 3]]
            return []

        def close(self):
            return None

    provider = CCXTHistoricalDataProvider("bitget", Client())
    candles = await provider.fetch("BTC/USDT", "1h", start, start + timedelta(hours=3))
    assert len(candles) == 3
    assert provider.client.calls[:2] == [base, base + 7_200_000]
    assert provider.page_limit == 200
