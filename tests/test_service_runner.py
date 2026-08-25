import pytest

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
