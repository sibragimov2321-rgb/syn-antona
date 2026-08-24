import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, select, text

from app.core.config import get_settings
from app.db import Base
from app.shadow.engine import PROTOCOL_ID
from app.shadow.protocol import canonical_json
from app.shadow.warmup_bundle import load_warmup_bundle
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH


TABLES = (
    "prospective_protocols",
    "shadow_candles",
    "shadow_quotes",
    "shadow_decisions",
    "shadow_trades",
    "shadow_daily_snapshots",
    "shadow_collector_state",
    "shadow_exchange_health",
    "shadow_system_events",
)


def _value(value):
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return normalized.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _rows(connection, table) -> list[dict]:
    statement = select(table)
    if "protocol_id" in table.c:
        statement = statement.where(table.c.protocol_id == PROTOCOL_ID)
    elif table.name == "prospective_protocols":
        statement = statement.where(table.c.id == PROTOCOL_ID)
    primary = list(table.primary_key.columns)
    if primary:
        statement = statement.order_by(*primary)
    return [dict(row) for row in connection.execute(statement).mappings()]


def _fingerprint(rows: list[dict]) -> str:
    normalized = [
        {key: _value(value) for key, value in sorted(row.items())} for row in rows
    ]
    return sha256(canonical_json(normalized)).hexdigest()


def _protocol_from_rows(rows: list[dict], lock_path: Path) -> dict:
    if len(rows) != 1:
        raise RuntimeError("PROTOCOL HASH MISMATCH: source database lock missing")
    file_protocol = json.loads(lock_path.read_text(encoding="utf-8"))
    file_hash = sha256(canonical_json(file_protocol)).hexdigest()
    row = rows[0]
    if row["protocol_hash"] != file_hash:
        raise RuntimeError("PROTOCOL HASH MISMATCH: source database/file differs")
    if row["config_hash"] != FROZEN_CONFIG_HASH:
        raise RuntimeError("PROTOCOL HASH MISMATCH: frozen config differs")
    return file_protocol


def transfer_runtime(
    source_url: str,
    target_url: str,
    lock_path: Path,
    *,
    warmup_file: Path | None = None,
    require_postgresql: bool = True,
) -> dict:
    if not lock_path.exists():
        raise RuntimeError("PROTOCOL HASH MISMATCH: protocol lock file missing")
    source_engine = create_engine(source_url, pool_pre_ping=True)
    target_engine = create_engine(target_url, pool_pre_ping=True)
    if require_postgresql and target_engine.dialect.name != "postgresql":
        raise RuntimeError("Production shadow target must be PostgreSQL")
    if source_engine.url.render_as_string(hide_password=False) == target_engine.url.render_as_string(
        hide_password=False
    ):
        raise RuntimeError("Source and target databases must differ")
    tables = {name: Base.metadata.tables[name] for name in TABLES}
    source_rows = {}
    with source_engine.connect() as source:
        for name, table in tables.items():
            source_rows[name] = _rows(source, table)
    protocol = _protocol_from_rows(source_rows["prospective_protocols"], lock_path)
    warmup_verification = None
    if warmup_file:
        warmups = load_warmup_bundle(warmup_file, protocol)
        warmup_verification = {
            "bundle_sha256": sha256(warmup_file.read_bytes()).hexdigest(),
            "combinations": len(warmups),
            "candles": sum(len(rows) for rows in warmups.values()),
            "warmup_hash": protocol["warmup"]["data_hash"],
        }

    with target_engine.begin() as target:
        other_protocols = target.execute(
            select(tables["prospective_protocols"].c.id).where(
                tables["prospective_protocols"].c.id != PROTOCOL_ID
            )
        ).all()
        if other_protocols:
            raise RuntimeError("Target database is not dedicated to Phase 4I")
        for name, table in tables.items():
            rows = source_rows[name]
            if not rows:
                continue
            primary = list(table.primary_key.columns)
            if len(primary) != 1:
                raise RuntimeError(f"Unsupported composite primary key in {name}")
            key = primary[0]
            existing = set(target.execute(select(key)).scalars())
            missing = [row for row in rows if row[key.name] not in existing]
            if missing:
                target.execute(table.insert(), missing)
        if target_engine.dialect.name == "postgresql":
            for name, table in tables.items():
                key = list(table.primary_key.columns)[0]
                if key.type.python_type is not int:
                    continue
                target.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:table_name, :column_name), "
                        "COALESCE((SELECT MAX(\"id\") FROM \""
                        + name
                        + "\"), 1), true)"
                    ),
                    {"table_name": name, "column_name": key.name},
                )

    verification = {}
    with target_engine.connect() as target:
        for name, table in tables.items():
            target_rows = _rows(target, table)
            source_hash = _fingerprint(source_rows[name])
            target_hash = _fingerprint(target_rows)
            if len(source_rows[name]) != len(target_rows) or source_hash != target_hash:
                raise RuntimeError(f"PostgreSQL transfer verification failed for {name}")
            verification[name] = {
                "rows": len(source_rows[name]),
                "fingerprint": source_hash,
            }
    return {
        "status": "TRANSFER VERIFIED",
        "protocol_id": PROTOCOL_ID,
        "locked_at": protocol["locked_at"],
        "strategy_version": protocol["strategy_version"],
        "config_hash": protocol["strategy_config_hash"],
        "protocol_hash": source_rows["prospective_protocols"][0]["protocol_hash"],
        "target_dialect": target_engine.dialect.name,
        "warmup_bundle": warmup_verification,
        "baseline_last_candle_close": max(
            (
                row["candle_close_time"]
                for row in source_rows["shadow_candles"]
            ),
            default=None,
        ),
        "tables": verification,
        "live_trading_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer the existing Phase 4I runtime from SQLite to PostgreSQL"
    )
    parser.add_argument("--source", default="sqlite:///./phase4a-real.db")
    parser.add_argument("--target", default=get_settings().database_url)
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
    result = transfer_runtime(
        arguments.source,
        arguments.target,
        arguments.protocol_lock,
        warmup_file=arguments.warmup_file,
    )
    temporary = arguments.manifest.with_suffix(arguments.manifest.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, arguments.manifest)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
