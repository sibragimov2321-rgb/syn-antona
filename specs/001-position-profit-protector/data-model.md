# Data Model: Local Position Profit Protector

## PositionProfitState

One durable row per bot-owned entry client order.

| Field | Meaning | Validation |
|---|---|---|
| entry_client_order_id | Stable ownership/idempotency key | Unique, non-empty |
| position_key | Exchange symbol and position index | Required |
| symbol | Current allowlisted contract | Required |
| side | LONG or SHORT | Required |
| quantity | Current tracked quantity | Greater than zero while OPEN |
| entry_price | Actual average entry | Greater than zero |
| initial_stop_loss | Original stop from approved proposal | Correct side of entry |
| initial_take_profit | Original target from approved proposal | Correct side of entry |
| initial_risk_usdt | Price risk plus conservative costs | Greater than zero |
| entry_fees_usdt | Known entry execution fees | Non-negative |
| estimated_exit_cost_usdt | Conservative current exit costs | Non-negative |
| current_price | Latest fresh executable price | Greater than zero |
| current_net_pnl | Independently calculated net PnL | Signed monetary amount |
| max_favorable_price | Best observed price for the side | Monotonic by side |
| max_favorable_excursion_usdt | Best observed net PnL | Monotonically non-decreasing |
| max_favorable_r | Best observed net PnL divided by initial risk | Monotonically non-decreasing |
| confirmed_stop_loss | Tightest exchange-confirmed stop | Never loosens |
| stage | INITIAL, BREAK_EVEN, PROFIT_LOCK, TRAILING, EXIT_PENDING, CLOSED, ERROR_UNKNOWN | Controlled transition |
| opened_at / last_observed_at / updated_at / closed_at | Lifecycle timestamps | UTC |

### State transitions

`INITIAL → BREAK_EVEN → PROFIT_LOCK → TRAILING → CLOSED`

`INITIAL|BREAK_EVEN|PROFIT_LOCK|TRAILING → EXIT_PENDING → CLOSED`

Any mutation timeout may transition to `ERROR_UNKNOWN`; automatic resubmission is forbidden until reconciliation resolves it.

## PositionProtectionEvent

One durable idempotency row per proposed stop level or early close.

| Field | Meaning |
|---|---|
| event_id | Deterministic hash of entry ID, action, and target level |
| entry_client_order_id | Parent position state |
| action | BREAK_EVEN, PROFIT_LOCK, TRAILING, EARLY_EXIT |
| requested_stop_loss | Proposed stop when applicable |
| preserved_take_profit | Existing TP sent with stop update |
| status | PENDING, CONFIRMED, REJECTED, UNKNOWN |
| reason | Short deterministic explanation/error class |
| requested_at / confirmed_at / updated_at | UTC audit timestamps |

## Relationships

- A protected execution ledger row and approved proposal own exactly one current `PositionProfitState` per entry.
- A `PositionProfitState` has zero or more `PositionProtectionEvent` rows.
- Existing proposal, execution, and position records remain unchanged.

