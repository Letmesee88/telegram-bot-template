from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import UserModel


class FoodAIEnabledFilter(BaseFilter):
    """Allows only users with active premium AND FoodAI feature enabled.

    Conditions:
    - users.is_premium == True
    - users.foodai_enabled_at IS NOT NULL
    """

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession) -> bool:
        # Duck-typing to work with tests and wrapper objects
        user = getattr(event, "from_user", None)
        if not user:
            return False

        db_user = await session.get(UserModel, user.id)
        if not db_user:
            return False

        ok = bool(getattr(db_user, "is_premium", False)) and (db_user.foodai_enabled_at is not None)
        if ok:
            return True

        # Send CTA to purchase subscription
        try:
            # Avoid duplicate sends across multiple routers/handlers
            if getattr(event, "_foodai_cta_sent", False):
                return False
            setattr(event, "_foodai_cta_sent", True)
            # Also dedup on the underlying message object if present
            msg_obj = getattr(event, "message", None) or (event if isinstance(event, Message) else None)
            if msg_obj is not None:
                if getattr(msg_obj, "_foodai_cta_sent", False):
                    return False
                setattr(msg_obj, "_foodai_cta_sent", True)

            # Do not spam during subscription navigation callbacks
            data = getattr(event, "data", None)
            if isinstance(data, str) and data.startswith("sale:"):
                return False

            # Show CTA only on actual FoodAI attempts:
            # - Photo message
            # - Plain text message (not a command)
            # - FoodAI callbacks (prefix 'foodai:')
            attempted = False
            if msg_obj is not None:
                try:
                    if getattr(msg_obj, "photo", None):
                        attempted = True
                    else:
                        t = getattr(msg_obj, "text", None)
                        if isinstance(t, str) and t.strip() and not t.startswith("/"):
                            attempted = True
                except Exception:
                    pass
            if isinstance(data, str) and data.startswith("foodai:"):
                attempted = True
            if not attempted:
                return False

            # Stop loading if it's a callback
            if hasattr(event, "answer"):
                try:
                    await event.answer()
                except Exception:
                    pass
            text = (
                "😴 Подписка не активна\n"
                "С каждым днём ты можешь быть ближе к цели, но без анализа это движение вслепую. \n"
                "calorissimo_ai_bot поможет тебе достичь нужного результата"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="sale:choose")]]
            )
            # Prefer replying in chat context for callbacks
            msg_obj = getattr(event, "message", None)
            if msg_obj and hasattr(msg_obj, "answer"):
                try:
                    await msg_obj.answer(text, reply_markup=kb, disable_web_page_preview=True)
                    return False
                except Exception:
                    pass
            # Fallback: direct answer on message
            if hasattr(event, "answer"):
                try:
                    await event.answer(text, reply_markup=kb, disable_web_page_preview=True)
                except Exception:
                    pass
        except Exception:
            pass
        return False
