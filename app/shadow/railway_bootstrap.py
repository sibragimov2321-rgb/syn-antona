"""One-time, local-to-Railway migration for the locked Phase 4I runtime."""

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Railway PostgreSQL and transfer the existing Phase 4I state"
    )
    parser.add_argument("--database-env", default="DATABASE_PUBLIC_URL")
    parser.add_argument("--source", default="sqlite:///./phase4a-real.db")
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--warmup-file", type=Path, default=Path("phase4i-warmup.json.gz")
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("phase4i-postgres-transfer-manifest.json")
    )
    arguments = parser.parse_args()

    target_url = os.getenv(arguments.database_env)
    if not target_url:
        raise RuntimeError(f"Railway variable {arguments.database_env} is unavailable")
    os.environ["DATABASE_URL"] = target_url
    os.environ["LIVE_TRADING_ENABLED"] = "false"

    from alembic.config import main as alembic_main

    alembic_main(argv=["upgrade", "head"])

    from app.shadow.transfer import transfer_runtime

    result = transfer_runtime(
        arguments.source,
        target_url,
        arguments.protocol_lock,
        warmup_file=arguments.warmup_file,
    )
    temporary = arguments.manifest.with_suffix(arguments.manifest.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, arguments.manifest)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
