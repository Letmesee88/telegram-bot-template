from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

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

    # List of Telegram user IDs who must have admin rights.
    # Accepts JSON list (preferred) or comma-separated string in .env
    # Examples:
    #   ADMIN_USER_IDS=[141872590,123456789]
    #   ADMIN_USER_IDS=141872590,123456789
    ADMIN_USER_IDS: list[int] = []

    # Robust parsing for CSV/JSON env values
    @field_validator("ADMIN_USER_IDS", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):  # type: ignore[no-untyped-def]
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, (set, tuple)):
            return [int(x) for x in list(v)]
        if isinstance(v, (int,)):
            return [int(v)]
        if isinstance(v, str):
            s = v.strip()
            # Try JSON-like list first
            if s.startswith("[") and s.endswith("]"):
                try:
                    import json  # local import to avoid top-level dependency at import time
                    arr = json.loads(s)
                    return [int(x) for x in arr]
                except Exception:
                    # fall through to CSV parsing
                    pass
            # CSV parsing
            parts = [p.strip() for p in s.split(",") if p.strip()]
            out: list[int] = []
            for p in parts:
                try:
                    out.append(int(p))
                except Exception:
                    # ignore invalid tokens quietly
                    continue
            return out
        return []

    # OpenAI / FoodAI settings
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None  # optional, e.g. custom proxy/Azure endpoint
    FOODAI_PROVIDER: str = "stub"  # one of: stub, openai
    FOODAI_DEFAULT_MODEL: str = "gpt-5-mini"
    FOODAI_EDIT_MODEL: str = "gpt-5-mini"
    FOODAI_API: str = "chat"  # one of: chat, responses
    FOODAI_REASONING_EFFORT: str = "minimal"  # minimal|low|medium|high (responses API)
    FOODAI_TEXT_VERBOSITY: str = "low"        # low|medium|high (responses API)
    # Enable LLM-based NLU for edit flow. If OPENAI_API_KEY is missing, code will fallback to local parser.
    FOODAI_EDIT_NLU: bool = True
    # DEPRECATED: use FOODAI_ESCALATE_CONF instead (kept only for backward-compat in runtime fallback)
    # FOODAI_CONFIDENCE_ESCALATE: float = 0.70
    # Confidence display thresholds (for category rendering)
    FOODAI_CONF_LOW: float = 0.60
    FOODAI_CONF_HIGH: float = 0.80
    FOODAI_TIMEOUT: int = 20
    # Vision controls
    FOODAI_IMAGE_DETAIL: str = "low"  # low|high|auto
    FOODAI_IMAGE_DETAIL_HIGH_RETRY: bool = True  # retry photo analysis with detail=high if confidence below threshold
    FOODAI_VISION_MODEL: str = "gpt-5-mini"  # preferred model for image analysis (falls back to FOODAI_DEFAULT_MODEL)
    # Analysis text rewrite controls
    # auto|always|off — auto: validate and rewrite only if needed; always: always rewrite; off: never rewrite
    FOODAI_ANALYSIS_REWRITE: str = "auto"
    FOODAI_ANALYSIS_REWRITE_TIMEOUT: int = 8

    # If True, photo precheck (foodness) failure aborts analysis with provider_unavailable.
    # If False (default), failure is treated as inconclusive and analysis proceeds.
    FOODAI_PRECHECK_STRICT: bool = False

    # Use Responses API for GPT-5 series models (e.g., gpt-5, gpt-5-mini) when analyzing photos.
    # When False (default), fall back to Chat API for stability; code may still try Responses behind
    # a per-attempt fallback if explicitly enabled at runtime.
    FOODAI_USE_RESPONSES_FOR_5: bool = False

    # Vision model escalation (feature-flagged)
    # Chain of models to try in order, split by '>' (e.g., "gpt-5-mini>gpt-5").
    FOODAI_VISION_ESCALATION_ENABLED: bool = True
    FOODAI_VISION_ESCALATION_CHAIN: str | None = "gpt-5-mini>gpt-5"
    # Escalation triggers and limits
    FOODAI_ESCALATE_CONF: float = 0.70
    FOODAI_ESCALATE_ITEMS_MIN: int = 3
    FOODAI_ESCALATE_ZERO_FIELDS: bool = True
    FOODAI_ESCALATE_ON_PROVIDER_ERROR: bool = True
    FOODAI_VISION_TOTAL_TIMEOUT: int = 25
    FOODAI_VISION_MAX_STEPS: int = 2
    FOODAI_VISION_DETAIL_ORDER: str = "low>high"
    # Fallbacks and UX flags
    FOODAI_ALLOW_FALLBACK_TO_4O_MINI: bool = True
    FOODAI_TEXT_FALLBACK_TO_CHAT: bool = True
    FOODAI_SHOW_LOW_CONF_HINT: bool = False
    # Show confidence category labels (низкая/средняя) in preview
    FOODAI_SHOW_CONF_LABELS: bool = True

    # Adjustment (onboarding final corrections) LLM settings
    ADJUST_LLM_ENABLED: bool = True
    ADJUST_LLM_MODEL: str | None = "gpt-4o-mini"
    ADJUST_LLM_TIMEOUT_SEC: float = 2.5
    ADJUST_LLM_CONF_MIN: float = 0.6

    # Adjustment explanation rephrasing (hybrid UX)
    ADJUST_REPHRASE_ENABLED: bool = False
    # neutral|friendly|clinical — controls tone only; numbers/units must remain EXACTLY the same
    ADJUST_REPHRASE_TONE: str = "neutral"
    # Small timeout, we fall back to deterministic text on timeout
    ADJUST_REPHRASE_TIMEOUT_SEC: float = 1.8
    # Rephrase only if explanation is long enough to benefit
    ADJUST_REPHRASE_LENGTH_MIN: int = 220

    # Adjustment engine mode:
    # - deterministic: LLM только классифицирует намерения, все числа считает код
    # - hybrid: LLM может подсказывать числа, но мы валидируем и пересчитываем по правилам
    ADJUST_ENGINE_MODE: str = "hybrid"  # deterministic|hybrid

    # Градуировка силы изменения (используется в hybrid-режиме)
    # Калории: проценты уменьшения/увеличения
    ADJUST_STRENGTH_CAL_PERCENT_SLIGHT: float = 5.0
    ADJUST_STRENGTH_CAL_PERCENT_MODERATE: float = 10.0
    ADJUST_STRENGTH_CAL_PERCENT_STRONG: float = 15.0

    # Углеводы (целевые граммы для low_carb по степени)
    ADJUST_STRENGTH_CARBS_G_SLIGHT: int = 120
    ADJUST_STRENGTH_CARBS_G_MODERATE: int = 80
    ADJUST_STRENGTH_CARBS_G_STRONG: int = 60

    # Жиры (дельта в граммах по степени; минимум жиров всё равно соблюдается)
    ADJUST_STRENGTH_FAT_DELTA_G_SLIGHT: int = 10
    ADJUST_STRENGTH_FAT_DELTA_G_MODERATE: int = 20
    ADJUST_STRENGTH_FAT_DELTA_G_STRONG: int = 30

    # Белок (целевые г/кг по степени; будут зажаты в безопасный диапазон 1.2..2.4 г/кг)
    ADJUST_STRENGTH_PROTEIN_GKG_SLIGHT: float = 1.6
    ADJUST_STRENGTH_PROTEIN_GKG_MODERATE: float = 1.8
    ADJUST_STRENGTH_PROTEIN_GKG_STRONG: float = 2.0

    # Принимать ли кастомные макросы без указания единиц ("г")
    ADJUST_ACCEPT_CUSTOM_MACROS_WITHOUT_UNITS: bool = False

    # Activity LLM classification
    ACTIVITY_LLM_ENABLED: bool = True
    ACTIVITY_LLM_MODEL: str | None = "gpt-4o-mini"
    ACTIVITY_LLM_TIMEOUT_SEC: float = 8.0

    # Charts / goal projection rendering
    CHARTS_ENABLED: bool = True
    CHARTS_PROVIDER: str = "quickchart"  # quickchart|off (reserve for future: plotly)
    CHARTS_PRIVACY_MODE: str = "kg"  # percent|kg
    CHARTS_BAND_FRAC: float = 0.0  # +/- 20% of weekly rate
    # Curve and style
    CHARTS_EASE: str = "ease_out"  # linear|ease_out
    CHARTS_EASE_POWER: float = 2.5  # strength of ease-out (>=1.0)
    CHARTS_FILL_MAIN: bool = True   # fill area under main curve
    CHARTS_SHOW_MARKERS: bool = True  # show start/goal markers
    CHARTS_USE_DATALABELS: bool = True  # show value labels on markers via plugin
    # Extra breathing room for Y axis around midpoint (kg mode only)
    CHARTS_Y_MARGIN_KG: float = 1.0

    # Color theme (override defaults below to match competitor)
    CHARTS_COLOR_BG: str | None = "#5368FF"          # canvas background
    CHARTS_COLOR_GRID: str | None = "#FFFFFF30"      # grid lines
    CHARTS_COLOR_AXIS: str | None = "#FFFFFF"        # axes/labels
    CHARTS_COLOR_LINE: str | None = "#00E676"        # main line (green)
    CHARTS_COLOR_BAND: str | None = None             # not used when band=0
    CHARTS_COLOR_LABEL_BG: str | None = "#FFFFFF"    # label chip background
    CHARTS_COLOR_LABEL_FG: str | None = "#1E293B"    # label chip text

    # QuickChart config
    QUICKCHART_URL: str = "https://quickchart.io/chart"
    QUICKCHART_WIDTH: int = 1200
    QUICKCHART_HEIGHT: int = 600
    QUICKCHART_TIMEOUT_SEC: float = 5


settings = Settings()
