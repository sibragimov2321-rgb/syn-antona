from app.core.config import get_settings


def test_railway_postgresql_url_uses_installed_psycopg_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@postgres:5432/database")
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == (
            "postgresql+psycopg://user:password@postgres:5432/database"
        )
    finally:
        get_settings.cache_clear()
