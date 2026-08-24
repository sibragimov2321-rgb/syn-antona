import asyncio
import json

from app.exchanges.adapters import create_adapter
from app.exchanges.ccxt_transport import CcxtTransport
from app.exchanges.models import HealthStatus

EXCHANGES = ("bybit", "binance", "okx", "bitget", "kucoin", "gateio", "kraken")


async def check(exchange: str) -> dict:
    transport = None
    try:
        transport = CcxtTransport(exchange)
        adapter = create_adapter(exchange, transport)
        await adapter.connect()
        report = await adapter.health_check()
        return {"exchange": exchange, "status": report.status, "latency_ms": str(report.latency_ms), "reasons": report.reasons}
    except Exception as error:
        return {"exchange": exchange, "status": HealthStatus.UNAVAILABLE, "reasons": (str(error),)}
    finally:
        if transport is not None:
            await transport.close()


async def run() -> list[dict]:
    return await asyncio.gather(*(check(exchange) for exchange in EXCHANGES))


def main() -> None:
    print(json.dumps(asyncio.run(run()), indent=2, default=str))


if __name__ == "__main__":
    main()
