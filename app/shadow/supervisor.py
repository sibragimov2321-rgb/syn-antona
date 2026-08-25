import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from app.core.config import get_settings
from app.shadow.logging import configure_structured_logging, log_event
from app.shadow.repository import ShadowRepository
from app.shadow.watchdog import check_health


logger = logging.getLogger(__name__)


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> None:
    configure_structured_logging()
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Watch and restart Phase 4I collector")
    parser.add_argument(
        "--protocol-lock",
        type=Path,
        default=Path("phase4i-prospective-lock.json"),
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument(
        "--warmup-file", type=Path, default=Path("phase4i-warmup.json.gz")
    )
    parser.add_argument("--watchdog-seconds", type=int, default=30)
    arguments = parser.parse_args()
    stopping = False
    stop_event = threading.Event()
    process = None

    def request_stop(signum, frame) -> None:
        nonlocal stopping
        stopping = True
        stop_event.set()
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    instance_id = os.getenv("SHADOW_INSTANCE_ID") or str(uuid.uuid4())
    child_environment = dict(os.environ, SHADOW_INSTANCE_ID=instance_id)
    restart_delay = 1
    while not stopping:
        command = [
            sys.executable,
            "-m",
            "app.shadow.runner",
            "--protocol-lock",
            str(arguments.protocol_lock),
            "--warmup-file",
            str(arguments.warmup_file),
            "--poll-seconds",
            str(arguments.poll_seconds),
        ]
        process = subprocess.Popen(command, env=child_environment)
        started = time.monotonic()
        log_event(
            logger,
            logging.INFO,
            "collector_process_started",
            {"pid": process.pid, "instance_id": instance_id},
        )
        restart_reason = None
        while not stopping and process.poll() is None:
            stop_event.wait(arguments.watchdog_seconds)
            if time.monotonic() - started < settings.shadow_heartbeat_max_age_seconds:
                continue
            try:
                health = check_health(
                    ShadowRepository(),
                    arguments.protocol_lock,
                    heartbeat_max_age=settings.shadow_heartbeat_max_age_seconds,
                    quote_max_age=300,
                    candle_max_age=7500,
                )
                if health["critical"]:
                    restart_reason = "; ".join(health["critical"])
                    break
            except Exception as error:
                restart_reason = f"watchdog failure: {type(error).__name__}: {error}"
                break
        if process.poll() is None:
            _stop(process)
        if stopping:
            break
        if process.returncode == 78:
            log_event(
                logger,
                logging.CRITICAL,
                "protocol_mismatch_supervisor_stopped",
                {"exit_code": process.returncode},
            )
            break
        restart_reason = restart_reason or f"collector exited with code {process.returncode}"
        log_event(
            logger,
            logging.ERROR,
            "collector_process_restart",
            {"reason": restart_reason, "delay_seconds": restart_delay},
        )
        stop_event.wait(restart_delay)
        restart_delay = min(60, restart_delay * 2)
    if process and process.poll() is None:
        _stop(process)
    log_event(logger, logging.INFO, "shadow_supervisor_stopped")


if __name__ == "__main__":
    main()
