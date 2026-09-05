# Research: Local Position Profit Protector

## Decision: Public Bybit linear WebSocket supplies live price and closed-candle updates

**Rationale**: The public linear endpoint supports `tickers.{symbol}` and `kline.5.{symbol}` topics without credentials. Ticker updates provide current bid/ask/mark data; the kline payload has an explicit confirmation flag, allowing momentum and ATR to use only closed intervals. A small REST warm-up is performed only for an already-open position after startup/reconnect.

**Alternatives considered**: Frequent REST ticker polling would increase rate-limit pressure and provide poorer continuity. A private WebSocket would require credential signing and position-event recovery even though authenticated position reads already exist.

## Decision: Preserve TP and send SL+TP together for every protection update

**Rationale**: Bybit full-position trading-stop can modify current protection. Sending both sides preserves the existing target and avoids the documented loss of TP/SL binding that can occur after a one-sided modification. The old protection is never explicitly cancelled.

**Alternatives considered**: Sending only `stopLoss` is smaller but can alter protection binding. Cancelling and recreating conditional orders introduces an unacceptable unprotected window.

## Decision: Monetary net R is the only threshold coordinate

**Rationale**: Initial R includes price risk plus conservative round-trip costs. Current net PnL is independently calculated from side, entry, quantity, live executable price, actual known entry fees, and conservative estimated exit costs. This avoids leverage-distorted ROI percentages.

**Alternatives considered**: Exchange ROI% and raw price movement ignore cost and leverage effects. Gross PnL/R can activate protection before the position is actually profitable after costs.

## Decision: Conservative deterministic trailing and reversal definitions

**Rationale**: After +1R, the trailing distance is `ATR14 × adaptive multiplier`, with the multiplier bounded from 1.5 to 2.0 as ATR/price rises. The candidate is also bounded by the +0.3R profit-lock stop and the last confirmed stop. Early exit requires: prior maximum net PnL at or above +1R, at least 0.5R retracement, three adverse closed five-minute closes, latest close beyond EMA9 in the adverse direction, and latest adverse move at least 0.5 ATR.

**Alternatives considered**: A single adverse tick is too noisy. An unbounded volatility multiplier can loosen the stop. AI-based interpretation violates the no-extra-calls requirement.

## Decision: Only bot-owned and reconciled positions are mutable

**Rationale**: The monitor resolves an open position to a durable protected proposal and execution-ledger row. Unknown manual/external positions are observed but never modified. Risk-reducing actions may operate after the ten-minute entry approval expires, but only with the exact original client ID, symbol, quantity bound, fill-average match, position ID, profile hash, protected ledger state, current exchange position, API permissions, and normal live gates. Bybit `createdTime` is deliberately not an ownership key because the exchange preserves it across later position lifecycles on the same symbol/position index.

**Alternatives considered**: Managing every account position risks changing a manual trade. Reusing the entry approval TTL would disable profit protection on normal holding periods.

## Decision: Persist state plus mutation events

**Rationale**: Per-entry state preserves initial values, MFE, current net PnL, and tightest confirmed stop across restart. A separate unique event record prevents duplicate stop updates or early-close submissions and records pending/confirmed/rejected/unknown outcomes.

**Alternatives considered**: In-memory state loses MFE on deploy. Encoding protector state into existing execution rows would overload verified ledger semantics and require destructive schema changes.

## Primary references

- Bybit V5 WebSocket connection and public linear endpoint documentation.
- Bybit V5 public ticker stream documentation.
- Bybit V5 kline stream documentation (`confirm=true` means closed).
- Bybit V5 Set Trading Stop documentation and full-position modification behavior.
