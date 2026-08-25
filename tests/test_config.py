from app.core.config import get_settings
import pytest


def test_railway_postgresql_url_uses_installed_psycopg_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@postgres:5432/database")
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == (
            "postgresql+psycopg://user:password@postgres:5432/database"
        )
    finally:
        get_settings.cache_clear()


def test_live_runtime_requires_both_controlled_arming_flags(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "false")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "false")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CONTROLLED_LIVE_ENABLED"):
        get_settings()
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="MANUAL_FIRST_ORDER_APPROVED"):
        get_settings()
    get_settings.cache_clear()
