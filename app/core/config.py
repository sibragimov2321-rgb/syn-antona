from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./trading.db"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: str | None = None
    encryption_key: str | None = None
    admin_telegram_ids: set[int] = Field(default_factory=set)
    live_trading_enabled: bool = False
    ai_provider: str = "mock"
    ai_model: str = "mock-v1"
    ai_api_key: str | None = None
    ai_timeout: float = 10.0
    ai_required: bool = True
    ai_max_requests_per_hour: int = 60
    ai_max_requests_per_day: int = 500
    shadow_quote_stale_seconds: int = 30
    shadow_live_candle_grace_seconds: int = 180
    shadow_offline_after_failures: int = 3
    shadow_lease_seconds: int = 300
    shadow_heartbeat_max_age_seconds: int = 300
    shadow_api_timeout_ms: int = 15_000

    def assert_safe_runtime(self) -> None:
        if self.live_trading_enabled:
            raise RuntimeError(
                "Live trading is not implemented in this phase. Set LIVE_TRADING_ENABLED=false."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.database_url.startswith("postgresql://"):
        settings.database_url = settings.database_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    elif settings.database_url.startswith("postgres://"):
        settings.database_url = settings.database_url.replace(
            "postgres://", "postgresql+psycopg://", 1
        )
    settings.assert_safe_runtime()
    return settings
