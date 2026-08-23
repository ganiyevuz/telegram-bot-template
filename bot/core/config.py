from __future__ import annotations
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DIR = Path(__file__).absolute().parent.parent.parent
BOT_DIR = Path(__file__).absolute().parent.parent
LOCALES_DIR = f"{BOT_DIR}/locales"
I18N_DOMAIN = "messages"
DEFAULT_LOCALE = "en"

_ENV = SettingsConfigDict(env_file=f"{DIR}/.env", env_file_encoding="utf-8", extra="ignore")


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="BOT_")

    token: SecretStr
    support_url: str | None = Field(default=None, validation_alias="SUPPORT_URL")
    rate_limit: float = Field(default=0.5, validation_alias="RATE_LIMIT")


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="DB_")

    host: str = "postgres"
    port: int = 5432
    user: str = "postgres"
    password: SecretStr | None = Field(default=None, validation_alias="DB_PASS")
    name: str = "postgres"
    pool_size: int = 10
    max_overflow: int = 5

    @property
    def url(self) -> str:
        if not self.password or not self.password.get_secret_value():
            auth = self.user
        else:
            auth = f"{self.user}:{self.password.get_secret_value()}"
        return f"postgresql+asyncpg://{auth}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="REDIS_")

    host: str = "redis"
    port: int = 6379
    password: SecretStr | None = Field(default=None, validation_alias="REDIS_PASS")
    db: int = 0

    @property
    def url(self) -> str:
        if not self.password or not self.password.get_secret_value():
            auth = ""
        else:
            auth = f":{self.password.get_secret_value()}@"
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="WEBHOOK_")

    enabled: bool = Field(default=False, validation_alias="USE_WEBHOOK")
    base_url: str = "https://example.com"
    path: str = "/webhook"
    secret: SecretStr = SecretStr("")
    verify_source_ip: bool = Field(default=True, validation_alias="WEBHOOK_VERIFY_SOURCE_IP")
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV)

    amplitude_api_key: str | None = Field(default=None, validation_alias="AMPLITUDE_API_KEY")
    posthog_api_key: str | None = Field(default=None, validation_alias="POSTHOG_API_KEY")
    flush_interval_seconds: int = Field(default=30, validation_alias="ANALYTICS_FLUSH_INTERVAL")
    buffer_key: str = "analytics:buffer"


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV)

    sentry_dsn: str | None = Field(default=None, validation_alias="SENTRY_DSN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


class Settings(BaseSettings):
    model_config = _ENV

    debug: bool = Field(default=False, validation_alias="DEBUG")

    bot: BotSettings = Field(default_factory=BotSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
