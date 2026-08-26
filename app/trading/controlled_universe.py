"""Immutable, dependency-light identity for the Phase 5F scanner universe."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path


CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "controlled_live_multi_symbol_v1.json"
)
MULTI_SYMBOL_CONFIG_HASH = (
    "e04de86c0182ff633b218cd8a1d4503c1d4b4a63a7caa929c724d99a01413838"
)
PROFILE_NAME = "CONTROLLED_LIVE_MULTI_SYMBOL_V1"
FROZEN_SIGNAL_SOURCE = "FROZEN_STRATEGY_ADMIN_REVIEW"


@dataclass(frozen=True)
class MultiSymbolScannerConfig:
    version: str
    base_profile_hash: str
    frozen_strategy: str
    frozen_strategy_hash: str
    symbols: tuple[str, ...]
    maximum_actual_minimum_notional: Decimal
    minimum_turnover_24h: Decimal
    maximum_spread_pct: Decimal
    maximum_order_notional: Decimal
    signal_threshold: int
    leverage: Decimal
    risk_per_trade_pct: Decimal
    daily_loss_limit_pct: Decimal
    total_experiment_loss_limit: Decimal
    minimum_risk_reward: Decimal
    config_hash: str


def load_scanner_config(path: Path = CONFIG_PATH) -> MultiSymbolScannerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    config_hash = hashlib.sha256(canonical).hexdigest()
    if path == CONFIG_PATH and config_hash != MULTI_SYMBOL_CONFIG_HASH:
        raise RuntimeError("CONTROLLED_LIVE_MULTI_SYMBOL_V1 immutable hash mismatch")
    if raw["version"] != PROFILE_NAME:
        raise RuntimeError("Unexpected multi-symbol scanner version")
    return MultiSymbolScannerConfig(
        version=raw["version"],
        base_profile_hash=raw["base_profile_hash"],
        frozen_strategy=raw["frozen_strategy"],
        frozen_strategy_hash=raw["frozen_strategy_hash"],
        symbols=tuple(raw["symbols"]),
        maximum_actual_minimum_notional=Decimal(
            raw["maximum_actual_minimum_notional_usdt"]
        ),
        minimum_turnover_24h=Decimal(raw["minimum_turnover_24h_usdt"]),
        maximum_spread_pct=Decimal(raw["maximum_spread_pct"]),
        maximum_order_notional=Decimal(raw["maximum_order_notional_usdt"]),
        signal_threshold=int(raw["signal_threshold"]),
        leverage=Decimal(raw["leverage"]),
        risk_per_trade_pct=Decimal(raw["risk_per_trade_pct"]),
        daily_loss_limit_pct=Decimal(raw["daily_loss_limit_pct"]),
        total_experiment_loss_limit=Decimal(
            raw["total_experiment_loss_limit_usdt"]
        ),
        minimum_risk_reward=Decimal(raw["minimum_risk_reward"]),
        config_hash=config_hash,
    )


SCANNER_CONFIG = load_scanner_config()
ALLOWED_SCANNER_SYMBOLS = frozenset(SCANNER_CONFIG.symbols)


def scanner_selection_hash(symbol: str) -> str:
    if symbol not in ALLOWED_SCANNER_SYMBOLS:
        raise ValueError("Symbol is not in the immutable scanner allowlist")
    return hashlib.sha256(f"{SCANNER_CONFIG.config_hash}:{symbol}".encode()).hexdigest()


def internal_symbol(symbol: str) -> str:
    if symbol not in ALLOWED_SCANNER_SYMBOLS or not symbol.endswith("USDT"):
        raise ValueError("Unsupported scanner symbol")
    return f"{symbol[:-4]}/USDT"
