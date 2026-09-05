"""GET-only all-market discovery with bounded, diagnostic Hermes analysis."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.live_trader import ClosedCandle, _frame_payload
from app.ai.service import OpenAICompatibleProvider
from app.db import AIMarketDiscoveryRuntimeRecord
from app.exchanges.bybit_readonly import BybitMainnetReadOnlyClient
from app.trading.controlled_universe import ALLOWED_SCANNER_SYMBOLS


RUNTIME_NAME = "HYBRID_MARKET_DISCOVERY"
LOCAL_SCAN_SECONDS = 300
HERMES_MIN_INTERVAL = timedelta(minutes=30)
TOP_CANDIDATES = 3
TECHNICAL_SHORTLIST = 20
MAX_SCAN_AGE = timedelta(seconds=45)
MIN_TURNOVER_24H = Decimal("5000000")
MAX_SPREAD_PCT = Decimal("0.002")
MAX_ACTUAL_MIN_NOTIONAL = Decimal("15")
STRONG_EVENT_MULTIPLIER = Decimal("1.20")
TIMEFRAMES = {"5m": ("5", 5), "15m": ("15", 15), "1h": ("60", 60)}


def D(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    bid: Decimal
    ask: Decimal
    spread_pct: Decimal
    turnover_24h: Decimal
    volume_24h: Decimal
    actual_minimum_notional: Decimal
    score: Decimal
    timeframes: dict[str, dict[str, Any]]

    def compact(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "spread_pct": str(self.spread_pct),
            "turnover_24h": str(self.turnover_24h),
            "volume_24h": str(self.volume_24h),
            "actual_minimum_notional": str(self.actual_minimum_notional),
            "local_score": str(self.score),
            "timeframes": self.timeframes,
        }


@dataclass(frozen=True)
class MarketDiscoverySnapshot:
    scanned_symbols: int
    eligible_symbols: int
    candidates: tuple[MarketCandidate, ...]
    scanned_at: datetime

    @property
    def signature(self) -> str:
        content = ":".join(item.symbol for item in self.candidates)
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def aggregate_score(self) -> Decimal:
        if not self.candidates:
            return Decimal()
        return sum((item.score for item in self.candidates), Decimal()) / Decimal(
            len(self.candidates)
        )


class MarketDiscoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: Literal["LONG", "SHORT", "WAIT"]
    confidence: int = Field(ge=0, le=100)
    stop_loss: float | None
    take_profit: float | None
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def levels_for_trade(self) -> MarketDiscoveryDecision:
        if self.action != "WAIT" and (
            self.stop_loss is None or self.take_profit is None
        ):
            raise ValueError("LONG/SHORT requires stop_loss and take_profit")
        return self


class MarketDiscoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[MarketDiscoveryDecision]

    @model_validator(mode="after")
    def unique_symbols(self) -> MarketDiscoveryBatch:
        symbols = [item.symbol for item in self.decisions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Duplicate discovery symbol")
        return self


class MarketDiscoveryRepository:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def initialize(self) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                session.add(
                    AIMarketDiscoveryRuntimeRecord(
                        runtime_name=RUNTIME_NAME,
                        status="RUNNING",
                        symbols_scanned=0,
                        eligible_symbols=0,
                        top_candidates_json="[]",
                        hermes_calls_today=0,
                        last_decisions_json="[]",
                        updated_at=now,
                    )
                )
            else:
                row.status = "RUNNING"
                row.last_error = None
                row.updated_at = now

    def claim_local_slot(self, slot: datetime) -> bool:
        current = _aware(slot)
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                raise RuntimeError("Market discovery runtime is missing")
            previous = row.last_local_slot_at
            if previous is not None and _aware(previous) >= current:
                return False
            row.last_local_slot_at = current
            row.updated_at = datetime.now(UTC)
            return True

    def save_local(self, snapshot: MarketDiscoverySnapshot) -> None:
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                raise RuntimeError("Market discovery runtime is missing")
            row.status = "RUNNING"
            row.last_local_scan_at = snapshot.scanned_at
            row.symbols_scanned = snapshot.scanned_symbols
            row.eligible_symbols = snapshot.eligible_symbols
            row.top_candidates_json = json.dumps(
                [item.compact() for item in snapshot.candidates],
                sort_keys=True,
                separators=(",", ":"),
            )
            row.last_error = None
            row.updated_at = datetime.now(UTC)

    def claim_hermes(self, snapshot: MarketDiscoverySnapshot) -> bool:
        if len(snapshot.candidates) != TOP_CANDIDATES:
            return False
        now = snapshot.scanned_at
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                raise RuntimeError("Market discovery runtime is missing")
            elapsed = (
                now - _aware(row.last_hermes_call_at)
                if row.last_hermes_call_at is not None
                else None
            )
            strong_event = bool(
                elapsed is not None
                and elapsed < HERMES_MIN_INTERVAL
                and row.last_candidate_signature
                and row.last_candidate_signature != snapshot.signature
                and row.last_candidate_score is not None
                and snapshot.aggregate_score
                >= Decimal(row.last_candidate_score) * STRONG_EVENT_MULTIPLIER
            )
            if elapsed is not None and elapsed < HERMES_MIN_INTERVAL and not strong_event:
                return False
            row.last_hermes_call_at = now
            row.last_candidate_signature = snapshot.signature
            row.last_candidate_score = snapshot.aggregate_score
            row.updated_at = datetime.now(UTC)
            return True

    def record_hermes_http_call(self, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                raise RuntimeError("Market discovery runtime is missing")
            if row.hermes_call_day != current.date():
                row.hermes_call_day = current.date()
                row.hermes_calls_today = 0
            row.hermes_calls_today += 1
            row.updated_at = current

    def save_decisions(self, result: MarketDiscoveryBatch) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is None:
                raise RuntimeError("Market discovery runtime is missing")
            row.last_decisions_json = result.model_dump_json()
            row.status = "RUNNING"
            row.last_error = None
            row.updated_at = now

    def fail(self, error: Exception) -> None:
        with self.session_factory.begin() as session:
            row = session.get(AIMarketDiscoveryRuntimeRecord, RUNTIME_NAME)
            if row is not None:
                row.status = "DEGRADED"
                row.last_error = f"{type(error).__name__}: {error}"[:1000]
                row.updated_at = datetime.now(UTC)


class BybitAllMarketReader:
    def __init__(self, client: BybitMainnetReadOnlyClient) -> None:
        self.client = client
        # The public Bybit API is shared with the live worker. Keep the
        # technical shortlist bounded instead of bursting 60 requests at once.
        self._public_request_slots = asyncio.Semaphore(8)

    @classmethod
    def from_environment(cls) -> BybitAllMarketReader:
        return cls(
            BybitMainnetReadOnlyClient(
                os.getenv("BYBIT_API_KEY", ""), os.getenv("BYBIT_API_SECRET", "")
            )
        )

    async def scan(self, now: datetime | None = None) -> MarketDiscoverySnapshot:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        started = datetime.now(UTC)
        await self.client.synchronize_time()
        instruments = await self._instruments()
        tickers_result = await self.client.public_get(
            "/v5/market/tickers", {"category": "linear"}
        )
        tickers = {
            str(item.get("symbol") or ""): item
            for item in tickers_result.result.get("list") or []
        }
        eligible: list[dict[str, Any]] = []
        scanned = 0
        for item in instruments:
            symbol = str(item.get("symbol") or "")
            if not symbol.endswith("USDT"):
                continue
            scanned += 1
            ticker = tickers.get(symbol)
            parsed = _eligible_market(item, ticker)
            if parsed is not None and symbol not in ALLOWED_SCANNER_SYMBOLS:
                eligible.append(parsed)
        if datetime.now(UTC) - started > MAX_SCAN_AGE:
            raise RuntimeError("All-market snapshot is stale")
        eligible.sort(key=lambda item: item["turnover"], reverse=True)
        shortlist = eligible[:TECHNICAL_SHORTLIST]
        jobs = [self._technical(item, current) for item in shortlist]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        ranked = [item for item in results if isinstance(item, MarketCandidate)]
        if datetime.now(UTC) - started > MAX_SCAN_AGE:
            raise RuntimeError("All-market technical snapshot is stale")
        ranked.sort(key=lambda item: (-item.score, item.spread_pct, -item.turnover_24h, item.symbol))
        return MarketDiscoverySnapshot(
            scanned,
            len(eligible),
            tuple(ranked[:TOP_CANDIDATES]),
            datetime.now(UTC),
        )

    async def _instruments(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = await self.client.public_get(
                "/v5/market/instruments-info", params
            )
            rows.extend(response.result.get("list") or [])
            cursor = str(response.result.get("nextPageCursor") or "")
            if not cursor:
                return rows
            if cursor in seen or len(seen) >= 10:
                raise RuntimeError("Bybit instrument pagination is incomplete")
            seen.add(cursor)

    async def _technical(
        self, item: dict[str, Any], now: datetime
    ) -> MarketCandidate | None:
        jobs = [
            self._closed_candles(item["symbol"], name, interval, minutes, now)
            for name, (interval, minutes) in TIMEFRAMES.items()
        ]
        results = await asyncio.gather(*jobs)
        if any(len(rows) < 60 for _, rows in results):
            return None
        frames = {name: _frame_payload(rows) for name, rows in results}
        score = _technical_score(item, frames)
        return MarketCandidate(
            item["symbol"],
            item["bid"],
            item["ask"],
            item["spread"],
            item["turnover"],
            item["volume"],
            item["actual_minimum_notional"],
            score,
            frames,
        )

    async def _closed_candles(
        self,
        symbol: str,
        name: str,
        interval: str,
        minutes: int,
        now: datetime,
    ) -> tuple[str, tuple[ClosedCandle, ...]]:
        async with self._public_request_slots:
            response = await self.client.public_get(
                "/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": interval,
                    "limit": 80,
                },
            )
        duration = timedelta(minutes=minutes)
        rows = []
        for raw in response.result.get("list") or []:
            opened = datetime.fromtimestamp(int(raw[0]) / 1000, tz=UTC)
            closed = opened + duration
            if closed <= now:
                rows.append(
                    ClosedCandle(
                        opened,
                        closed,
                        D(raw[1]),
                        D(raw[2]),
                        D(raw[3]),
                        D(raw[4]),
                        D(raw[5]),
                    )
                )
        rows.sort(key=lambda row: row.opened_at)
        if rows and now - rows[-1].closed_at > duration + timedelta(minutes=2):
            return name, ()
        return name, tuple(rows)

    async def close(self) -> None:
        await self.client.close()


def _eligible_market(
    instrument: dict[str, Any], ticker: dict[str, Any] | None
) -> dict[str, Any] | None:
    if (
        ticker is None
        or instrument.get("status") != "Trading"
        or instrument.get("contractType") != "LinearPerpetual"
        or instrument.get("settleCoin") != "USDT"
    ):
        return None
    lot = instrument.get("lotSizeFilter") or {}
    bid = D(ticker.get("bid1Price"))
    ask = D(ticker.get("ask1Price"))
    step = D(lot.get("qtyStep"))
    minimum_quantity = D(lot.get("minOrderQty"))
    minimum_notional = D(lot.get("minNotionalValue"))
    if bid <= 0 or ask <= 0 or ask < bid or step <= 0:
        return None
    midpoint = (bid + ask) / 2
    spread = (ask - bid) / midpoint
    turnover = D(ticker.get("turnover24h"))
    if spread > MAX_SPREAD_PCT or turnover < MIN_TURNOVER_24H:
        return None
    actual_quantity = max(minimum_quantity, _ceil_step(minimum_notional / ask, step))
    actual_notional = actual_quantity * ask
    if actual_notional > MAX_ACTUAL_MIN_NOTIONAL:
        return None
    return {
        "symbol": str(instrument["symbol"]),
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "turnover": turnover,
        "volume": D(ticker.get("volume24h")),
        "actual_minimum_notional": actual_notional,
    }


def _metric(frame: dict[str, Any], name: str) -> Decimal:
    return D(frame["indicators"].get(name))


def _technical_score(item: dict[str, Any], frames: dict[str, dict[str, Any]]) -> Decimal:
    primary = frames["5m"]
    latest = D(primary["candles"][-1][4])
    spread_component = max(Decimal(), Decimal("1") - item["spread"] / MAX_SPREAD_PCT)
    turnover_component = min(Decimal("1"), item["turnover"] / Decimal("100000000"))
    volume_component = min(Decimal("1"), _metric(primary, "relative_volume_20") / 2)
    volatility_component = min(
        Decimal("1"), _metric(primary, "realized_volatility_20") * 1000
    )
    move = abs(D(primary["indicators"]["price_moves"].get("12")))
    momentum_component = min(Decimal("1"), move * 50)
    rsi_component = min(Decimal("1"), abs(_metric(primary, "rsi_14") - 50) / 50)
    macd = _metric(primary, "macd")
    macd_signal = _metric(primary, "macd_signal")
    ema_component = (
        min(Decimal("1"), abs(macd - macd_signal) / latest * 1000)
        if latest > 0
        else Decimal()
    )
    atr_component = (
        min(Decimal("1"), _metric(primary, "atr_14") / latest * 100)
        if latest > 0
        else Decimal()
    )
    trends = [frame["trend"] for frame in frames.values()]
    trend_component = Decimal("1") if max(trends.count("BULLISH"), trends.count("BEARISH")) >= 2 else Decimal("0.2")
    score = (
        turnover_component * 10
        + spread_component * 10
        + volume_component * 10
        + volatility_component * 15
        + momentum_component * 15
        + rsi_component * 10
        + ema_component * 10
        + atr_component * 10
        + trend_component * 10
    )
    return min(Decimal("100"), score).quantize(Decimal("0.0001"))


def build_market_prompt(snapshot: MarketDiscoverySnapshot) -> str:
    return json.dumps(
        {
            "task": (
                "Return exactly one diagnostic LONG, SHORT or WAIT decision for each supplied "
                "market-discovery candidate. Use only supplied closed-candle indicators. "
                "These decisions cannot execute orders."
            ),
            "execution_allowed": False,
            "candidates": [item.compact() for item in snapshot.candidates],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class HybridMarketDiscoveryService:
    def __init__(
        self,
        repository: MarketDiscoveryRepository,
        reader: BybitAllMarketReader,
        provider: OpenAICompatibleProvider,
    ) -> None:
        self.repository = repository
        self.reader = reader
        self.provider = provider
        self.provider.request_observer = repository.record_hermes_http_call

    async def cycle(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        slot = current.replace(
            minute=(current.minute // 5) * 5, second=0, microsecond=0
        )
        if not self.repository.claim_local_slot(slot):
            return {"status": "ALREADY_SCANNED", "slot": slot.isoformat()}
        try:
            snapshot = await self.reader.scan(current)
            self.repository.save_local(snapshot)
            if not self.repository.claim_hermes(snapshot):
                return {
                    "status": "LOCAL_ONLY",
                    "symbols_scanned": snapshot.scanned_symbols,
                    "top": [item.symbol for item in snapshot.candidates],
                }
            expected = frozenset(item.symbol for item in snapshot.candidates)
            raw = await self.provider.complete_json(
                build_market_prompt(snapshot),
                MarketDiscoveryBatch,
                expected_symbols=expected,
            )
            result = MarketDiscoveryBatch.model_validate(raw)
            if {item.symbol for item in result.decisions} != set(expected):
                raise ValueError("Hermes omitted a market-discovery candidate")
            self.repository.save_decisions(result)
            return {
                "status": "HERMES_COMPLETED",
                "symbols_scanned": snapshot.scanned_symbols,
                "top": [item.symbol for item in snapshot.candidates],
            }
        except Exception as error:
            self.repository.fail(error)
            return {"status": "FAILED", "error": f"{type(error).__name__}: {error}"}

    async def run(self) -> None:
        self.repository.initialize()
        while True:
            await self.cycle()
            await asyncio.sleep(60)

    async def close(self) -> None:
        await self.reader.close()
