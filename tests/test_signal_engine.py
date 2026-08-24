from decimal import Decimal

from app.domain.models import Decision
from app.market.indicators import IndicatorSnapshot
from app.signals.engine import MarketFrame, SignalEngine


def frame(direction: str, timeframe: str = "5M") -> MarketFrame:
    if direction == "long":
        values = (Decimal("110"), Decimal("105"), Decimal("100"), Decimal("60"), Decimal("2"))
    elif direction == "short":
        values = (Decimal("90"), Decimal("95"), Decimal("100"), Decimal("40"), Decimal("-2"))
    else:
        values = (Decimal("100"), Decimal("100"), Decimal("100"), Decimal("50"), Decimal("0"))
    return MarketFrame(
        timeframe, Decimal("100"), IndicatorSnapshot(
            rsi_14=values[3], ema_9=values[0], ema_21=values[1], ema_50=values[2],
            macd=values[4], macd_signal=Decimal("0"), atr_14=Decimal("1"),
            bollinger_upper=Decimal("105"), bollinger_lower=Decimal("95"),
        ),
    )


def test_long_signal_is_actionable() -> None:
    signal = SignalEngine().analyze("BTCUSDT", frame("long"))
    assert signal.decision is Decision.LONG
    assert signal.signal_score >= 70
    assert signal.risk_reward_ratio == Decimal("2")


def test_short_signal_is_actionable() -> None:
    signal = SignalEngine().analyze("BTCUSDT", frame("short"))
    assert signal.decision is Decision.SHORT
    assert signal.proposed_stop_loss > signal.proposed_entry


def test_conflicting_timeframes_return_wait() -> None:
    frames = {timeframe: frame("long", timeframe) for timeframe in SignalEngine.required_timeframes}
    frames["4H"] = frame("short", "4H")
    assert SignalEngine().confirm_multi_timeframe("BTCUSDT", frames).decision is Decision.WAIT
