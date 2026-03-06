import contextlib

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.analytics.types import BaseEvent, EventProperties
from bot.database.models.onboarding_answer import OnboardingAnswerModel
from bot.services.analytics import analytics


class OnboardingCompletedFilter(BaseFilter):
    """Allows only users who have completed onboarding (OnboardingAnswerModel exists). If not completed,
    sends a CTA message to start onboarding and logs analytics event. """

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession) -> bool:
        # Duck-typing to ease testing and be resilient to wrapper types
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if not user:
            return False

        exists = await session.scalar(
            select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user.id)
        )
        ok = bool(exists)
        if ok:
            return True

        # Tell user to complete onboarding
        text = _("Завершите онбординг за пару минут, чтобы получить полный доступ к данным")
        try:
            if hasattr(event, "answer"):
                with contextlib.suppress(Exception):
                    await event.answer()
            msg_obj = getattr(event, "message", None)
            if msg_obj and hasattr(msg_obj, "answer"):
                try:
                    await msg_obj.answer(text, reply_markup=self._cta_kb())
                    return False
                except Exception:
                    pass
            if not msg_obj and hasattr(event, "answer"):
                try:
                    await event.answer(text, reply_markup=self._cta_kb())
                    return False
                except Exception:
                    pass
        except Exception:
            pass

        # Analytics
        try:
            if analytics.logger and user:
                analytics.fire_event(
                    BaseEvent(
                        user_id=user.id,
                        event_type="Gated:OnboardingRequired",
                        event_properties=EventProperties(
                            chat_id=getattr(chat, "id", None),
                            chat_type=getattr(chat, "type", None),
                            command=None,
                            text=getattr(event, "data", None) if isinstance(event, CallbackQuery) else None,
                        ),
                        language=getattr(user, "language_code", None),
                    )
                )
        except Exception:
            pass
        return False

    @staticmethod
    def _cta_kb():
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=_("Начать"), callback_data="onboarding_start")]]
        )
