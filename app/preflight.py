"""One-command deterministic pre-launch correctness gate.

This command intentionally does not enable testnet/live execution and never reads or prints secrets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings


CRITICAL_TESTS = (
    "tests/test_prelaunch_execution.py",
    "tests/test_phase4d_audit.py",
    "tests/test_risk_manager.py",
    "tests/test_paper_broker.py",
    "tests/test_backtest.py",
    "tests/test_phase4a_core.py",
    "tests/test_multi_exchange.py",
    "tests/test_phase4i_resilience.py",
    "tests/test_security.py",
    "tests/test_ai.py",
    "tests/test_bybit_readonly.py",
    "tests/test_controlled_live.py",
)


def _run(command: list[str], root: Path) -> int:
    return subprocess.run(command, cwd=root, check=False).returncode


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = get_settings()
    safety = {
        "checked_at": datetime.now(UTC).isoformat(),
        "live_trading_enabled": settings.live_trading_enabled,
        "controlled_live_enabled": settings.controlled_live_enabled,
        "manual_first_order_approved": settings.manual_first_order_approved,
        "real_orders_possible_from_deployed_services": False,
        "private_bybit_credentials_required_for_this_gate": False,
        "critical_test_files": len(CRITICAL_TESTS),
    }
    print(json.dumps(safety, indent=2), flush=True)
    if settings.live_trading_enabled:
        raise SystemExit("BLOCKER: LIVE_TRADING_ENABLED must be false during preflight")

    pytest_code = _run(
        [sys.executable, "-m", "pytest", "-q", *CRITICAL_TESTS],
        root,
    )
    ruff_code = _run([sys.executable, "-m", "ruff", "check", "app", "tests"], root)
    if pytest_code or ruff_code:
        raise SystemExit(1)
    print("PRE-LAUNCH AUTOMATED GATE: PASS", flush=True)
    print("LIVE TRADING REMAINS DISABLED", flush=True)


if __name__ == "__main__":
    main()
