from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path

from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.orchestrator import LOW_RISK
from app.shadow.engine import PROTOCOL_ID, floor_hour
from app.strategy_lab.phase4g import (
    CONFIRMATION_ASSETS,
    CONTROL_ASSETS,
    EXCHANGES,
    FROZEN_CONFIG_HASH,
    FROZEN_VERSION,
    canonical_json,
    verify_frozen_implementation,
)

WARMUP_CANDLES = 300
WARMUP_FETCH_BUFFER = 24
SOURCE_FILES = (
    "app/strategy_lab/phase4f.py",
    "app/strategy_lab/phase4g.py",
    "app/risk/manager.py",
    "app/trading/positions.py",
    "app/shadow/engine.py",
)


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def source_manifest(project_root: Path) -> dict:
    files = {name: _file_hash(project_root / name) for name in SOURCE_FILES}
    return {"kind": "sha256_source_manifest", "files": files, "hash": sha256(canonical_json(files)).hexdigest()}


def warmup_hash(warmups: dict) -> str:
    digest = sha256()
    for exchange, symbol in sorted(warmups):
        for candle in warmups[(exchange, symbol)]:
            digest.update(
                canonical_json(
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "timestamp": candle.timestamp,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "volume": candle.volume,
                    }
                )
            )
    return digest.hexdigest()


async def load_warmups(market, cutoff: datetime) -> dict:
    warmups = {}
    for exchange in EXCHANGES:
        for symbol in CONTROL_ASSETS + CONFIRMATION_ASSETS:
            candles = await market.warmup(
                exchange,
                symbol,
                cutoff - timedelta(hours=WARMUP_CANDLES + WARMUP_FETCH_BUFFER),
                cutoff,
            )
            candles = [candle for candle in candles if candle.timestamp < cutoff]
            candles = candles[-WARMUP_CANDLES:]
            if len(candles) != WARMUP_CANDLES:
                raise RuntimeError(
                    f"Warm-up incomplete for {exchange} {symbol}: {len(candles)}"
                )
            warmups[(exchange, symbol)] = candles
    return warmups


def build_protocol(
    project_root: Path, locked_at: datetime, cutoff: datetime, data_hash: str
) -> dict:
    manifest = source_manifest(project_root)
    return {
        "id": PROTOCOL_ID,
        "locked_at": locked_at.astimezone(UTC).isoformat(),
        "strategy_version": FROZEN_VERSION,
        "strategy_config_hash": FROZEN_CONFIG_HASH,
        "source_revision": manifest,
        "exchanges": list(EXCHANGES),
        "assets": list(CONTROL_ASSETS + CONFIRMATION_ASSETS),
        "market_type": "spot_public_data",
        "timeframe": "1h",
        "decision_policy": "only after candle_open_time + 1h <= exchange_timestamp",
        "warmup": {
            "cutoff": cutoff.astimezone(UTC).isoformat(),
            "candles_per_exchange_asset": WARMUP_CANDLES,
            "purpose": "indicator initialization only; no training or selection",
            "data_hash": data_hash,
        },
        "fee_assumptions": {
            exchange: asdict(PHASE4E_SPOT_PROFILES[exchange])
            for exchange in EXCHANGES
        },
        "slippage_model": "venue profile + frozen 0.00005 market impact per leg",
        "spread_model": "observed top-of-book bid/ask for shadow fills; frozen modeled spread remains in signal cost gate",
        "risk_settings": asdict(LOW_RISK),
        "starting_balance_per_exchange_asset": Decimal("1000"),
        "maximum_trades_per_day": None,
        "minimum_observation_days": 30,
        "correlation_policy": "exchange copies of one asset are clustered, not independent trades",
        "strategy_changes_after_lock": False,
        "private_api_keys_required": False,
        "live_trading_enabled": False,
        "real_orders_allowed": False,
    }


def create_or_verify_lock(
    path: Path,
    repository,
    project_root: Path,
    warmups: dict,
    cutoff: datetime,
) -> dict:
    verify_frozen_implementation()
    current_warmup_hash = warmup_hash(warmups)
    if path.exists():
        protocol = json.loads(path.read_text(encoding="utf-8"))
        record = repository.protocol(PROTOCOL_ID)
        if not record:
            raise RuntimeError("Protocol file exists without database lock")
        expected_hash = sha256(canonical_json(protocol)).hexdigest()
        if record.protocol_hash != expected_hash:
            raise RuntimeError("Prospective protocol file/database hash mismatch")
        if protocol["strategy_config_hash"] != FROZEN_CONFIG_HASH:
            raise RuntimeError("Prospective strategy config hash changed")
        if protocol["source_revision"] != source_manifest(project_root):
            raise RuntimeError("Trading-relevant source changed after prospective lock")
        if protocol["warmup"]["data_hash"] != current_warmup_hash:
            raise RuntimeError("Pre-lock warm-up data changed after prospective lock")
        return protocol

    locked_at = datetime.now(UTC)
    if cutoff > floor_hour(locked_at):
        raise RuntimeError("Warm-up cutoff is not pre-lock")
    protocol = build_protocol(project_root, locked_at, cutoff, current_warmup_hash)
    protocol_hash = sha256(canonical_json(protocol)).hexdigest()
    repository.create_protocol(
        {
            "id": PROTOCOL_ID,
            "locked_at": locked_at,
            "strategy_version": FROZEN_VERSION,
            "config_hash": FROZEN_CONFIG_HASH,
            "source_hash": protocol["source_revision"]["hash"],
            "warmup_hash": current_warmup_hash,
            "protocol_hash": protocol_hash,
            "protocol_json": json.dumps(protocol, default=str, sort_keys=True),
            "status": "ACTIVE",
        }
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(protocol, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)
    return protocol


def verify_existing_lock(
    path: Path,
    repository,
    project_root: Path,
    warmups: dict,
) -> dict:
    """Verify the one-time Phase 4I lock without any creation path."""

    def mismatch(reason: str) -> None:
        raise RuntimeError(f"PROTOCOL HASH MISMATCH: {reason}")

    verify_frozen_implementation()
    if not path.exists():
        mismatch("protocol lock file is missing; automatic lock creation is disabled")
    protocol = json.loads(path.read_text(encoding="utf-8"))
    record = repository.protocol(PROTOCOL_ID)
    if not record:
        mismatch("database protocol record is missing")
    if protocol.get("id") != PROTOCOL_ID:
        mismatch("protocol id changed")
    expected_hash = sha256(canonical_json(protocol)).hexdigest()
    if record.protocol_hash != expected_hash:
        mismatch("file/database protocol hash differs")
    checks = {
        "config hash": (
            protocol.get("strategy_config_hash"),
            record.config_hash,
            FROZEN_CONFIG_HASH,
        ),
        "strategy version": (
            protocol.get("strategy_version"),
            record.strategy_version,
            FROZEN_VERSION,
        ),
        "source hash": (
            protocol.get("source_revision", {}).get("hash"),
            record.source_hash,
            source_manifest(project_root)["hash"],
        ),
        "warm-up hash": (
            protocol.get("warmup", {}).get("data_hash"),
            record.warmup_hash,
            warmup_hash(warmups),
        ),
    }
    for label, values in checks.items():
        if len(set(values)) != 1:
            mismatch(f"{label} differs")
    locked_at = datetime.fromisoformat(protocol["locked_at"])
    database_locked_at = record.locked_at
    if database_locked_at.tzinfo is None:
        database_locked_at = database_locked_at.replace(tzinfo=UTC)
    if locked_at.astimezone(UTC) != database_locked_at.astimezone(UTC):
        mismatch("prospective start timestamp differs")
    return protocol
