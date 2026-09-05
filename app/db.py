from datetime import UTC, date, datetime

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(16), default="DEMO")
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Numeric(24, 10))
    quantity: Mapped[float] = mapped_column(Numeric(24, 10))
    stop_loss: Mapped[float] = mapped_column(Numeric(24, 10))
    take_profit: Mapped[float] = mapped_column(Numeric(24, 10))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    exchange: Mapped[str] = mapped_column(String(32), default="paper", index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    position_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64), default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TradeEvent(Base):
    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ExecutionOrderRecord(Base):
    """Durable idempotency ledger for every private exchange order attempt."""

    __tablename__ = "execution_orders"
    __table_args__ = (
        UniqueConstraint(
            "exchange", "account_id", "client_order_id", name="uq_execution_client_order"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ControlledLiveStateRecord(Base):
    """Persistent fail-closed state for the immutable controlled-live profile."""

    __tablename__ = "controlled_live_state"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_order_in_progress: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    first_order_executed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    automatic_execution_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    experiment_start_equity: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    starting_day_equity: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    starting_day_utc: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ControlledLiveProposalRecord(Base):
    """Admin-reviewed first-order preview; it contains no exchange credential."""

    __tablename__ = "controlled_live_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    profile_name: Mapped[str] = mapped_column(String(64), index=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PREVIEWED")
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class FirstLiveProposalStateRecord(Base):
    """Durable Phase 5E cursor and one-shot proposal link.

    ``started_at`` is deliberately persisted before any signal is considered.  It
    prevents a deployment or restart from turning an old shadow decision into a
    retrospective live proposal.
    """

    __tablename__ = "first_live_proposal_state"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_scanned_candle_open: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scanned_decision_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    source_decision_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    proposal_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    available_equity: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WAITING_FOR_SIGNAL"
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class SignalWaitRuntimeRecord(Base):
    """Latest GET-only Bybit account snapshot for the credential-free Telegram service."""

    __tablename__ = "signal_wait_runtime"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    open_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_orders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trades_today: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_realized_pnl: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    open_planned_risk: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    account_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    account_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class MultiSymbolScannerStateRecord(Base):
    """Durable prospective cursor for the immutable controlled-live scanner."""

    __tablename__ = "multi_symbol_scanner_state"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_scanned_candle_open: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="RUNNING"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class MultiSymbolScannerInstrumentRecord(Base):
    """Latest GET-only Bybit eligibility snapshot for one scanner symbol."""

    __tablename__ = "multi_symbol_scanner_instruments"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    internal_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    instrument_status: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    bid_price: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    ask_price: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    quantity_step: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    minimum_notional: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    actual_minimum_quantity: Mapped[Decimal] = mapped_column(
        Numeric(24, 10), nullable=False
    )
    actual_minimum_notional: Mapped[Decimal] = mapped_column(
        Numeric(24, 10), nullable=False
    )
    spread_pct: Mapped[Decimal] = mapped_column(Numeric(18, 12), nullable=False)
    turnover_24h: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ControlledLiveSignalRecord(Base):
    """Closed-candle frozen-strategy decision owned by Controlled Live, not Shadow."""

    __tablename__ = "controlled_live_signals"
    __table_args__ = (
        UniqueConstraint(
            "profile_name", "symbol", "candle_open_time",
            name="uq_controlled_live_signal",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_name: Mapped[str] = mapped_column(String(64), index=True)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(8))
    signal_score: Mapped[int] = mapped_column(Integer, default=0)
    decision_price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    risk_status: Mapped[str] = mapped_column(String(16), default="NOT_APPLICABLE")
    risk_reason: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class ControlledLiveRuntimeRecord(Base):
    """Heartbeat and actual flags from the dedicated Controlled Live worker."""

    __tablename__ = "controlled_live_runtime"

    profile_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    controlled_live_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    manual_first_order_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    real_order_execution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AILiveRuntimeRecord(Base):
    """Last durable heartbeat and account state for the autonomous AI scanner."""

    __tablename__ = "ai_live_runtime"

    runtime_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="DISABLED")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="NOT_CONFIGURED")
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_market_data_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    available_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_positions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    open_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_scans: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    core_hermes_call_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    core_hermes_calls_today: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AIMarketDiscoveryRuntimeRecord(Base):
    """Durable cadence, ranking and AI-call state for GET-only market discovery."""

    __tablename__ = "ai_market_discovery_runtime"

    runtime_name: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="STARTING")
    last_local_slot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_local_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    symbols_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_symbols: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_candidate_signature: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_candidate_score: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    last_hermes_call_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hermes_call_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    hermes_calls_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_decisions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AILiveScanRecord(Base):
    """One idempotent five-minute multi-symbol AI request."""

    __tablename__ = "ai_live_scans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AILiveDecisionRecord(Base):
    """Validated AI output and its deterministic execution disposition."""

    __tablename__ = "ai_live_decisions"
    __table_args__ = (
        UniqueConstraint("scan_id", "symbol", name="uq_ai_live_scan_symbol"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WAIT"
    )
    proposal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class BybitFeeRateCacheRecord(Base):
    """Latest account-specific Bybit fee rate used by the live entry gates."""

    __tablename__ = "bybit_fee_rate_cache"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    maker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    taker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class HistoricalCandleRecord(Base):
    __tablename__ = "historical_candles"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "timeframe", "timestamp", name="uq_candle_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(32, 10))


class BacktestRunRecord(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(8), default="5m")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    starting_balance: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    final_equity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    risk_profile: Mapped[str] = mapped_column(String(20))
    validation_status: Mapped[str] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(64), default="baseline_v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class BacktestTradeRecord(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    exit: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    fees: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    slippage_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    funding: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    spread_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    signal_score: Mapped[int] = mapped_column(Integer, default=0)
    regime: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    reason: Mapped[str] = mapped_column(String(32))
    context_json: Mapped[str] = mapped_column(Text, default="{}")


class BacktestEquityRecord(Base):
    __tablename__ = "backtest_equity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    equity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    drawdown: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10))


class BacktestMetricRecord(Base):
    __tablename__ = "backtest_metrics"
    __table_args__ = (UniqueConstraint("run_id", "section", "name", name="uq_metric_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(32), default="overall")
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)


class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    regime: Mapped[str] = mapped_column(String(32))


class MonteCarloRecord(Base):
    __tablename__ = "monte_carlo_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), unique=True)
    simulations: Mapped[int] = mapped_column(Integer)
    median_final_equity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    worst_5pct: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    best_5pct: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    expected_max_drawdown: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    probability_dd_10: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    probability_dd_20: Mapped[Decimal] = mapped_column(Numeric(12, 8))


class StrategyVersionRecord(Base):
    __tablename__ = "strategy_versions"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_json: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class StrategyExperimentRecord(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_version: Mapped[str] = mapped_column(ForeignKey("strategy_versions.version"))
    symbol: Mapped[str] = mapped_column(String(20))
    data_split: Mapped[str] = mapped_column(String(32))
    cost_scenario: Mapped[str] = mapped_column(String(32))
    metrics_json: Mapped[str] = mapped_column(Text)
    selected: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ExchangeAccountRecord(Base):
    __tablename__ = "exchange_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "exchange", "account_name", name="uq_exchange_account_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    account_name: Mapped[str] = mapped_column(String(80))
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    encrypted_secret: Mapped[str] = mapped_column(Text)
    encrypted_passphrase: Mapped[str | None] = mapped_column(Text, nullable=True)
    permissions_json: Mapped[str] = mapped_column(Text, default="{}")
    account_status: Mapped[str] = mapped_column(String(24), default="DISCONNECTED")
    sandbox: Mapped[int] = mapped_column(Integer, default=1)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ExchangeHealthRecord(Base):
    __tablename__ = "exchange_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    latency_ms: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ProspectiveProtocolRecord(Base):
    __tablename__ = "prospective_protocols"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    warmup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    protocol_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ShadowCandleRecord(Base):
    __tablename__ = "shadow_candles"
    __table_args__ = (
        UniqueConstraint(
            "protocol_id", "exchange", "symbol", "candle_open_time",
            name="uq_shadow_candle",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("prospective_protocols.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    candle_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(32, 10))
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recovered_after_downtime: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShadowQuoteRecord(Base):
    __tablename__ = "shadow_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("prospective_protocols.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    bid: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    ask: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    last: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    spread: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    spread_pct: Mapped[Decimal] = mapped_column(Numeric(18, 12))
    orderbook_json: Mapped[str] = mapped_column(Text, default="{}")
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ShadowDecisionRecord(Base):
    __tablename__ = "shadow_decisions"
    __table_args__ = (
        UniqueConstraint(
            "protocol_id", "exchange", "symbol", "candle_open_time",
            name="uq_shadow_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("prospective_protocols.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(8))
    signal_score: Mapped[int] = mapped_column(Integer, default=0)
    decision_price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    observed_bid: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    observed_ask: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    observed_spread: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    risk_status: Mapped[str] = mapped_column(String(16), default="NOT_APPLICABLE")
    risk_reason: Mapped[str] = mapped_column(Text, default="")
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ShadowTradeRecord(Base):
    __tablename__ = "shadow_trades"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("shadow_decisions.id"), unique=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("prospective_protocols.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(8))
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision_price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_reference: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    take_profit: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    leverage: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("1"))
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    expected_fees: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    observed_spread: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_spread_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_slippage_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_reference: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_fee: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    exit_spread_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    exit_slippage_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=Decimal())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShadowDailySnapshotRecord(Base):
    __tablename__ = "shadow_daily_snapshots"
    __table_args__ = (
        UniqueConstraint("protocol_id", "snapshot_date", name="uq_shadow_daily_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("prospective_protocols.id"), index=True)
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    report_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShadowCollectorStateRecord(Base):
    __tablename__ = "shadow_collector_state"

    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("prospective_protocols.id"), primary_key=True
    )
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_db_write_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    dry_run: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    live_trading_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    controlled_live_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    manual_first_order_approved: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    real_order_execution_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replica_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_start_cause: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowExchangeHealthRecord(Base):
    __tablename__ = "shadow_exchange_health"
    __table_args__ = (
        UniqueConstraint(
            "protocol_id", "exchange", name="uq_shadow_exchange_health"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("prospective_protocols.id"), index=True
    )
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_quote_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowSystemEventRecord(Base):
    __tablename__ = "shadow_system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("prospective_protocols.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    exchange: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    alerted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PositionProfitStateRecord(Base):
    """Durable high-water mark and confirmed native protection per real entry."""

    __tablename__ = "position_profit_states"

    entry_client_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    position_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    initial_stop_loss: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    initial_take_profit: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    initial_risk_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    entry_fee_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    estimated_exit_cost_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    current_net_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    max_favorable_price: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    max_favorable_excursion_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 10), nullable=False
    )
    max_favorable_r: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    confirmed_stop_loss: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="INITIAL")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class PositionProtectionEventRecord(Base):
    """Idempotent audit row for each requested and confirmed risk reduction."""

    __tablename__ = "position_protection_events"
    __table_args__ = (
        UniqueConstraint(
            "entry_client_order_id", "action", "requested_stop_loss",
            name="uq_position_protection_action_stop",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_client_order_id: Mapped[str] = mapped_column(
        ForeignKey("position_profit_states.entry_client_order_id"), index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_stop_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    preserved_take_profit: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
