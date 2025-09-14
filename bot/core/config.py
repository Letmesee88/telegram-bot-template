from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from sqlalchemy.engine.url import URL

DIR = Path(__file__).absolute().parent.parent.parent
BOT_DIR = Path(__file__).absolute().parent.parent
LOCALES_DIR = f"{BOT_DIR}/locales"
I18N_DOMAIN = "messages"
DEFAULT_LOCALE = "ru"


class EnvBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class WebhookSettings(EnvBaseSettings):
    USE_WEBHOOK: bool = False
    WEBHOOK_BASE_URL: str = "https://xxx.ngrok-free.app"
    WEBHOOK_PATH: str = "/webhook"
    WEBHOOK_SECRET: str = ""
    WEBHOOK_HOST: str = "localhost"
    WEBHOOK_PORT: int = 8080

    @property
    def webhook_url(self) -> str:
        if settings.USE_WEBHOOK:
            return f"{self.WEBHOOK_BASE_URL}{self.WEBHOOK_PATH}"
        return f"http://localhost:{settings.WEBHOOK_PORT}{settings.WEBHOOK_PATH}"


class BotSettings(WebhookSettings):
    BOT_TOKEN: str
    SUPPORT_URL: str | None = None
    RATE_LIMIT: int | float = 0.5  # for throttling control
    PAY_URL: str | None = None


class DBSettings(EnvBaseSettings):
    DB_HOST: str = "postgres"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASS: str | None = None
    DB_NAME: str = "postgres"

    @property
    def database_url(self) -> URL | str:
        if self.DB_PASS:
            return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        return f"postgresql+asyncpg://{self.DB_USER}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def database_url_psycopg2(self) -> str:
        if self.DB_PASS:
            return f"postgresql://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        return f"postgresql://{self.DB_USER}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"


class CacheSettings(EnvBaseSettings):
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASS: str | None = None

    # REDIS_DATABASE: int = 1
    # REDIS_USERNAME: int | None = None
    # REDIS_TTL_STATE: int | None = None
    # REDIS_TTL_DATA: int | None = None

    @property
    def redis_url(self) -> str:
        if self.REDIS_PASS:
            return f"redis://{self.REDIS_PASS}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


class Settings(BotSettings, DBSettings, CacheSettings):
    DEBUG: bool = False

    SENTRY_DSN: str | None = None

    AMPLITUDE_API_KEY: str  # or for example it could be POSTHOG_API_KEY
    AMPLITUDE_BASE_URL: str | None = None  # e.g., https://api.eu.amplitude.com/2/httpapi for EU region
    # Tests can force synchronous analytics in /start to avoid race conditions
    ANALYTICS_SYNC_START: bool = False

    # OpenAI / FoodAI settings
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None  # optional, e.g. custom proxy/Azure endpoint
    FOODAI_PROVIDER: str = "stub"  # one of: stub, openai
    FOODAI_DEFAULT_MODEL: str = "gpt-5-mini"
    FOODAI_EDIT_MODEL: str = "gpt-5"
    FOODAI_API: str = "chat"  # one of: chat, responses
    FOODAI_REASONING_EFFORT: str = "minimal"  # minimal|low|medium|high (responses API)
    FOODAI_TEXT_VERBOSITY: str = "low"        # low|medium|high (responses API)
    # Enable LLM-based NLU for edit flow. If OPENAI_API_KEY is missing, code will fallback to local parser.
    FOODAI_EDIT_NLU: bool = True
    FOODAI_CONFIDENCE_ESCALATE: float = 0.70
    # Confidence display thresholds (for category rendering)
    FOODAI_CONF_LOW: float = 0.60
    FOODAI_CONF_HIGH: float = 0.80
    FOODAI_TIMEOUT: int = 20
    # Vision controls
    FOODAI_IMAGE_DETAIL: str = "low"  # low|high|auto
    FOODAI_IMAGE_DETAIL_HIGH_RETRY: bool = True  # retry photo analysis with detail=high if confidence below threshold
    FOODAI_VISION_MODEL: str = "gpt-4o-mini"  # preferred model for image analysis (falls back to FOODAI_DEFAULT_MODEL)
    # Analysis text rewrite controls
    # auto|always|off — auto: validate and rewrite only if needed; always: always rewrite; off: never rewrite
    FOODAI_ANALYSIS_REWRITE: str = "auto"
    FOODAI_ANALYSIS_REWRITE_TIMEOUT: int = 8


settings = Settings()
