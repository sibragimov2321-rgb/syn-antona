"""Strict read-only Phase 5F arming preflight.

This command performs GET requests and local/database reads only.  It never calls
``sign_post``, ``post_signed``, or a gateway mutation method.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import (
    ControlledLiveProposalRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
    MultiSymbolScannerStateRecord,
    SessionLocal,
)
from app.exchanges.bybit_readonly import build_report as build_bybit_report
from app.exchanges.bybit_v5_gateway import ALLOWED_SYMBOLS, MAX_NOTIONAL, MUTATING_PATHS
from app.trading.controlled_live import CONTROLLED_LIVE_V1
from app.trading.controlled_universe import PROFILE_NAME, SCANNER_CONFIG
from app.trading.multi_symbol_scanner import BybitMultiSymbolReadOnlyReader


async def _telegram_check() -> dict[str, str]:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.admin_telegram_ids:
        return {
            "token": "SET" if settings.telegram_bot_token else "NOT SET",
            "admin_ids": "SET" if settings.admin_telegram_ids else "NOT SET",
            "authorization": "FAIL",
        }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
            )
            payload = response.json()
        passed = response.is_success and payload.get("ok") is True
    except (httpx.HTTPError, ValueError):
        passed = False
    return {
        "token": "SET",
        "admin_ids": "SET",
        "authorization": "PASS" if passed else "FAIL",
    }


def _database_checks() -> dict:
    with SessionLocal() as session:
        controlled = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
        scanner = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
        execution_rows = int(
            session.scalar(select(func.count()).select_from(ExecutionOrderRecord)) or 0
        )
        proposals = int(
            session.scalar(select(func.count()).select_from(ControlledLiveProposalRecord))
            or 0
        )
    return {
        "kill_switch": (
            "SAFE" if controlled is not None and not controlled.kill_switch_active else "UNSAFE"
        ),
        "controlled_profile_hash": (
            "MATCH"
            if controlled is not None
            and controlled.profile_hash == CONTROLLED_LIVE_V1.config_hash
            else "MISMATCH"
        ),
        "scanner_hash": (
            "MATCH"
            if scanner is not None and scanner.config_hash == SCANNER_CONFIG.config_hash
            else "MISMATCH"
        ),
        "scanner_status": scanner.status if scanner is not None else "MISSING",
        "execution_ledger_rows": execution_rows,
        "controlled_proposals": proposals,
    }


async def build_report() -> dict:
    settings = get_settings()
    gates = {
        "DRY_RUN": settings.dry_run,
        "LIVE_TRADING_ENABLED": settings.live_trading_enabled,
        "CONTROLLED_LIVE_ENABLED": settings.controlled_live_enabled,
        "MANUAL_FIRST_ORDER_APPROVED": settings.manual_first_order_approved,
    }
    bybit = await build_bybit_report()
    telegram = await _telegram_check()
    database = _database_checks()
    scanner_reader = BybitMultiSymbolReadOnlyReader.from_environment()
    try:
        scanner = await scanner_reader.read()
    finally:
        await scanner_reader.close()
    instruments = {
        symbol: {
            "enabled": item.enabled,
            "reason": item.exclusion_reason or "PASS",
            "status": item.status,
            "contract_type": item.contract_type,
            "minimum_quantity": str(item.minimum_quantity),
            "quantity_step": str(item.quantity_step),
            "minimum_notional": str(item.minimum_notional),
            "actual_minimum_notional": str(item.actual_minimum_notional),
            "spread_pct": str(item.spread_pct),
            "turnover_24h": str(item.turnover_24h),
        }
        for symbol, item in scanner.instruments.items()
    }
    permissions = bybit.get("permissions") or {}
    balance = bybit.get("balance") or {}
    balance_ok = Decimal(str(balance.get("usdt_wallet_balance") or "0")) >= Decimal("50")
    reconciliation = (bybit.get("reconciliation") or {}).get("status") == "MATCH"
    gateway_ready = (
        frozenset(ALLOWED_SYMBOLS) == frozenset(SCANNER_CONFIG.symbols)
        and MAX_NOTIONAL == Decimal("10")
        and MUTATING_PATHS
        == frozenset(
            {
                "/v5/order/create",
                "/v5/order/cancel",
                "/v5/position/set-leverage",
                "/v5/position/trading-stop",
            }
        )
        and settings.dry_run
        and not settings.live_trading_enabled
        and not settings.controlled_live_enabled
        and not settings.manual_first_order_approved
    )
    checks = {
        "bybit_auth": bybit.get("private_api_auth") == "PASS",
        "read_permission": permissions.get("read") == "YES",
        "trade_permission": permissions.get("trade") == "YES",
        "withdraw_disabled": permissions.get("withdraw") == "NO",
        "transfer_disabled": permissions.get("transfer") == "NO",
        "balance_at_least_50_usdt": balance_ok,
        "positions_zero": bybit.get("open_positions_count") == 0,
        "open_orders_zero": bybit.get("open_orders_count") == 0,
        "fills_read": bybit.get("fills_read") == "PASS",
        "reconciliation_match": reconciliation,
        "telegram_admin": telegram["authorization"] == "PASS",
        "kill_switch_safe": database["kill_switch"] == "SAFE",
        "controlled_profile_hash": database["controlled_profile_hash"] == "MATCH",
        "scanner_hash": database["scanner_hash"] == "MATCH",
        "production_gateway_ready": gateway_ready,
        "arming_gates_closed": (
            settings.dry_run
            and not settings.live_trading_enabled
            and not settings.controlled_live_enabled
            and not settings.manual_first_order_approved
        ),
    }
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "gates": gates,
        "account_type": (bybit.get("account") or {}).get("account_type", "UNKNOWN"),
        "balance": balance,
        "permissions": permissions,
        "open_positions": bybit.get("open_positions_count"),
        "open_orders": bybit.get("open_orders_count"),
        "reconciliation": bybit.get("reconciliation"),
        "telegram": telegram,
        "database": database,
        "scanner_config_hash": SCANNER_CONFIG.config_hash,
        "instruments": instruments,
        "enabled_symbols": sorted(
            symbol for symbol, item in scanner.instruments.items() if item.enabled
        ),
        "excluded_symbols": {
            symbol: item.exclusion_reason
            for symbol, item in scanner.instruments.items()
            if not item.enabled
        },
        "real_orders_sent_by_preflight": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Phase 5F controlled-live preflight")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    before = _database_checks()["execution_ledger_rows"]
    report = asyncio.run(build_report())
    after = _database_checks()["execution_ledger_rows"]
    report["execution_ledger_unchanged"] = before == after
    if not report["execution_ledger_unchanged"]:
        report["status"] = "FAIL"
    encoded = json.dumps(report, indent=2, sort_keys=True, default=str)
    if arguments.output:
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, flush=True)
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
