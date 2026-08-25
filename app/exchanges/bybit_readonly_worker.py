"""Idle Railway process for manually invoked GET-only Bybit preflight commands."""

import os
import signal
import threading


def main() -> None:
    if os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() != "false":
        raise RuntimeError("Read-only worker requires LIVE_TRADING_ENABLED=false")
    print(
        "BYBIT READ-ONLY WORKER READY "
        f"api_key={'SET' if os.getenv('BYBIT_API_KEY') else 'NOT SET'} "
        f"api_secret={'SET' if os.getenv('BYBIT_API_SECRET') else 'NOT SET'} "
        "live_trading=false",
        flush=True,
    )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    stopped.wait()


if __name__ == "__main__":
    main()
