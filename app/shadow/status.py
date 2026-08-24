from datetime import UTC, datetime
import json

from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def system_status(
    repository: ShadowRepository, now: datetime | None = None
) -> dict:
    current = now or datetime.now(UTC)
    protocol = repository.protocol(PROTOCOL_ID)
    if not protocol:
        return {"protocol": "MISSING", "live_trading": "OFF"}
    locked_at = _utc(protocol.locked_at)
    state = repository.collector_state(PROTOCOL_ID)
    health = repository.exchange_health(PROTOCOL_ID)
    counts = repository.decision_counts(PROTOCOL_ID)
    latest = repository.latest_candle(PROTOCOL_ID)
    closed = repository.closed_trades(PROTOCOL_ID)
    return {
        "validation_day": max(1, int((current - locked_at).total_seconds() // 86400) + 1),
        "minimum_days": 30,
        "protocol": "LOCKED",
        "protocol_hash": protocol.protocol_hash,
        "strategy_hash": protocol.config_hash,
        "live_trading": "OFF",
        "exchanges": {
            name: {
                "status": record.status,
                "reason": record.reason,
                "last_quote_at": _utc(record.last_quote_at),
            }
            for name, record in health.items()
        },
        "last_1h_candle": _utc(latest.candle_close_time) if latest else None,
        "signals": counts["LONG"] + counts["SHORT"],
        "wait": counts["WAIT"],
        "long": counts["LONG"],
        "short": counts["SHORT"],
        "open_positions": len(repository.open_trades(PROTOCOL_ID)),
        "closed_positions": len(closed),
        "collector_status": state.status if state else "OFFLINE",
        "collector_started_at": _utc(state.started_at) if state else None,
        "collector_uptime_seconds": (
            max(0, int((current - _utc(state.started_at)).total_seconds()))
            if state
            else 0
        ),
        "last_heartbeat": _utc(state.heartbeat_at) if state else None,
        "last_db_write": _utc(state.last_db_write_at) if state else None,
        "restart_count": state.restart_count if state else 0,
    }


def telegram_system_status(repository: ShadowRepository) -> str:
    values = system_status(repository)
    if values["protocol"] == "MISSING":
        return "🔴 <b>Shadow System Status</b>\n\nProtocol: MISSING\nLive trading: OFF"
    exchange_lines = []
    for exchange in ("binance", "bybit", "okx", "bitget"):
        health = values["exchanges"].get(exchange, {"status": "OFFLINE"})
        exchange_lines.append(f"{exchange.title()}: {health['status']}")
    uptime = values["collector_uptime_seconds"]
    return (
        "🟢 <b>Shadow System Status</b>\n\n"
        f"Validation day: {values['validation_day']} / 30\n"
        "Protocol: LOCKED\n"
        f"Strategy hash: <code>{values['strategy_hash'][:12]}…</code>\n"
        "Live trading: OFF\n\n"
        + "\n".join(exchange_lines)
        + "\n\n"
        f"Last 1H candle: {values['last_1h_candle'] or 'NONE'}\n"
        f"Signals: {values['signals']}\n"
        f"WAIT: {values['wait']}\n"
        f"LONG: {values['long']}\n"
        f"SHORT: {values['short']}\n"
        f"Open shadow positions: {values['open_positions']}\n"
        f"Closed positions: {values['closed_positions']}\n"
        f"Collector uptime: {uptime // 3600}h {(uptime % 3600) // 60}m\n"
        f"Last DB write: {values['last_db_write'] or 'NONE'}"
    )


def snapshot_metrics(record) -> dict:
    return json.loads(record.metrics_json)
