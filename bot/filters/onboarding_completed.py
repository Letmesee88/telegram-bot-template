from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from bot.database.models.onboarding_answer import OnboardingAnswerModel
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties
from aiogram.utils.i18n import gettext as _


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
            # Clear callback "loading" if present
            if hasattr(event, "answer"):
                try:
                    await event.answer()
                except Exception:
                    pass
            # Prefer replying in chat context for callbacks
            msg_obj = getattr(event, "message", None)
            if msg_obj and hasattr(msg_obj, "answer"):
                try:
                    await msg_obj.answer(text, reply_markup=self._cta_kb())
                    raise SystemExit  # prevent duplicate send below
                except SystemExit:
                    pass
                except Exception:
                    pass
            # Fallback: direct answer on message
            if hasattr(event, "answer"):
                try:
                    await event.answer(text, reply_markup=self._cta_kb())
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
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=_("Начать"), callback_data="onboarding_start")]]
        )
