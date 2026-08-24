from datetime import UTC, datetime, timedelta

from app.shadow.engine import HOUR, PROTOCOL_ID, candle_hash, floor_hour
from app.shadow.repository import ShadowRepository
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH


async def recover_after_downtime(
    repository: ShadowRepository,
    market,
    protocol: dict,
    *,
    through: datetime | None = None,
    recovery_time: datetime | None = None,
) -> dict:
    """Persist missed OHLCV as non-tradable recovery observations.

    No quote or order-book value is synthesized. Each recovered candle and its suppressed WAIT
    decision are committed atomically, so repeated recovery calls are idempotent.
    """
    now = recovery_time or datetime.now(UTC)
    end = floor_hour(through or now)
    locked_at = datetime.fromisoformat(protocol["locked_at"]).astimezone(UTC)
    recovered = 0
    errors: list[dict] = []
    by_exchange = {exchange: 0 for exchange in protocol["exchanges"]}
    for exchange in protocol["exchanges"]:
        for symbol in protocol["assets"]:
            last = repository.last_candle_open(PROTOCOL_ID, exchange, symbol)
            start = last + HOUR if last else floor_hour(locked_at)
            if start >= end:
                continue
            try:
                candles = await market.recovery_candles(exchange, symbol, start, end)
                for candle in candles:
                    close_time = candle.timestamp + timedelta(hours=1)
                    if close_time <= locked_at or close_time > end:
                        continue
                    if repository.record_recovered_candle(
                        PROTOCOL_ID,
                        exchange,
                        symbol,
                        candle,
                        candle_hash(exchange, symbol, candle),
                        FROZEN_CONFIG_HASH,
                        now,
                    ):
                        recovered += 1
                        by_exchange[exchange] += 1
            except Exception as error:
                errors.append(
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    return {
        "recovered": recovered,
        "by_exchange": by_exchange,
        "through": end,
        "errors": errors,
        "recovered_after_downtime": True,
        "live_quote_synthesized": False,
    }
