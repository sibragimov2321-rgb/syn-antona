# Сын Антона

This is a **DEMO/testnet-only** Telegram algorithmic trading system. It has a rules-based Signal
Engine, multi-timeframe confirmation, strict risk authorization, a virtual exchange, position
protection, Trade Journal, statistics and a Telegram dashboard. No code path can submit a live order.

## Safety boundary

- `LIVE_TRADING_ENABLED` defaults to `false` and must remain false for this phase.
- Seven exchange adapters share one contract. Mutating methods require an explicit sandbox transport;
  production order submission is blocked.
- API keys are encrypted before persistence and never returned by the API or written to logs.
- A Risk Manager must approve a paper order before it is recorded; neither Signal Engine nor Telegram
  can bypass it.
- The system does not make profit claims or use AI to execute trades.

## Run locally

1. Copy `.env.example` to `.env`, set an encryption key and (optionally) a Telegram bot token.
2. Start PostgreSQL and Redis: `docker compose up -d db redis`.
3. Install package dependencies: `python -m pip install -e ".[dev]"`.
4. Apply migrations: `python -m alembic upgrade head`.
5. Start the API with `uvicorn app.main:app --reload`.
6. Start Telegram separately: `python -m app.telegram.runner`.
7. In Telegram, send `/start`, then choose **Start DEMO**. The current safe demo runner uses an
   explicitly synthetic data feed; it opens and manages virtual positions only.
8. Run safeguards with `python -m pytest` and `python -m ruff check .`.

## Historical integration backtest

The backend command below downloads only missing public candles into the configured database,
aligns closed 4H/1H/15M candles to each 5M decision, warms all timeframes for 200 candles, and runs
the existing Signal Engine and Risk Manager. It never submits an exchange order.

```powershell
python -m app.backtest.run_integration --exchange bybit --symbol BTC/USDT --days 30 --balance 1000
```

Phase 4A stores historical candles, runs, trades, equity points, metrics, regimes, and Monte Carlo
results in PostgreSQL. `LIVE_TRADING_ENABLED=false` remains mandatory.

## Multi-exchange backend

The backend includes adapters for Bybit, Binance, OKX, Bitget, KuCoin, Gate.io and Kraken, encrypted
multi-account persistence, capability declarations, symbol/precision normalization, independent
position ownership, exchange health/rate limits, venue selection and two-level portfolio risk.
See [docs/MULTI_EXCHANGE.md](docs/MULTI_EXCHANGE.md) for the capability matrix, safety boundary and
the checklist for adding an exchange. All private operations still require testnet verification.
Run a public, read-only connectivity check with `python -m app.exchanges.run_health`.

## Strategy Lab V2 (Phase 4C)

Run the locked two-year BTC/ETH/SOL research protocol with:

```powershell
python -m app.strategy_lab.run_research --years 2 --output strategy-lab-v2-report.json
```

The lab evaluates seven independent strategy families plus an ensemble under strategy-only,
strategy-plus-AI-filter and AI-signal-plus-deterministic-risk variants. The offline AI provider is a
frozen deterministic research surrogate; it cannot place orders and does not establish a live LLM
edge. FINAL HOLDOUT is opened once after validation selection. Live trading remains disabled.

Phase 3 (AI analysis) has not been started. A production market-data feed, additional strategies and
all testnet/live work remain separate changes requiring security review and reconciliation tests.

## Phase 4E cost-aware research

Run the frozen cost-aware Mean Reversion protocol on the immutable Phase 4D boundaries with:

```powershell
$env:DATABASE_URL="sqlite:///./phase4a-real.db"
$env:LIVE_TRADING_ENABLED="false"
python -m app.strategy_lab.run_phase4e --boundaries strategy-lab-v2-audit-rerun-report.json --output phase4e-cost-aware-report.json
python -m app.strategy_lab.finalize_phase4e --source phase4e-cost-aware-report.json --output phase4e-final-report.json
```

Candidate ranking uses TRAIN only, VALIDATION is confirmation-only, and the selected FINAL HOLDOUT
batch is opened once. Maker fills require both price trade-through and a deterministic conservative
fill-probability check; an unfilled limit is recorded as NO TRADE. Exchange comparisons are cost
sensitivity runs on the same Bybit candles, not claims about performance on other venues.

## Phase 4F new-edge discovery

Phase 4F uses a pre-locked search space of nine economically documented strategy families on
BTC/USDT, ETH/USDT and SOL/USDT across 5m, 15m, 1h and 4h. It uses real Binance spot candles,
seven-day purge/embargo gaps, TRAIN-only timeframe selection, VALIDATION confirmation, three rolling
WALK_FORWARD windows, cost-aware entries, market-impact estimates and Bonferroni/deflated-Sharpe
protection. FINAL HOLDOUT is opened only after the locked walk-forward gate passes.

```powershell
$env:DATABASE_URL="sqlite:///./phase4a-real.db"
$env:LIVE_TRADING_ENABLED="false"
python -m app.strategy_lab.run_phase4f --days 730 --protocol-lock phase4f-protocol-lock.json --output phase4f-new-edge-report.json
python -m app.strategy_lab.run_phase4f_stress --protocol-lock phase4f-protocol-lock.json --research-report phase4f-new-edge-report.json --output phase4f-walk-stress-report.json
python -m app.strategy_lab.finalize_phase4f --protocol-lock phase4f-protocol-lock.json --research-report phase4f-new-edge-report.json --stress-report phase4f-walk-stress-report.json --output phase4f-final-report.json
```

The stress command re-evaluates only the already frozen validation-confirmed hypotheses on
WALK_FORWARD at 1.25x, 1.5x and 2x costs. It never opens FINAL HOLDOUT. Funding/basis research is
skipped for this spot dataset because synchronized derivatives history is not available; spot
funding is zero. These venue-specific results are not evidence for other exchanges. Live trading and
real orders remain disabled.

## Phase 4I prospective live shadow validation

Phase 4I runs the immutable `phase4g_volatility_expansion_1h_frozen_v1` strategy against new public
market data received only after a one-time protocol lock. The service uses Binance, Bybit, OKX and
Bitget public OHLCV/order books for the frozen twelve-asset universe. It has no private API or order
method. Decisions use only fully closed 1h candles; fills use observed top-of-book plus frozen
slippage/impact, exchange precision/minimums and the deterministic Risk Manager.

```powershell
$env:DATABASE_URL="sqlite:///./phase4a-real.db"
$env:LIVE_TRADING_ENABLED="false"
python -m alembic upgrade head
python -m app.shadow.runner --protocol-lock phase4i-prospective-lock.json --poll-seconds 60
```

The one-time `PHASE4I_PROSPECTIVE_LOCK` already exists. Production startup has no lock-creation path:
the lock file, database record, original UTC timestamp, source manifest, config and warm-up hash must
all match or startup exits with `PROTOCOL HASH MISMATCH`. Warm-up is never used for training or
selection. Resume reloads persisted candles and open positions, then continues after the last
committed exchange/asset timestamp. Database constraints and deterministic ids reject duplicate
candles, decisions and trades.

OHLCV missed during downtime is stored with `RECOVERED_AFTER_DOWNTIME=true` and an atomic suppressed
`WAIT`; bid, ask and spread remain null because they were not observed live. Such a candle can never
open a shadow trade. Fresh signals require a current exchange timestamp and observed order book.
Binance, Bybit, OKX and Bitget are isolated as `HEALTHY`, `DEGRADED` or `OFFLINE`, so one failed venue
does not stop the others. The supervisor restarts a crashed or heartbeat-stalled collector. Telegram
provides `👁 Shadow Trading`, `🟢 System Status`, transition-only alerts and one completed daily report.

### One-time PostgreSQL server transfer

Stop the local collector before copying its state. Never copy the lock without its database.

```powershell
New-Item -ItemType Directory -Force runtime
Copy-Item .\phase4i-prospective-lock.json .\runtime\phase4i-prospective-lock.json
Copy-Item .\phase4i-warmup.json.gz .\runtime\phase4i-warmup.json.gz
Copy-Item .\phase4a-real.db .\runtime\phase4a-real.db
Copy-Item .\.env.production.example .\.env.production
# Set a new POSTGRES_PASSWORD in .env.production; do not commit that file.

docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml run --rm shadow python -m alembic upgrade head
docker compose -f docker-compose.prod.yml run --rm shadow python -m app.shadow.transfer --source sqlite:////runtime/phase4a-real.db --protocol-lock /runtime/phase4i-prospective-lock.json --warmup-file /runtime/phase4i-warmup.json.gz --manifest /runtime/phase4i-postgres-transfer-manifest.json
Get-Content .\runtime\phase4i-postgres-transfer-manifest.json
```

The transfer is idempotent and verifies row counts plus SHA-256 fingerprints for the protocol,
candles, quotes, decisions, trades, daily snapshots and runtime-health tables. PostgreSQL uses a named
persistent volume. A missing database lock is deliberately not reconstructed from the JSON file.

### Continuous production operation

```powershell
docker compose -f docker-compose.prod.yml up -d shadow
docker compose -f docker-compose.prod.yml --profile telegram up -d telegram
docker compose -f docker-compose.prod.yml logs -f shadow
docker compose -f docker-compose.prod.yml exec shadow python -m app.shadow.watchdog --protocol-lock /runtime/phase4i-prospective-lock.json
docker compose -f docker-compose.prod.yml exec shadow python -m app.shadow.verify_deployment --protocol-lock /runtime/phase4i-prospective-lock.json --manifest /runtime/phase4i-postgres-transfer-manifest.json --require-new-candle
```

Preview the report at any time with
`python -m app.shadow.report --preview --output phase4i-preview.json`. After 30 complete calendar
days, omit `--preview` to create the immutable final report.

`LIVE_TRADING_ENABLED=false` is mandatory. Real orders and real money remain unavailable.

## Phase 5A controlled Mainnet preparation

The immutable profile [CONTROLLED_LIVE_V1](config/controlled_live_v1.json) is preparation for one
manually reviewed BTCUSDT USDT-perpetual infrastructure-validation order. Its canonical SHA-256 is
`f9aef880cc9ac20b80d6db01adf8c0dab6e6085d84fd872889611013b1e69079`. It fixes 1x leverage, one
position, four trades/day maximum, 0.5% risk, 2% daily loss, two consecutive losses, a 60-minute
cooldown, minimum 1:2 R/R, $10 position notional, a $5 first-order cap and no trailing stop.

Submission requires all three environment gates plus an exact persistent admin-approved proposal:

```text
LIVE_TRADING_ENABLED=false
CONTROLLED_LIVE_ENABLED=false
MANUAL_FIRST_ORDER_APPROVED=false
```

The Phase 5A repository stores the preview, profile hash, manual approval, durable client order ID,
exchange order/position identifiers and fail-closed first-order state. A timeout becomes `UNKNOWN`
and cannot be retried automatically. After a confirmed fill the coordinator requires exchange-native
full-position market SL/TP protection; a protection failure invokes the reduce-only emergency-close
path. Strategy and AI sources are rejected for the first order. Telegram emergency actions require
membership in `ADMIN_TELEGRAM_IDS`.

Phase 5A does not include or instantiate a production order HTTP gateway. All execution-path tests
use deterministic fakes. The three gates must remain false until a separate explicit authorization.

## Phase 5C production Bybit order gateway

The production boundary is implemented in `app.exchanges.bybit_v5_gateway`. It is restricted to
`SOLUSDT` Linear Perpetual, `0.1 SOL`, 1x leverage, one position and a $10 notional cap. Before
every private POST it revalidates the three arming gates, an unexpired persistent admin approval,
the kill switch, the immutable hashes, current instrument limits and quote, positions, daily loss,
and the key permissions. The V5 mutation allowlist contains only create/cancel order, set leverage,
and full-position native trading-stop endpoints.

`DRY_RUN=true` is the default. In this mode the exact compact JSON payload is signed and locally
validated, but no mutating HTTP request is sent. A create timeout is persisted as `UNKNOWN`; the
same deterministic `orderLinkId` cannot be resubmitted until read-only order/history/execution
reconciliation has resolved it. Risk-reducing cancellation and close are the only operations that
remain eligible after an emergency stop. No production service arms or calls its mutation paths
while the three execution gates remain false.

### Phase 5B first-instrument selection

`CONTROLLED_LIVE_V1_FIRST_SYMBOL=SOLUSDT` is frozen separately from the original Phase 5A profile
in [controlled_live_v1_first_symbol.json](config/controlled_live_v1_first_symbol.json), selection
hash `63a3b52a6aecc19202d778ba6a50885eb9f8db9707bfc9aec5defc358e08a73b`. The choice uses Bybit's
own Mainnet linear-perpetual instrument and ticker data. At selection, `0.1 SOL` was approximately
`$9.80`; SOL had the highest 24-hour turnover among the non-BTC candidates whose actual minimum
order fitted `$5–10`.

The Phase 5B supplement raises only the manual infrastructure-validation first-order cap to `$10`;
all other `CONTROLLED_LIVE_V1` risk limits remain unchanged. Immediately before any future submit,
the coordinator must re-fetch status, ask price, minimum quantity, quantity step and minimum
notional. It blocks if `0.1 SOL × ask` moves above `$10`, if the contract is not `Trading`, or if the
quantity no longer matches Bybit's current rules. This check occurs before the durable submission
claim and before any HTTP order request.

## Phase 5E first controlled-live proposal

The shadow worker persists a one-shot Phase 5E cursor before scanning decisions. Only a new
`bybit / SOL/USDT` `LONG` or `SHORT` decision produced by the immutable
`phase4g_volatility_expansion_1h_frozen_v1` hash and already approved by the deterministic Risk
Manager can become a proposal. Old decisions, WAIT, rejected risk decisions, stale exchange state,
an existing position/order, reconciliation mismatches, quantity/notional violations, risk above
0.5%, or R/R below 1:2 all remain `WAITING_FOR_SIGNAL`.

The proposal uses fresh Bybit Linear Perpetual bid/ask and instrument/account reads, is stored once
in PostgreSQL, survives restart, and is delivered to the configured Telegram admin with Russian
approve/cancel buttons. Approval only records consent. The Telegram handler has no order-gateway
call, and the production defaults send zero real orders:

```text
DRY_RUN=true
LIVE_TRADING_ENABLED=false
CONTROLLED_LIVE_ENABLED=false
MANUAL_FIRST_ORDER_APPROVED=false
```

## Phase 4G frozen cross-confirmation

Phase 4G independently validates the exact Phase 4F `VOLATILITY_EXPANSION:1h:v1` configuration.
The frozen configuration hash is
`1fc165201603485c20ebc9e4709e710fc9192fb25679f8b0dab730c8cd8301be`; the runner refuses to
continue if the underlying Phase 4F implementation changes. Binance, Bybit, OKX and Bitget use
their own real spot OHLCV and venue-specific cost profiles. The confirmation range ends before the
old Phase 4F FINAL HOLDOUT and an overlap guard prevents accessing it.

```powershell
$env:DATABASE_URL="sqlite:///./phase4a-real.db"
$env:LIVE_TRADING_ENABLED="false"
python -m app.strategy_lab.run_phase4g --phase4f-protocol phase4f-protocol-lock.json --protocol-lock phase4g-protocol-lock.json --output phase4g-confirmation-report.json
```

The report includes all exchange/asset ranking rows, normal through 2x cost stress, regime and
quarter/half-year attribution, leave-one-asset/exchange-out diagnostics, cluster-bootstrap
expectancy intervals, inherited multiple-testing correction and trade-count adequacy. Spot funding
is zero. The inherited synthetic SHORT accounting has no spot-borrow model and is explicitly marked
as a research limitation. Live trading and real orders remain disabled.
