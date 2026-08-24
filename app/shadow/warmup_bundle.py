import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path

from app.backtest.core import Candle
from app.backtest.persistence import HistoricalCandleCache
from app.shadow.protocol import WARMUP_CANDLES, canonical_json, warmup_hash


def _protocol_hash(protocol: dict) -> str:
    return sha256(canonical_json(protocol)).hexdigest()


def _serialize(candle: Candle) -> dict:
    return {
        "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def _deserialize(values: dict) -> Candle:
    return Candle(
        datetime.fromisoformat(values["timestamp"]).astimezone(UTC),
        Decimal(values["open"]),
        Decimal(values["high"]),
        Decimal(values["low"]),
        Decimal(values["close"]),
        Decimal(values["volume"]),
    )


def export_warmup_bundle(
    protocol_path: Path,
    output_path: Path,
    cache: HistoricalCandleCache | None = None,
) -> dict:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(protocol["warmup"]["cutoff"]).astimezone(UTC)
    candle_cache = cache or HistoricalCandleCache()
    warmups = {}
    for exchange in protocol["exchanges"]:
        for symbol in protocol["assets"]:
            candles = candle_cache.load(
                exchange,
                symbol,
                "1h",
                cutoff - timedelta(hours=WARMUP_CANDLES + 24),
                cutoff,
            )
            candles = [candle for candle in candles if candle.timestamp < cutoff][
                -WARMUP_CANDLES:
            ]
            if len(candles) != WARMUP_CANDLES:
                raise RuntimeError(
                    f"Immutable warm-up incomplete for {exchange} {symbol}: {len(candles)}"
                )
            warmups[(exchange, symbol)] = candles
    actual_hash = warmup_hash(warmups)
    if actual_hash != protocol["warmup"]["data_hash"]:
        raise RuntimeError("PROTOCOL HASH MISMATCH: cached warm-up differs from lock")
    payload = {
        "format": "phase4i_immutable_warmup_v1",
        "protocol_hash": _protocol_hash(protocol),
        "config_hash": protocol["strategy_config_hash"],
        "warmup_hash": actual_hash,
        "cutoff": cutoff.isoformat(),
        "candles_per_exchange_asset": WARMUP_CANDLES,
        "created_at": datetime.now(UTC).isoformat(),
        "warmups": {
            f"{exchange}|{symbol}": [_serialize(candle) for candle in candles]
            for (exchange, symbol), candles in sorted(warmups.items())
        },
    }
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
    os.replace(temporary, output_path)
    return {
        "output": str(output_path.resolve()),
        "protocol_hash": payload["protocol_hash"],
        "warmup_hash": actual_hash,
        "combinations": len(warmups),
        "candles": sum(len(rows) for rows in warmups.values()),
        "bundle_sha256": sha256(output_path.read_bytes()).hexdigest(),
    }


def load_warmup_bundle(path: Path, protocol: dict) -> dict:
    if not path.exists():
        raise RuntimeError(
            "PROTOCOL HASH MISMATCH: immutable warm-up bundle is missing"
        )
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    checks = {
        "format": payload.get("format") == "phase4i_immutable_warmup_v1",
        "protocol": payload.get("protocol_hash") == _protocol_hash(protocol),
        "config": payload.get("config_hash") == protocol["strategy_config_hash"],
        "locked warm-up": payload.get("warmup_hash")
        == protocol["warmup"]["data_hash"],
        "cutoff": payload.get("cutoff") == protocol["warmup"]["cutoff"],
    }
    failed = [label for label, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(
            "PROTOCOL HASH MISMATCH: warm-up bundle " + ", ".join(failed)
        )
    warmups = {}
    expected = {
        (exchange, symbol)
        for exchange in protocol["exchanges"]
        for symbol in protocol["assets"]
    }
    for key, rows in payload["warmups"].items():
        exchange, symbol = key.split("|", 1)
        warmups[(exchange, symbol)] = [_deserialize(row) for row in rows]
    if set(warmups) != expected or any(
        len(rows) != WARMUP_CANDLES for rows in warmups.values()
    ):
        raise RuntimeError("PROTOCOL HASH MISMATCH: warm-up universe/count differs")
    if warmup_hash(warmups) != protocol["warmup"]["data_hash"]:
        raise RuntimeError("PROTOCOL HASH MISMATCH: warm-up candle data differs")
    return warmups


def main() -> None:
    parser = argparse.ArgumentParser(description="Export immutable Phase 4I warm-up")
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("phase4i-warmup.json.gz")
    )
    arguments = parser.parse_args()
    print(
        json.dumps(
            export_warmup_bundle(
                arguments.protocol_lock, arguments.output
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
