import pytest
from decimal import Decimal

from app.ai.models import AIResult, RiskLevel, RoleResult, Trend
from app.ai.service import AIAnalyst, AIConsensusEngine, AIUnavailable, MockAIProvider
from app.core.config import Settings
from app.domain.models import Decision, Signal
from app.signals.engine import MarketFrame
from app.ai.context import MarketContextBuilder
from app.market.indicators import IndicatorSnapshot

def context():
    i=IndicatorSnapshot(Decimal("60"),Decimal("110"),Decimal("105"),Decimal("100"),Decimal("2"),Decimal("1"),Decimal("1"),Decimal("105"),Decimal("95"))
    frames={name:MarketFrame(name,Decimal("100"),i) for name in ("4H","1H","15M","5M")}
    signal=Signal("BTCUSDT","5M",Decision.LONG,80,80,80,80,("trend",),Decimal("100"),Decimal("98"),Decimal("104"),Decimal("2"))
    return MarketContextBuilder().build("BTCUSDT",frames,signal,0),signal
def valid(direction="LONG"):
    return {"decision":direction,"confidence":80,"trend_score":80,"momentum_score":80,"volatility_score":70,"setup_quality":80,"suggested_entry":"100","suggested_stop_loss":"98","suggested_take_profit":"104","reasons":["ok"],"risk_flags":[]}
@pytest.mark.asyncio
async def test_valid_ai_and_cache():
    c,_=context(); p=MockAIProvider(valid()); a=AIAnalyst(p,Settings())
    assert (await a.analyze(c)).decision is Decision.LONG; await a.analyze(c); assert p.calls==1
@pytest.mark.asyncio
async def test_invalid_prices_and_unavailable_are_rejected():
    c,_=context(); bad=valid(); bad["suggested_stop_loss"]="102"
    with pytest.raises(ValueError): await AIAnalyst(MockAIProvider(bad),Settings()).analyze(c)
    assert await AIAnalyst(MockAIProvider(AIUnavailable()),Settings(ai_required=True)).analyze(c) is None
def test_consensus_rejects_extreme_and_disagreement():
    _,tech=context(); result=AIResult.model_validate(valid())
    engine=AIConsensusEngine(); extreme=RoleResult(direction=Trend.NEUTRAL,score=0,risk=RiskLevel.EXTREME)
    assert engine.aggregate(tech,RoleResult(direction=Trend.BULLISH,score=80),RoleResult(direction=Trend.BULLISH,score=80),extreme,result).decision is Decision.WAIT
    assert engine.aggregate(tech,RoleResult(direction=Trend.BEARISH,score=80),RoleResult(direction=Trend.BEARISH,score=80),RoleResult(direction=Trend.NEUTRAL,score=80,risk=RiskLevel.LOW),result).decision is Decision.WAIT
