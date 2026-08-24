# Multi-exchange architecture

The strategy, signal, position sizing and risk logic are exchange-independent. `ExchangeAdapter` is
the only boundary used for venue market data and execution. Production execution is intentionally
blocked: mutating adapter methods accept only an explicitly configured sandbox transport.

## Adapter capability declaration

| Exchange | Spot | Futures | Perpetual | Short | Leverage | Hedge | Funding | OI | SL | TP | Trailing | WS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Bybit | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| Binance | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| OKX | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| Bitget | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| KuCoin | yes | yes | yes | yes | yes | no | yes | yes | yes | yes | no | yes |
| Gate.io | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | no | yes |
| Kraken | yes | yes | yes | yes | yes | no | yes | yes | yes | yes | no | yes |

The matrix is deliberately conservative. Capabilities describe adapter policy, not proof of account-
specific availability. Region, account tier, instrument and API version can narrow it. Every private
operation, native conditional order and WebSocket reconciliation path still requires exchange
testnet/sandbox verification before it can be approved for production.

## Safety and account isolation

- API key, secret and optional passphrase are encrypted independently with Fernet.
- API keys are masked in account views; decrypted credentials are only returned by an internal,
  owner-scoped method.
- Withdrawal permission produces a warning and blocks autotrading.
- Production transports cannot call create/cancel/close or protective-order methods.
- Positions are keyed by `(exchange, account_id, position_id)` and retain `strategy_version`.
- An unavailable exchange is excluded for new orders; existing positions are never migrated.
- Per-exchange limits are evaluated before global exposure, leverage, open-risk, daily-loss and
  correlated-major limits.

## Connecting a new exchange

1. Subclass `UnifiedExchangeAdapter` and declare an honest `ExchangeCapabilities` and fee schedule.
2. Implement symbol formatting only in that adapter; the internal format remains `BTC/USDT`.
3. Use `CcxtTransport` or supply another transport implementing `connect()` and
   `call(operation, **parameters)`.
4. Register the class in `ADAPTER_TYPES`.
5. Add symbol, precision, capability, rate-limit, health, permission and sandbox order tests.
6. Verify public data and every private operation on the exchange testnet. Do not enable a
   production transport until reconciliation and emergency-stop tests pass.

## Exchange comparison backtests

`ExchangeComparisonBacktest` accepts independent candle datasets and cost profiles per exchange,
then runs the same immutable strategy factory on each. It reports PnL, fees, slippage, profit factor,
drawdown and trade count without mixing market data between venues.
