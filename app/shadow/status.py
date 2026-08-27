from datetime import UTC, datetime
import json

from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def execution_runtime_status(state, now: datetime | None = None) -> dict:
    current = now or datetime.now(UTC)
    heartbeat = _utc(state.heartbeat_at) if state else None
    shadow_active = bool(
        state
        and state.status in {"STARTING", "RUNNING", "DEGRADED"}
        and heartbeat
        and (current - heartbeat).total_seconds() <= 300
    )
    dry_run = state.dry_run if state else None
    live = state.live_trading_enabled if state else None
    controlled = state.controlled_live_enabled if state else None
    manual = state.manual_first_order_approved if state else None
    real_execution = state.real_order_execution_enabled if state else None
    armed = bool(
        shadow_active
        and dry_run is False
        and live is True
        and controlled is True
        and manual is True
        and real_execution is True
    )
    return {
        "shadow": "ACTIVE" if shadow_active else "INACTIVE",
        "controlled_live": "ARMED" if armed else "DISARMED",
        "real_order_execution": "ENABLED" if armed else "DISABLED",
        "dry_run": dry_run,
        "live_trading_enabled": live,
        "controlled_live_enabled": controlled,
        "manual_first_order_approved": manual,
        "deployment_id": state.deployment_id if state else None,
        "replica_id": state.replica_id if state else None,
        "last_start_cause": state.last_start_cause if state else None,
    }


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
    runtime = execution_runtime_status(state, current)
    return {
        "validation_day": max(1, int((current - locked_at).total_seconds() // 86400) + 1),
        "minimum_days": 30,
        "protocol": "LOCKED",
        "protocol_hash": protocol.protocol_hash,
        "strategy_hash": protocol.config_hash,
        "live_trading": (
            "ON" if runtime["real_order_execution"] == "ENABLED" else "OFF"
        ),
        "execution_runtime": runtime,
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
        return "🔴 <b>СОСТОЯНИЕ SHADOW-СИСТЕМЫ</b>\n\nПротокол: НЕ НАЙДЕН\nРеальная торговля: ВЫКЛЮЧЕНА"
    status_labels = {
        "HEALTHY": "РАБОТАЕТ",
        "DEGRADED": "НЕСТАБИЛЬНО",
        "OFFLINE": "НЕДОСТУПНА",
    }
    exchange_lines = []
    for exchange in ("binance", "bybit", "okx", "bitget"):
        health = values["exchanges"].get(exchange, {"status": "OFFLINE"})
        exchange_lines.append(
            f"{exchange.title()}: {status_labels.get(health['status'], health['status'])}"
        )
    uptime = values["collector_uptime_seconds"]
    runtime = values["execution_runtime"]
    return (
        "🟢 <b>СОСТОЯНИЕ SHADOW-СИСТЕМЫ</b>\n\n"
        f"SHADOW: <b>{runtime['shadow']}</b>\n"
        f"CONTROLLED LIVE: <b>{runtime['controlled_live']}</b>\n"
        "REAL ORDER EXECUTION: "
        f"<b>{runtime['real_order_execution']}</b>\n\n"
        "Фактические flags execution-сервиса:\n"
        f"DRY_RUN={_flag(runtime['dry_run'])}\n"
        f"LIVE_TRADING_ENABLED={_flag(runtime['live_trading_enabled'])}\n"
        "CONTROLLED_LIVE_ENABLED="
        f"{_flag(runtime['controlled_live_enabled'])}\n\n"
        f"День проверки: {values['validation_day']} / 30\n"
        "Протокол: ЗАФИКСИРОВАН\n"
        f"Хэш стратегии: <code>{values['strategy_hash'][:12]}…</code>\n"
        f"Накопительный restart counter: {values['restart_count']}\n"
        f"Причина последнего запуска: {runtime['last_start_cause'] or 'НЕИЗВЕСТНО'}\n\n"
        + "\n".join(exchange_lines)
        + "\n\n"
        f"Последняя свеча 1ч: {values['last_1h_candle'] or 'НЕТ'}\n"
        f"Сигналы: {values['signals']}\n"
        f"ОЖИДАНИЕ: {values['wait']}\n"
        f"ПОКУПКА: {values['long']}\n"
        f"ПРОДАЖА: {values['short']}\n"
        f"Открытые shadow-позиции: {values['open_positions']}\n"
        f"Закрытые позиции: {values['closed_positions']}\n"
        f"Время работы collector: {uptime // 3600}ч {(uptime % 3600) // 60}м\n"
        f"Последняя запись в БД: {values['last_db_write'] or 'НЕТ'}"
    )


def _flag(value: bool | None) -> str:
    if value is None:
        return "UNKNOWN"
    return "true" if value else "false"


def snapshot_metrics(record) -> dict:
    return json.loads(record.metrics_json)
