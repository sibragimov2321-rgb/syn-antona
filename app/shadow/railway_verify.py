"""Run deployment verification locally with Railway's public PostgreSQL URL."""

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the Railway Phase 4I deployment")
    parser.add_argument("--database-env", default="DATABASE_PUBLIC_URL")
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("phase4i-postgres-transfer-manifest.json")
    )
    parser.add_argument("--require-new-candle", action="store_true")
    arguments = parser.parse_args()

    database_url = os.getenv(arguments.database_env)
    if not database_url:
        raise RuntimeError(f"Railway variable {arguments.database_env} is unavailable")
    os.environ["DATABASE_URL"] = database_url
    os.environ["LIVE_TRADING_ENABLED"] = "false"

    from app.shadow.repository import ShadowRepository
    from app.shadow.verify_deployment import verify_deployment

    result = verify_deployment(
        ShadowRepository(),
        arguments.protocol_lock,
        arguments.manifest,
        require_new_candle=arguments.require_new_candle,
    )
    import json

    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    if result["status"] != "VERIFIED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
