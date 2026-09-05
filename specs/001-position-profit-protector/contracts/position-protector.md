# Contract: Local Position Profit Protector

## Inputs

- Fresh public ticker observation: symbol, bid, ask, mark/last price, exchange timestamp.
- Confirmed closed five-minute interval: start/end time, open, high, low, close.
- Authenticated live position: symbol, position index, side, size, average entry, stop loss, take profit.
- Durable ownership: protected proposal plus `FILLED_PROTECTED` execution-ledger row.
- Account fee snapshot or fresh persisted fee cache.

## Output actions

### No action

Returned when data is stale/incomplete, ownership cannot be reconciled, thresholds are not met, a candidate would loosen protection, or an unknown event awaits reconciliation.

### Native protection update

Contains symbol, exact tighter stop, unchanged take profit, position index, quantity bound, entry client order ID, and action label. It must pass the gateway as a risk-reducing request and be read back from Bybit before confirmation.

### Early profit exit

Contains symbol, full current quantity, deterministic derived close ID, and the owning entry client order ID. It must be reduce-only, cannot increase or reverse the position, and must reconcile before confirmation.

## Notification contract

- Break-even confirmation: `PROFIT PROTECTED` and `BREAK EVEN ACTIVATED`.
- +1R profit-lock confirmation: `PROFIT PROTECTED`.
- Subsequent tighter trailing confirmation: `TRAILING UPDATED`.
- Reconciled early exit: `EARLY PROFIT EXIT`.
- No notification for routine observations, unchanged candidates, stale inputs, or unconfirmed mutations.

## Failure contract

- Known reject: persist `REJECTED`; keep current exchange protection; later fresh observations may calculate a new event.
- Timeout/ambiguous result: persist `UNKNOWN`; do not resubmit; reconcile live state before any further mutation.
- Verification mismatch: persist `UNKNOWN`; do not claim protection.
- Connection loss: reconnect with bounded backoff; do not act on cached stale prices.

