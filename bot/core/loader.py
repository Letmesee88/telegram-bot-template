from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n.core import I18n
from aiohttp import web
from redis.asyncio import ConnectionPool, Redis

from bot.core.config import DEFAULT_LOCALE, I18N_DOMAIN, LOCALES_DIR, settings

app = web.Application()

token = settings.BOT_TOKEN

bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

redis_client = Redis(
    connection_pool=ConnectionPool(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASS,
        db=0,
    ),
)

storage = RedisStorage(
    redis=redis_client,
    key_builder=DefaultKeyBuilder(with_bot_id=True),
)

dp = Dispatcher(storage=storage)

try:
    i18n: I18n = I18n(path=LOCALES_DIR, default_locale=DEFAULT_LOCALE, domain=I18N_DOMAIN)
except Exception:
    # In tests locales may be missing or not compiled; provide a minimal fallback
    class _DummyI18n:
        def gettext(self, message: str) -> str:
            return message

        def __call__(self, message: str) -> str:
            return message

    i18n = _DummyI18n()

DEBUG = settings.DEBUG
