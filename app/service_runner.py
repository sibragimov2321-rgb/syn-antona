"""Select the Railway process without duplicating deployment configuration."""

import os
import sys


def command_for_role(role: str) -> list[str]:
    if role == "shadow":
        # Production-safe fail-closed compatibility role: no collector, lease,
        # watchdog, alerts, or trading process is started.
        return [sys.executable, "-m", "app.shadow.disabled"]
    if role == "controlled_live":
        return [sys.executable, "-m", "app.trading.controlled_live_runner"]
    if role == "telegram":
        return [sys.executable, "-m", "app.telegram.runner"]
    if role == "bybit_preflight":
        return [sys.executable, "-m", "app.exchanges.bybit_readonly_worker"]
    raise RuntimeError(f"Unknown SERVICE_ROLE: {role}")


def main() -> None:
    command = command_for_role(
        os.getenv("SERVICE_ROLE", "controlled_live").strip().lower()
    )
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
