# Implementation Plan: Local Position Profit Protector

**Branch**: `position-profit-protector` | **Date**: 2026-09-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-position-profit-protector/spec.md`

## Summary

Add a local, deterministic monitor for bot-owned open Bybit positions. Public WebSocket prices and closed five-minute candles drive net-PnL/R thresholds; durable PostgreSQL state preserves initial risk, maximum favorable excursion, confirmed stop, and idempotency across restart. Extend the same monitor with a cost-aware `PROFIT WATCH`, 35% MFE-giveback protection, and 50% MFE-giveback early exit gated by confirmed adverse momentum. Every stop change and early exit reuses the existing production gateway and its private API safety boundary. No entry, strategy, initial SL/TP, scanner, symbol, leverage, or AI behavior changes.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: asyncio, SQLAlchemy 2, Alembic, httpx, websockets, aiogram

**Storage**: PostgreSQL in production; SQLite-compatible additive migration for tests

**Testing**: pytest 8 with deterministic async fakes and existing regression suite

**Target Platform**: Existing Linux Railway controlled-live worker

**Project Type**: Long-running worker plus Telegram service

**Performance Goals**: Process streaming ticks without blocking the five-minute entry scanner; update MFE on every fresh WebSocket quote; at most one position refresh per 10 seconds and no AI calls from the monitor

**Constraints**: No real test orders; no stop loosening; closed candles only for ATR/momentum; existing TP preserved; timeout/unknown fails closed; only ledger-owned positions may mutate

**Scale/Scope**: Up to the existing maximum of three simultaneous positions across the current eight-symbol allowlist

## Constitution Check

*GATE: Passed before research and re-checked after design.*

- **Safety Before Trading Capability — PASS**: mutations remain in the existing guarded gateway; the monitor cannot open or add size; stop movement is monotonic; unknown outcomes fail closed.
- **Mandatory Spec-Driven Workflow — PASS**: spec, plan, research, data model, contracts, quickstart, and tasks precede application edits.
- **Minimal and Backward-Compatible Changes — PASS**: one additive monitor, two additive tables, gateway extensions, notifier methods, and worker task wiring; entry flow and public contracts are unchanged.
- **Evidence-Based Verification — PASS**: deterministic LONG/SHORT, threshold, restart, stale data, timeout, idempotency, notification, no-AI, gateway, migration, and regression tests are planned.
- **Durable State and Operational Integrity — PASS**: state and event records are additive and authoritative across restart; no existing record is renamed or deleted.
- **Early MFE amendment — PASS**: reuses existing state/event tables, stream, native-stop verification, and reduce-only close; no migration, entry-flow, or parameter changes outside the protector.
- **Production release gate — PASS WITH SEPARATE RELEASE TASK**: code verification occurs before any deployment; deployment may enable only the new feature flag and must not send a test order.

## Project Structure

### Documentation (this feature)

```text
specs/001-position-profit-protector/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── position-protector.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── core/config.py
├── db.py
├── exchanges/bybit_v5_gateway.py
├── shadow/notifier.py
└── trading/
    ├── controlled_live_runner.py
    └── profit_protector.py
migrations/versions/
└── 20260905_22_position_profit_protector.py
tests/
├── test_position_profit_protector.py
├── test_bybit_v5_gateway.py
└── test_service_runner.py
```

**Structure Decision**: Reuse the current single Python package, existing controlled-live worker, gateway, notifier, ORM registry, and Alembic chain. The monitor belongs under `app/trading` because it is deterministic position-risk management rather than AI strategy logic.

## Complexity Tracking

No constitutional exceptions are required.
