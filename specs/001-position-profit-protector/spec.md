# Feature Specification: Local Position Profit Protector

**Feature Branch**: `position-profit-protector`

**Created**: 2026-09-05

**Status**: Approved for implementation

**Input**: Protect already-open real positions locally without changing entries, initial protection, strategy, symbols, risk settings, or using additional AI calls.

## User Scenarios & Testing

### User Story 1 - Preserve earned profit (Priority: P1)

As the operator, I want a profitable real position to receive progressively tighter native protection so that a meaningful favorable move cannot turn back into the full originally planned loss.

**Why this priority**: Preventing a profitable position from reverting to a full loss is the requested safety outcome.

**Independent Test**: Feed deterministic LONG and SHORT price paths with known initial risk and costs, then verify protection activates at +0.5R and +1R and never loosens.

**Acceptance Scenarios**:

1. **Given** a protected real position below +0.5R, **When** price updates arrive, **Then** its original stop and take profit remain unchanged.
2. **Given** a position reaches +0.5R after costs, **When** the protector evaluates it, **Then** its native stop moves to a break-even level covering estimated round-trip costs.
3. **Given** a position reaches +1R after costs, **When** the protector evaluates it, **Then** its native stop protects at least +0.3R and adaptive trailing becomes active.
4. **Given** a tightened stop, **When** price later retraces or volatility increases, **Then** the stop never moves away from profit.

---

### User Story 2 - React locally to momentum reversal (Priority: P2)

As the operator, I want a position that achieved substantial profit to close early on a sharp confirmed momentum reversal instead of waiting for its initial target or surrendering most of the move.

**Why this priority**: Early exit is useful only after monotonic stop protection is reliable.

**Independent Test**: Feed a profitable deterministic price series followed by a sharp reversal and verify exactly one reduce-only close is requested without any AI call.

**Acceptance Scenarios**:

1. **Given** a position has not reached +1R, **When** short-term momentum reverses, **Then** no early exit occurs.
2. **Given** a position has reached at least +1R and reversal evidence is confirmed, **When** the protector evaluates closed market intervals, **Then** it may issue one idempotent reduce-only close.
3. **Given** the close outcome is unknown or rejected, **When** another market update arrives, **Then** the protector does not blindly resubmit and reports failure for reconciliation.

---

### User Story 3 - Observe protection events (Priority: P3)

As the administrator, I want Telegram notifications for meaningful protection changes so I can understand how an open real position is being managed without receiving routine tick spam.

**Why this priority**: Operational visibility is required after the protection behavior is safe.

**Independent Test**: Exercise each state transition and verify one notification is emitted for each new transition and none for unchanged ticks.

**Acceptance Scenarios**:

1. **Given** the stop moves to cost-covered break-even, **When** Bybit confirms the new native stop, **Then** Telegram receives `BREAK EVEN ACTIVATED` and `PROFIT PROTECTED`.
2. **Given** trailing tightens the stop, **When** Bybit confirms it, **Then** Telegram receives `TRAILING UPDATED`.
3. **Given** a reversal causes an early reduce-only close, **When** the close is accepted and reconciled, **Then** Telegram receives `EARLY PROFIT EXIT`.

### Edge Cases

- A position is missing its initial stop or take profit: fail closed, leave exchange protection untouched, and report the condition.
- A process restarts while a position is open: reconstruct state from durable records and the live Bybit position without resetting the maximum favorable excursion or loosening the stop.
- WebSocket disconnects or becomes stale: reconnect with backoff; do not modify protection from stale prices.
- A tick crosses multiple thresholds at once: apply only the safest single monotonic stop update and persist the resulting stage.
- Bybit rounds prices to instrument tick size: round toward less claimed protection, then independently verify the exact native level.
- Bybit accepts a protection request but verification times out: retain durable pending/unknown state and do not assume either success or failure.
- Position quantity changes or the position closes externally: reconcile before further action and never create or increase a position.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST monitor only already-open real positions and MUST NOT create or add quantity to any position.
- **FR-002**: The monitor MUST operate without Hermes, Codex, or any other AI request.
- **FR-003**: For each open position the system MUST durably retain entry, side, quantity, current price, initial stop, initial target, initial monetary risk, current net profit/loss, maximum favorable excursion, accumulated known fees, current protected stop, protection stage, and update timestamps.
- **FR-004**: Profit thresholds MUST use monetary net profit/loss divided by initial monetary risk, after known and estimated round-trip costs, and MUST NOT use exchange ROI percentage.
- **FR-005**: At +0.5R or greater, the system MUST move the native stop to a price intended to cover entry cost and estimated exit cost, spread, and slippage.
- **FR-006**: At +1R or greater, the system MUST move the native stop to protect at least +0.3R net profit.
- **FR-007**: At +1R or greater, the system MUST calculate an adaptive trailing candidate from recent closed market intervals and current volatility.
- **FR-008**: For LONG positions a replacement stop MUST be strictly greater than the last confirmed stop; for SHORT positions it MUST be strictly lower.
- **FR-009**: The original native target MUST remain unchanged by this feature.
- **FR-010**: Each stop change MUST use the existing guarded exchange gateway, MUST preserve the existing target in the same request, and MUST be independently confirmed from the live position before state is marked confirmed.
- **FR-011**: The previous protection MUST never be explicitly cancelled before a replacement is confirmed.
- **FR-012**: An early exit MUST be allowed only after the position achieved at least +1R and a deterministic reversal rule based on closed intervals is satisfied.
- **FR-013**: Early exit MUST be full-position, reduce-only, idempotent, and reconciled before completion is recorded.
- **FR-014**: Stale, missing, malformed, or contradictory market/position data MUST result in no mutation.
- **FR-015**: Restart recovery MUST preserve the highest favorable excursion and the tightest confirmed stop.
- **FR-016**: Notifications MUST be emitted only after confirmed state transitions and MUST use the exact labels `PROFIT PROTECTED`, `BREAK EVEN ACTIVATED`, `TRAILING UPDATED`, and `EARLY PROFIT EXIT`.
- **FR-017**: Existing entry decisions, scanning, symbols, leverage, quantity sizing, original stop/target calculation, duplicate protection, reconciliation, kill switch, and emergency-close behavior MUST remain unchanged.
- **FR-018**: Verification MUST use deterministic mocks and read-only checks; it MUST send zero real test orders.

### Key Entities

- **Protection State**: Durable per-position state containing immutable initial values, evolving market observations, monotonic confirmed protection, and the current protection stage.
- **Protection Event**: An idempotent record of a proposed, confirmed, failed, or unknown stop update or early exit.
- **Market Observation**: A fresh price update plus recent confirmed closed intervals used for net-R and volatility/momentum calculations.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Deterministic LONG and SHORT tests activate break-even on the first observation at or above +0.5R and never below it.
- **SC-002**: Deterministic LONG and SHORT tests protect at least +0.3R on the first observation at or above +1R.
- **SC-003**: Across every tested favorable-then-adverse price path, confirmed protection moves zero times away from profit.
- **SC-004**: Restart tests preserve 100% of the recorded favorable excursion and tightest confirmed stop.
- **SC-005**: Failure tests for stale data, rejected updates, timeouts, and mismatched verification produce zero unguarded or duplicate mutations.
- **SC-006**: Monitoring and all protection tests make exactly zero AI requests and zero real test orders.
- **SC-007**: Existing execution, scanner, reconciliation, Telegram, and startup regression tests continue to pass unchanged.

## Assumptions

- Initial monetary risk equals the price distance from entry to the original stop multiplied by quantity, plus known entry fees and conservative estimated exit costs.
- Net break-even includes known entry fees plus conservative estimated exit fee, half-spread, and slippage; this intentionally protects slightly beyond raw entry.
- Adaptive trailing uses recent fully closed five-minute intervals; incomplete intervals cannot trigger a protection change or early exit.
- A substantial favorable move means the position has reached at least +1R after costs.
- A sharp reversal requires both adverse short-term momentum over consecutive closed intervals and a retracement from maximum favorable excursion; one transient tick is insufficient.
- The system may use public streaming prices with authenticated position reads; exchange mutations remain routed through the existing production gateway.

