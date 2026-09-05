# Tasks: Local Position Profit Protector

**Input**: Design documents from `/specs/001-position-profit-protector/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Deterministic tests are mandatory because the feature mutates protection for real positions.

## Phase 1: Setup

- [x] T001 Add the direct WebSocket runtime dependency and disabled-by-default protector setting in `pyproject.toml`, `app/core/config.py`, `.env.example`, and `.env.production.example`

## Phase 2: Foundational

- [x] T002 Add the two additive protector ORM records in `app/db.py`
- [x] T003 Add and validate the additive migration in `migrations/versions/20260905_22_position_profit_protector.py`
- [x] T004 Write failing persistence/restart and pure-calculation tests in `tests/test_profit_protector.py`

## Phase 3: User Story 1 - Preserve earned profit (Priority: P1)

**Goal**: Activate cost-covered break-even at +0.5R and at least +0.3R profit lock plus ATR trailing at +1R, with monotonic protection for LONG and SHORT.

**Independent Test**: Deterministic price paths produce exact expected stages and never loosen a confirmed stop.

- [x] T005 [US1] Implement net-PnL/R, cost, ATR, tick rounding, break-even, profit-lock, and monotonic trailing calculations in `app/trading/profit_protector.py`
- [x] T006 [US1] Implement durable ownership/state/event lifecycle and restart recovery in `app/trading/profit_protector.py`
- [x] T007 [US1] Extend guarded risk-reducing ownership authorization and native protection update/verification in `app/exchanges/bybit_v5_gateway.py`
- [x] T008 [US1] Complete LONG/SHORT, threshold, tick rounding, stale-data, timeout, no-loosening, idempotency, and restart tests in `tests/test_profit_protector.py` and `tests/test_bybit_v5_gateway.py`

## Phase 4: User Story 2 - React locally to momentum reversal (Priority: P2)

**Goal**: Permit one deterministic reconciled reduce-only early exit after substantial profit and a confirmed sharp reversal.

**Independent Test**: A known favorable-then-reversal candle series produces exactly one close; early/noisy reversals produce none.

- [x] T009 [US2] Implement closed-candle reversal evaluation and unknown-safe event handling in `app/trading/profit_protector.py`
- [x] T010 [US2] Add guarded idempotent protective close and reconciliation support in `app/exchanges/bybit_v5_gateway.py`
- [x] T011 [US2] Add early-exit, duplicate, timeout/UNKNOWN, and no-AI-call tests in `tests/test_profit_protector.py` and `tests/test_bybit_v5_gateway.py`

## Phase 5: User Story 3 - Observe protection events (Priority: P3)

**Goal**: Run continuously beside the existing scanner and notify only confirmed material events.

**Independent Test**: Fake WebSocket/position streams survive reconnect and emit exactly one required notification per confirmed transition.

- [x] T012 [US3] Implement the reconnecting Bybit public ticker/closed-kline stream in `app/trading/profit_protector.py`
- [x] T013 [US3] Add the four event notifications without changing existing messages in `app/shadow/notifier.py`
- [x] T014 [US3] Start and stop the independent monitor task with the existing controlled-live worker in `app/trading/controlled_live_runner.py`
- [x] T015 [US3] Add stream/reconnect, worker lifecycle, notification deduplication, and entry-regression tests in `tests/test_profit_protector.py` and `tests/test_service_runner.py`

## Phase 6: Verification and release gate

- [x] T016 Run targeted protector/gateway/worker tests and fix all failures
- [x] T017 Run the full test suite, lint, additive migration upgrade, and non-mutating startup/preflight checks
- [x] T018 Review diff for entry/strategy/risk/Hermes changes, secrets, and unintended scope in the whole branch
- [x] T019 Perform a read-only production readiness check while the protector flag remains disabled; do not deploy or send a real test order

## Phase 7: Early MFE giveback protection amendment

- [x] T020 Specify fixed cost-aware watch, 35% giveback protection, and 50% giveback-plus-momentum early-exit rules without changing entry behavior
- [x] T021 Extend the pure decision result with MFE/giveback diagnostics and implement LONG/SHORT rules in `app/trading/profit_protector.py`
- [x] T022 Persist and deduplicate notification-only `PROFIT WATCH` transitions using the existing event/state tables
- [x] T023 Extend Telegram protection notifications with exact watch/protect/exit labels and current PnL/MFE/giveback/action
- [x] T024 Add deterministic threshold, no-premature-exit, monotonic-stop, restart/idempotency, notifier, and zero-AI tests
- [ ] T025 Run targeted tests, full suite, lint, migration-head check, and production startup/runtime verification; send zero real test orders

## Dependencies & Execution Order

- T001-T004 establish configuration, persistence, migration, and failing tests.
- User Story 1 depends on T001-T004 and is required before User Story 2.
- User Story 2 depends on confirmed monotonic protection from User Story 1.
- User Story 3 depends on both action paths and integrates them into the worker.
- Verification depends on all user stories.

## Implementation Strategy

Implement and verify the pure deterministic safety core first, then guarded exchange mutations, then streaming/lifecycle integration. Production deployment and activation remain outside implementation until all critical checks pass.
