"""Select the Railway process without duplicating deployment configuration."""

import os
import sys


def command_for_role(role: str) -> list[str]:
    if role == "shadow":
        return [
            sys.executable,
            "-m",
            "app.shadow.supervisor",
            "--protocol-lock",
            "/app/phase4i-prospective-lock.json",
            "--warmup-file",
            "/app/phase4i-warmup.json.gz",
            "--poll-seconds",
            "60",
            "--watchdog-seconds",
            "30",
        ]
    if role == "telegram":
        return [sys.executable, "-m", "app.telegram.runner"]
    raise RuntimeError(f"Unknown SERVICE_ROLE: {role}")


def main() -> None:
    command = command_for_role(os.getenv("SERVICE_ROLE", "shadow").strip().lower())
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
