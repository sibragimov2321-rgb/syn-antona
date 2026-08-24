from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class IndicatorSnapshot:
    rsi_14: Decimal | None
    ema_9: Decimal | None
    ema_21: Decimal | None
    ema_50: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    atr_14: Decimal | None
    bollinger_upper: Decimal | None
    bollinger_lower: Decimal | None


def ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    multiplier = Decimal("2") / Decimal(period + 1)
    result = sum(values[:period]) / Decimal(period)
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def rsi(values: list[Decimal], period: int = 14) -> Decimal | None:
    if len(values) <= period:
        return None
    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(delta, Decimal("0")) for delta in deltas]
    losses = [abs(min(delta, Decimal("0"))) for delta in deltas]
    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + loss) / Decimal(period)
    if avg_loss == 0:
        return Decimal("100")
    return Decimal("100") - (Decimal("100") / (Decimal("1") + avg_gain / avg_loss))


def atr(
    highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14
) -> Decimal | None:
    if len(closes) <= period or not (len(highs) == len(lows) == len(closes)):
        return None
    ranges = [
        max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        for index in range(1, len(closes))
    ]
    result = sum(ranges[:period]) / Decimal(period)
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / Decimal(period)
    return result


def snapshot(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal]) -> IndicatorSnapshot:
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd_value = fast - slow if fast is not None and slow is not None else None
    recent = closes[-20:]
    middle = sum(recent) / Decimal(len(recent)) if len(recent) == 20 else None
    deviation = (
        (sum((price - middle) ** 2 for price in recent) / Decimal(20)).sqrt()
        if middle is not None
        else None
    )
    return IndicatorSnapshot(
        rsi_14=rsi(closes),
        ema_9=ema(closes, 9),
        ema_21=ema(closes, 21),
        ema_50=ema(closes, 50),
        macd=macd_value,
        macd_signal=None,
        atr_14=atr(highs, lows, closes),
        bollinger_upper=middle + Decimal("2") * deviation if deviation is not None else None,
        bollinger_lower=middle - Decimal("2") * deviation if deviation is not None else None,
    )
