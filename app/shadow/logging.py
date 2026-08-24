from datetime import UTC, datetime
import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = details
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_structured_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    details: dict | None = None,
    *,
    exc_info: bool = False,
) -> None:
    logger.log(
        level,
        event,
        extra={"event": event, "details": details or {}},
        exc_info=exc_info,
    )
