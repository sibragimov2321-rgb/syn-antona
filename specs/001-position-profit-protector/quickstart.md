# Quickstart Validation: Local Position Profit Protector

## Prerequisites

- Development dependencies installed.
- No production credentials are required for deterministic tests.
- Production remains untouched during implementation verification.

## Targeted verification

```powershell
pytest -q tests/test_profit_protector.py tests/test_bybit_v5_gateway.py
```

Expected: LONG/SHORT thresholds, costs, monotonic stops, trailing, reversal, restart, timeout, idempotency, gateway confirmation, notifications, and zero AI calls all pass.

## Migration verification

```powershell
$env:DATABASE_URL = "sqlite+pysqlite:///./profit-protector-migration-check.db"
alembic upgrade head
alembic current
```

Expected: revision `20260905_22` is current and all existing plus two new tables are present.

## Regression verification

```powershell
pytest -q
ruff check app tests
python -m app.preflight
```

Expected: established tests and lint pass; no real order is submitted.

## Non-mutating production check before release

Confirm there are no unknown ledger orders, the controlled-live worker is healthy, Bybit reconciliation matches, and the new feature flag is still disabled. Deployment/activation is a separate release action after these checks pass.
