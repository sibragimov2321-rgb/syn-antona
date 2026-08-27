import pytest
from pathlib import Path

from app.core.config import Settings
from app.service_runner import command_for_role


def test_railway_service_roles_are_isolated():
    shadow = command_for_role("shadow")
    telegram = command_for_role("telegram")

    assert "app.shadow.supervisor" in shadow
    assert "app.telegram.runner" not in shadow
    assert "app.telegram.runner" in telegram
    assert "app.shadow.supervisor" not in telegram


def test_unknown_railway_service_role_is_rejected():
    with pytest.raises(RuntimeError, match="Unknown SERVICE_ROLE"):
        command_for_role("live")


def test_docker_image_includes_immutable_controlled_live_profile():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "COPY config ./config" in dockerfile


def test_watchdog_window_covers_long_multi_exchange_hourly_cycle():
    settings = Settings(_env_file=None)
    assert settings.shadow_lease_seconds == 900
    assert settings.shadow_heartbeat_max_age_seconds == 900
