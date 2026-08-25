# Pre-launch execution audit

Run the deterministic critical gate with:

```powershell
$env:LIVE_TRADING_ENABLED="false"
python -m app.preflight
```

The gate checks independent LONG/SHORT arithmetic, entry/exit fees, adverse slippage, position
sizing, hard portfolio risk, instrument precision, SL/TP ambiguity, closed-candle alignment,
warm-up, stale data, durable client-order idempotency, timeout handling, restart duplicate
protection, persistence, secret masking and AI failure behavior.

Passing this command does **not** authorize live trading. A real Bybit account is intentionally not
configured. Before testnet, the operator must provide a testnet-only key through protected runtime
variables, confirm Read + Trade and no Withdraw permission, identify the exact unified
account/subaccount, configure a fixed symbol allowlist, and execute the manual exchange
reconciliation checklist. Production credentials must never be used for the first execution test.

After any private-order timeout, the durable ledger status is `UNKNOWN`. The application must not
retry that client order id. An operator or reconciliation service must first query Bybit positions,
open orders and order history. Reusing an id with a different payload is always rejected.
