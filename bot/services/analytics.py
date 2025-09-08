from __future__ import annotations
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar
import asyncio

from aiogram.types import CallbackQuery, Message

from bot.analytics.amplitude import AmplitudeTelegramLogger
from bot.analytics.types import AbstractAnalyticsLogger, BaseEvent, EventProperties, EventType, UserProperties
from bot.core.config import settings
from bot.utils.singleton import SingletonMeta
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_Func = TypeVar("_Func")


class AnalyticsService(metaclass=SingletonMeta):
    def __init__(self, logger: AbstractAnalyticsLogger | None) -> None:
        self.logger = logger

    def _fire_and_forget(self, coro: Any) -> None:
        """Schedule analytics call without blocking handler.

        Any exception is caught and logged to avoid crashing the loop.
        """
        if coro is None:
            return
        task = asyncio.create_task(coro)
        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("analytics task failed: {}", e)
        task.add_done_callback(_done)

    def _track_error_fire(self, user_id: int, error_text: str) -> None:
        if not self.logger:
            return
        self._fire_and_forget(
            self.logger.log_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Error",
                    event_properties=EventProperties(text=error_text),
                )
            )
        )

    def fire_event(self, event: BaseEvent) -> None:
        """Schedule analytics event send without blocking handler.

        Safe to call without checking logger presence; this method is a no-op when logger is None.
        """
        if not self.logger:
            return
        self._fire_and_forget(self.logger.log_event(event))

    def track_event(
        self,
        event_name: EventType,
    ) -> Callable[[Callable[..., Awaitable[_Func]]], Callable[..., Awaitable[_Func]]]:
        """Decorator for tracking events in Amplitude, Google Analytics or Posthog."""

        def decorator(
            handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[_Func]],
        ) -> Callable[..., Awaitable[_Func]]:
            @wraps(handler)
            async def wrapper(update: Message | CallbackQuery, *args: Any, **kwargs: Any) -> Any:
                if not self.logger:
                    return await handler(update, *args, **kwargs)

                if isinstance(update, (Message, CallbackQuery)) and getattr(update, "from_user", None):
                    user_id = update.from_user.id
                    first_name = update.from_user.first_name
                    last_name = update.from_user.last_name
                    username = update.from_user.username
                    url = update.from_user.url
                    language = update.from_user.language_code
                else:
                    return None

                chat_id: int | None
                chat_type: str | None
                if isinstance(update, Message):
                    chat_id = update.chat.id
                    chat_type = update.chat.type
                    text = update.text
                    command = update.text if update.text and update.text.startswith("/") else None
                elif isinstance(update, CallbackQuery):
                    chat_id = update.message.chat.id if update.message else None
                    chat_type = update.message.chat.type if update.message else None
                    text = update.data
                    command = None

                # Do NOT block handler on external analytics
                self._fire_and_forget(
                    self.logger.log_event(
                        BaseEvent(
                            user_id=user_id,
                            event_type=event_name,
                            user_properties=UserProperties(
                                first_name=first_name,
                                last_name=last_name,
                                username=username,
                                url=url,
                            ),
                            event_properties=EventProperties(
                                chat_id=chat_id,
                                chat_type=chat_type,
                                text=text,
                                command=command,
                            ),
                            language=language,
                        )
                    )
                )
                try:
                    result = await handler(update, *args, **kwargs)
                except Exception as e:
                    # Schedule error event, don't block exception propagation
                    self._track_error_fire(user_id, str(e))
                    raise
                return result

            return wrapper

        return decorator


if settings.AMPLITUDE_API_KEY:
    base_url = getattr(settings, "AMPLITUDE_BASE_URL", None)
    logger = (
        AmplitudeTelegramLogger(api_token=settings.AMPLITUDE_API_KEY, base_url=base_url)
        if base_url else AmplitudeTelegramLogger(api_token=settings.AMPLITUDE_API_KEY)
    )
else:
    logger = None

analytics = AnalyticsService(logger)
