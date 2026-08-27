"""Fail-closed compatibility process: Shadow execution is disabled."""

import asyncio
import logging

from app.shadow.logging import configure_structured_logging, log_event


async def run() -> None:
    log_event(
        logging.getLogger(__name__),
        logging.INFO,
        "shadow_disabled",
        {
            "collectors_running": 0,
            "watchdog": "OFF",
            "auto_restart": "OFF",
            "telegram_alerts": "OFF",
        },
    )
    await asyncio.Event().wait()


def main() -> None:
    configure_structured_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
