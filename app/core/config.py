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
    controlled_live_enabled: bool = False
    manual_first_order_approved: bool = False
    dry_run: bool = True
    # Shadow is an archived prospective experiment in production.  The switch is
    # deliberately independent from the controlled-live execution gates.
    shadow_execution_enabled: bool = False
    ai_provider: str = "mock"
    ai_model: str = "mock-v1"
    ai_api_key: str | None = None
    ai_base_url: str | None = None
    ai_trading_enabled: bool = False
    ai_confidence_threshold: int = 70
    ai_scan_interval_seconds: int = 300
    ai_position_notional_usdt: float = 15.0
    ai_timeout: float = 45.0
    ai_required: bool = True
    ai_max_requests_per_hour: int = 60
    ai_max_requests_per_day: int = 500
    shadow_quote_stale_seconds: int = 30
    shadow_live_candle_grace_seconds: int = 180
    shadow_offline_after_failures: int = 3
    # A full 1H multi-exchange cycle can legitimately exceed five minutes.
    # Keep one bounded 15-minute watchdog window so a healthy long cycle is
    # not killed while a genuinely stuck process is still restarted.
    shadow_lease_seconds: int = 900
    shadow_heartbeat_max_age_seconds: int = 900
    shadow_api_timeout_ms: int = 15_000

    def assert_safe_runtime(self) -> None:
        if self.live_trading_enabled and not self.controlled_live_enabled:
            raise RuntimeError(
                "CONTROLLED_LIVE_ENABLED must also be true before live execution can arm."
            )
        if self.live_trading_enabled and not self.manual_first_order_approved:
            raise RuntimeError(
                "MANUAL_FIRST_ORDER_APPROVED must also be true before live execution can arm."
            )
        if self.ai_trading_enabled:
            missing = [
                name
                for name, value in (
                    ("AI_API_KEY", self.ai_api_key),
                    ("AI_BASE_URL", self.ai_base_url),
                    ("AI_MODEL", self.ai_model),
                )
                if not value or str(value).strip().lower() in {"mock", "mock-v1"}
            ]
            if missing:
                raise RuntimeError(
                    "AI_TRADING_ENABLED requires configured " + ", ".join(missing)
                )
            if not (
                self.live_trading_enabled
                and self.controlled_live_enabled
                and self.manual_first_order_approved
                and not self.dry_run
            ):
                raise RuntimeError(
                    "AI live execution requires all production arming gates and DRY_RUN=false"
                )
            if self.ai_confidence_threshold != 70:
                raise RuntimeError("AI_CONFIDENCE_THRESHOLD must remain 70")
            if self.ai_scan_interval_seconds != 300:
                raise RuntimeError("AI_SCAN_INTERVAL_SECONDS must remain 300")
            if self.ai_position_notional_usdt != 15.0:
                raise RuntimeError("AI_POSITION_NOTIONAL_USDT must remain 15")
            if not self.admin_telegram_ids:
                raise RuntimeError("AI live execution requires ADMIN_TELEGRAM_IDS")


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
