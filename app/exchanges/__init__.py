"""Unified multi-exchange contracts. Production order execution remains disabled."""

from app.exchanges.adapters import (
    BinanceAdapter,
    BitgetAdapter,
    BybitAdapter,
    GateIOAdapter,
    KrakenAdapter,
    KuCoinAdapter,
    OKXAdapter,
    create_adapter,
)
from app.exchanges.base import ExchangeAdapter
from app.exchanges.ccxt_transport import CcxtTransport, create_ccxt_transport

__all__ = [
    "ExchangeAdapter",
    "BybitAdapter",
    "BinanceAdapter",
    "OKXAdapter",
    "BitgetAdapter",
    "KuCoinAdapter",
    "GateIOAdapter",
    "KrakenAdapter",
    "create_adapter",
    "CcxtTransport",
    "create_ccxt_transport",
]
