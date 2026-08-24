from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backtest.persistence import BacktestRepository
from app.backtest.research import ResearchFeature, StrategyConfig, action_for
from app.backtest.research_framework import HoldoutVault
from app.db import Base
from app.domain.models import Decision


def feature(**overrides) -> ResearchFeature:
    values=dict(timestamp=datetime(2025,1,1,tzinfo=UTC),price=Decimal("100"),decision=Decision.LONG,signal_score=82,proposed_stop=Decimal("98"),proposed_target=Decimal("104"),rsi=Decimal("60"),macd=Decimal("1"),atr=Decimal("1"),atr_pct=Decimal("0.01"),ema_50=Decimal("105"),ema_200=Decimal("100"),adx=Decimal("25"),volume_ratio=Decimal("1.2"),breakout="UP",trend_regime="BULL",volatility_regime="NORMAL_VOLATILITY",primary_regime="BULL",timeframe_alignment=4)
    values.update(overrides)
    return ResearchFeature(**values)


def test_research_filters_and_cost_coverage() -> None:
    config=StrategyConfig("candidate",min_score=80,adx_min=Decimal("20"),volume_ratio_min=Decimal("1.1"),require_breakout=True,direction_filter=True,allow_sideways=False,cost_coverage=Decimal("2"))
    assert action_for(config,feature(),Decimal("0.0006"),Decimal("0.0002")) is not None
    assert action_for(config,feature(adx=Decimal("10")),Decimal("0.0006"),Decimal("0.0002")) is None
    assert action_for(config,feature(trend_regime="BEAR"),Decimal("0.0006"),Decimal("0.0002")) is None


def test_holdout_can_only_open_after_selection_once() -> None:
    vault=HoldoutVault()
    with pytest.raises(RuntimeError): vault.open("a","BTC/USDT")
    vault.lock_selection("a"); vault.open("a","BTC/USDT")
    with pytest.raises(RuntimeError): vault.open("a","BTC/USDT")
    with pytest.raises(RuntimeError): vault.lock_selection("b")


def test_strategy_version_is_immutable(tmp_path) -> None:
    engine=create_engine(f"sqlite:///{tmp_path/'research.db'}"); Base.metadata.create_all(engine)
    repository=BacktestRepository(sessionmaker(bind=engine,expire_on_commit=False))
    repository.register_strategy("trend_v2",{"min_score":75})
    repository.register_strategy("trend_v2",{"min_score":75})
    with pytest.raises(ValueError): repository.register_strategy("trend_v2",{"min_score":80})
