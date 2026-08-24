from bisect import bisect_right, insort
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import sqrt

from app.backtest.core import Candle
from app.backtest.persistence import TIMEFRAME_SECONDS


@dataclass(frozen=True, slots=True)
class AuxiliaryMarketSeries:
    funding_rate: dict[datetime, float]
    open_interest: dict[datetime, float]


@dataclass(frozen=True, slots=True)
class LabFeature:
    timestamp: datetime
    price: float
    rsi: float
    adx: float
    atr: float
    atr_pct: float
    atr_percentile: float
    relative_volume: float
    vwap: float
    bollinger_width: float
    bollinger_z: float
    ema_20: float
    ema_50: float
    ema_200: float
    trend_strength: float
    momentum: float
    momentum_acceleration: float
    distance_from_ema: float
    volatility_regime: str
    trend_regime: str
    breakout: int
    mtf_alignment: int
    funding_rate: float | None
    open_interest: float | None
    open_interest_change: float | None

    def context(self) -> dict:
        return {
            "rsi": self.rsi,
            "adx": self.adx,
            "atr_pct": self.atr_pct,
            "atr_percentile": self.atr_percentile,
            "relative_volume": self.relative_volume,
            "vwap": self.vwap,
            "bollinger_width": self.bollinger_width,
            "bollinger_z": self.bollinger_z,
            "trend_strength": self.trend_strength,
            "momentum": self.momentum,
            "momentum_acceleration": self.momentum_acceleration,
            "distance_from_ema": self.distance_from_ema,
            "volatility_regime": self.volatility_regime,
            "trend_regime": self.trend_regime,
            "mtf_alignment": self.mtf_alignment,
            "funding_rate": self.funding_rate,
            "open_interest": self.open_interest,
            "open_interest_change": self.open_interest_change,
        }


class LabFeatureBuilder:
    """Linear-time indicator pipeline using closed-candle MTF alignment only."""

    warmup = 200

    def build(self, histories: dict[str, list[Candle]], start: datetime, end: datetime, auxiliary: AuxiliaryMarketSeries | None = None) -> dict[datetime, LabFeature]:
        base = histories["5m"]
        values = _series(base)
        higher = {timeframe: _closed_trends(candles, timeframe) for timeframe, candles in histories.items() if timeframe != "5m"}
        aux = auxiliary or AuxiliaryMarketSeries({}, {})
        funding_times = sorted(aux.funding_rate)
        interest_times = sorted(aux.open_interest)
        features: dict[datetime, LabFeature] = {}
        for index in range(self.warmup - 1, len(base)):
            candle = base[index]
            if not start <= candle.timestamp < end:
                continue
            decision_time = candle.timestamp + timedelta(seconds=TIMEFRAME_SECONDS["5m"])
            aligned = values["trend"][index]
            valid = True
            for timeframe in ("15m", "1h", "4h"):
                close_times, trends = higher[timeframe]
                aligned_index = bisect_right(close_times, decision_time) - 1
                if aligned_index < 0:
                    valid = False
                    break
                aligned += trends[aligned_index]
            if not valid:
                continue
            funding = _latest(aux.funding_rate, funding_times, decision_time)
            interest = _latest(aux.open_interest, interest_times, decision_time)
            previous_interest = _previous(aux.open_interest, interest_times, decision_time)
            interest_change = (interest / previous_interest - 1) if interest is not None and previous_interest not in (None, 0) else None
            atr = values["atr"][index]
            price = values["close"][index]
            ema_50 = values["ema50"][index]
            ema_200 = values["ema200"][index]
            atr_pct = atr / price if price else 0.0
            percentile = values["atr_percentile"][index]
            separation = (ema_50 - ema_200) / ema_200 if ema_200 else 0.0
            trend_regime = "BULL" if separation >= 0.003 else "BEAR" if separation <= -0.003 else "SIDEWAYS"
            volatility_regime = "HIGH_VOLATILITY" if percentile >= 80 else "LOW_VOLATILITY" if percentile <= 20 else "NORMAL_VOLATILITY"
            features[candle.timestamp] = LabFeature(
                candle.timestamp,
                price,
                values["rsi"][index],
                values["adx"][index],
                atr,
                atr_pct,
                percentile,
                values["relative_volume"][index],
                values["vwap"][index],
                values["bollinger_width"][index],
                values["bollinger_z"][index],
                values["ema20"][index],
                ema_50,
                ema_200,
                abs(ema_50 - ema_200) / atr if atr else 0.0,
                values["momentum"][index],
                values["momentum_acceleration"][index],
                (price - ema_50) / atr if atr else 0.0,
                volatility_regime,
                trend_regime,
                values["breakout"][index],
                aligned,
                funding,
                interest,
                interest_change,
            )
        return features


def _series(candles: list[Candle]) -> dict[str, list]:
    close = [float(candle.close) for candle in candles]
    high = [float(candle.high) for candle in candles]
    low = [float(candle.low) for candle in candles]
    volume = [float(candle.volume) for candle in candles]
    ema20, ema50, ema200 = _ema(close, 20), _ema(close, 50), _ema(close, 200)
    atr = _wilder_atr(high, low, close, 14)
    rsi = _wilder_rsi(close, 14)
    adx = _adx(high, low, close, 14)
    relative_volume = _relative_volume(volume, 20)
    vwap = _rolling_vwap(high, low, close, volume, 96)
    middle, deviation = _rolling_mean_deviation(close, 20)
    width = [(4 * deviation[index] / middle[index]) if middle[index] else 0.0 for index in range(len(close))]
    zscore = [((close[index] - middle[index]) / deviation[index]) if deviation[index] else 0.0 for index in range(len(close))]
    momentum = [(close[index] / close[index - 12] - 1) if index >= 12 and close[index - 12] else 0.0 for index in range(len(close))]
    acceleration = [momentum[index] - momentum[index - 6] if index >= 6 else 0.0 for index in range(len(close))]
    breakout = _breakouts(high, low, close, 20)
    trend = [1 if ema20[index] > ema50[index] and close[index] > ema20[index] else -1 if ema20[index] < ema50[index] and close[index] < ema20[index] else 0 for index in range(len(close))]
    return {
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "atr": atr,
        "rsi": rsi,
        "adx": adx,
        "relative_volume": relative_volume,
        "vwap": vwap,
        "bollinger_width": width,
        "bollinger_z": zscore,
        "momentum": momentum,
        "momentum_acceleration": acceleration,
        "atr_percentile": _rolling_percentile(atr, 288),
        "breakout": breakout,
        "trend": trend,
    }


def _ema(values: list[float], period: int) -> list[float]:
    output = [0.0] * len(values)
    if not values:
        return output
    multiplier = 2 / (period + 1)
    result = values[0]
    for index, value in enumerate(values):
        result = value if index == 0 else result + multiplier * (value - result)
        output[index] = result
    return output


def _wilder_atr(high: list[float], low: list[float], close: list[float], period: int) -> list[float]:
    ranges = [high[0] - low[0]] + [max(high[index] - low[index], abs(high[index] - close[index - 1]), abs(low[index] - close[index - 1])) for index in range(1, len(close))]
    return _wilder(ranges, period)


def _wilder(values: list[float], period: int) -> list[float]:
    output = [0.0] * len(values)
    if not values:
        return output
    result = values[0]
    for index, value in enumerate(values):
        result = value if index == 0 else (result * (period - 1) + value) / period
        output[index] = result
    return output


def _wilder_rsi(close: list[float], period: int) -> list[float]:
    gains = [0.0] * len(close)
    losses = [0.0] * len(close)
    for index in range(1, len(close)):
        delta = close[index] - close[index - 1]
        gains[index] = max(delta, 0.0)
        losses[index] = max(-delta, 0.0)
    average_gain, average_loss = _wilder(gains, period), _wilder(losses, period)
    return [100.0 if loss == 0 else 100 - 100 / (1 + gain / loss) for gain, loss in zip(average_gain, average_loss, strict=True)]


def _adx(high: list[float], low: list[float], close: list[float], period: int) -> list[float]:
    plus = [0.0] * len(close)
    minus = [0.0] * len(close)
    for index in range(1, len(close)):
        up = high[index] - high[index - 1]
        down = low[index - 1] - low[index]
        plus[index] = up if up > down and up > 0 else 0.0
        minus[index] = down if down > up and down > 0 else 0.0
    smoothed_plus, smoothed_minus = _wilder(plus, period), _wilder(minus, period)
    atr = _wilder_atr(high, low, close, period)
    dx = []
    for plus_value, minus_value, atr_value in zip(smoothed_plus, smoothed_minus, atr, strict=True):
        plus_di = 100 * plus_value / atr_value if atr_value else 0.0
        minus_di = 100 * minus_value / atr_value if atr_value else 0.0
        dx.append(100 * abs(plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.0)
    return _wilder(dx, period)


def _relative_volume(volume: list[float], period: int) -> list[float]:
    output = [0.0] * len(volume)
    total = 0.0
    for index, value in enumerate(volume):
        if index:
            total += volume[index - 1]
        if index > period:
            total -= volume[index - period - 1]
        count = min(index, period)
        output[index] = value / (total / count) if count and total else 0.0
    return output


def _rolling_vwap(high: list[float], low: list[float], close: list[float], volume: list[float], period: int) -> list[float]:
    output = [0.0] * len(close)
    value_sum = volume_sum = 0.0
    typical = [(h + low[index] + close[index]) / 3 for index, h in enumerate(high)]
    for index in range(len(close)):
        value_sum += typical[index] * volume[index]
        volume_sum += volume[index]
        if index >= period:
            value_sum -= typical[index - period] * volume[index - period]
            volume_sum -= volume[index - period]
        output[index] = value_sum / volume_sum if volume_sum else close[index]
    return output


def _rolling_mean_deviation(values: list[float], period: int) -> tuple[list[float], list[float]]:
    means, deviations = [0.0] * len(values), [0.0] * len(values)
    total = squares = 0.0
    for index, value in enumerate(values):
        total += value
        squares += value * value
        if index >= period:
            old = values[index - period]
            total -= old
            squares -= old * old
        count = min(index + 1, period)
        mean = total / count
        variance = max(squares / count - mean * mean, 0.0)
        means[index] = mean
        deviations[index] = sqrt(variance)
    return means, deviations


def _rolling_percentile(values: list[float], period: int) -> list[float]:
    output = [0.0] * len(values)
    window: list[float] = []
    for index, value in enumerate(values):
        insort(window, value)
        if index >= period:
            old = values[index - period]
            window.pop(bisect_right(window, old) - 1)
        output[index] = 100 * bisect_right(window, value) / len(window)
    return output


def _breakouts(high: list[float], low: list[float], close: list[float], period: int) -> list[int]:
    output = [0] * len(close)
    for index in range(period, len(close)):
        if close[index] > max(high[index - period:index]):
            output[index] = 1
        elif close[index] < min(low[index - period:index]):
            output[index] = -1
    return output


def _closed_trends(candles: list[Candle], timeframe: str) -> tuple[list[datetime], list[int]]:
    values = _series(candles)
    duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return [candle.timestamp + duration for candle in candles], values["trend"]


def _latest(values: dict[datetime, float], times: list[datetime], timestamp: datetime) -> float | None:
    index = bisect_right(times, timestamp) - 1
    return values[times[index]] if index >= 0 else None


def _previous(values: dict[datetime, float], times: list[datetime], timestamp: datetime) -> float | None:
    index = bisect_right(times, timestamp) - 2
    return values[times[index]] if index >= 0 else None
